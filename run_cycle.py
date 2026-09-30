#!/usr/bin/env python3
"""Sheldon cycle runner.

Runs the deterministic LP scoring engine and, if configured, emits George-schema
signal files. Zero model reasoning. Outputs: JSON report, human summary log line,
and optional signal JSON files for George.

Usage:
    python3 run_cycle.py [--write-signals] [--signals-dir /path] [--json]

Returns exit 0 on success, 1 on fatal error, 2 on stale/missing data.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import lp_scoring
import range_state
from capital import DUST_MIN_USD
from readiness import (
    build_prep_swap_signal,
    gate_prep_swaps,
    load_raw_wallet,
    plan_funding,
)
from strategy import (
    MIN_POSITION_USD,
    build_strategies,
    capital_plan,
    get_policy,
    meteora_bin_step_allowed,
)

# --------------------------------------------------------------------------
# Policy values loaded from sheldon_policy.json (single source).
# --------------------------------------------------------------------------
_POLICY = get_policy()

# Protocols George can execute. Signals for anything else stay in review.
SUPPORTED_DEXES = {"meteora", "raydium", "orca"}

# Where George's executor looks for pending signals.
DEFAULT_SIGNALS_DIR = "/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/signals/pending"
DEFAULT_MEMORY_DIR = "/data/.openclaw/workspace-agents/sheldon/memory"


def _utc_iso():
    # Use Asia/Shanghai (UTC+8) as the canonical timezone for timestamps.
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _load_active_positions(positions_dir: str) -> dict:
    """Return dict of pool_address -> position dict for positions not closed.

    Includes 'inactive' (zero-liquidity but still open on-chain) positions:
    re-opening a pool that already holds one creates duplicate positions,
    which is exactly the bug this dedup exists to prevent.
    """
    pos_path = lp_scoring.newest_file(positions_dir, "position_scan")
    if not pos_path:
        return {}
    try:
        data = lp_scoring.load_json(pos_path)
    except Exception:
        return {}
    positions = data.get("positions", []) if isinstance(data, dict) else data
    return {p.get("pool_address"): p for p in positions
            if p.get("pool_address") and p.get("status") != "closed"}


def _in_trading_window(windows: dict, action: str) -> bool:
    """Return True if the current Asia/Shanghai time is inside the configured
    trading window and not on a blackout date.
    """
    from datetime import datetime, timedelta, timezone
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)

    blackout = windows.get("blackout_dates") or []
    today = now.strftime("%Y-%m-%d")
    if today in blackout:
        return False

    window_key = "open_window_utc" if action == "open" else "close_window_utc"
    window = windows.get(window_key, "00:00-23:59")
    try:
        start_str, end_str = window.split("-")
        start_hour, start_min = map(int, start_str.strip().split(":"))
        end_hour, end_min = map(int, end_str.strip().split(":"))
    except (ValueError, AttributeError):
        return True

    current_min = now.hour * 60 + now.minute
    start_min_total = start_hour * 60 + start_min
    end_min_total = end_hour * 60 + end_min
    if end_min_total < start_min_total:
        return current_min >= start_min_total or current_min <= end_min_total
    return start_min_total <= current_min <= end_min_total


def _filter_open_candidates(open_candidates: list, active_positions: dict) -> tuple:
    """Drop open candidates that must not become signals.

    Returns (kept, skipped). Skips:
      - pools already holding a non-closed position (dedup),
      - duplicate pool addresses (first candidate wins),
      - candidates whose current tick/bin is unknown (center == 0):
        centering a range at 0 means the live price is unknown, which is
        how the ZEC/USDC out-of-range re-open happened.
      - pools outside the configured open window or on a blackout date,
      - pools that fail the Sheldon policy eligibility gates (TVL, volume),
      - Meteora pools whose bin_step is not in George's allowed_bin_steps
        rail (minimum 10): the executor would hard-reject the open, so
        the candidate is skipped here and explained as a review item.
    """
    kept = []
    skipped = []
    seen = set()
    policy_windows = {
        "open_window_utc": _POLICY.get("open_window_utc"),
        "close_window_utc": _POLICY.get("close_window_utc"),
        "blackout_dates": _POLICY.get("blackout_dates"),
    }
    min_liquidity = _POLICY.get("min_pool_liquidity_usd", 250000.0)
    min_volume = _POLICY.get("min_24h_volume_usd", 1000000.0)

    if not _in_trading_window(policy_windows, "open"):
        skipped.append({
            "pool_address": None,
            "dex": "any",
            "score": None,
            "reason": "outside configured open window or blackout date",
        })
        return kept, skipped

    for v in open_candidates:
        addr = v.get("pool_address")
        dex = v.get("dex") or "unknown"
        base = {"pool_address": addr, "dex": dex, "score": v.get("score")}
        if addr in active_positions:
            skipped.append({**base,
                            "reason": "already holding a position in this pool (dedup)"})
            continue
        if addr in seen:
            skipped.append({**base,
                            "reason": "duplicate pool in scan; first candidate kept"})
            continue
        pool = v.get("_pool") or {}
        if _range_center(pool, dex) == 0:
            skipped.append({**base,
                            "reason": "current tick/bin unknown (center=0); refusing to open blind"})
            continue

        tvl = float(pool.get("tvl") or 0.0)
        if tvl < min_liquidity:
            skipped.append({**base,
                            "reason": f"pool TVL ${tvl:.0f} < policy ${min_liquidity:.0f}"})
            continue
        volume = float(pool.get("volume_window") or pool.get("volume") or 0.0)
        if volume < min_volume:
            skipped.append({**base,
                            "reason": f"pool volume ${volume:.0f} < policy ${min_volume:.0f}"})
            continue

        bin_ok, bin_reason = meteora_bin_step_allowed(pool, dex)
        if not bin_ok:
            skipped.append({**base, "reason": bin_reason})
            continue
        seen.add(addr)
        kept.append(v)
    return kept, skipped


def _range_center(pool: dict, dex: str) -> int:
    """Return the pool's current position index.

    Meteora uses DLMM bin IDs (``active_bin_id``); Raydium CLMM and Orca
    Whirlpool use ticks. Missy may supply ``current_tick``,
    ``current_tick_index`` or reuse ``active_bin_id`` for the tick value.
    """
    if dex == "meteora":
        return int(pool.get("active_bin_id") or 0)
    for key in ("current_tick", "current_tick_index", "active_bin_id"):
        val = pool.get(key)
        if val not in (None, "", 0, "0"):
            return int(val)
    return 0


def _build_open_signal(strategy: dict, v: dict, idx: int, base: int) -> dict:
    """Build a George-schema 'open' signal from a strategy and verdict."""
    dex = v.get("dex") or "unknown"
    pool = v.get("_pool") or {}
    if dex not in SUPPORTED_DEXES or not pool:
        return None
    # Last-resort guard: even if a filtered candidate slipped through, the
    # builder must never emit an open for a bin_step rail violation.
    bin_ok, _reason = meteora_bin_step_allowed(pool, dex)
    if not bin_ok:
        return None

    # Pull prices and decimals.
    px_x = float(pool.get("token_x_price_usd") or 0.0)
    px_y = float(pool.get("token_y_price_usd") or 0.0)
    dec_x = int(pool.get("token_x_decimals") or 0)
    dec_y = int(pool.get("token_y_decimals") or 0)
    if px_x <= 0 or px_y <= 0 or dec_x <= 0 or dec_y <= 0:
        return None

    position_usd = strategy["suggested_usdc"]
    if position_usd < MIN_POSITION_USD:
        return None

    # 50/50 USD split.
    half_usd = position_usd / 2.0
    amount_x = int((half_usd / px_x) * (10 ** dec_x))
    amount_y = int((half_usd / px_y) * (10 ** dec_y))
    if amount_x <= 0 or amount_y <= 0:
        return None

    bin_range = strategy["bin_range"]

    return {
        "signal_id": f"sheldon-{base}-{idx}",
        "action": "open",
        "dex": dex,
        "pool_address": pool.get("pool_address"),
        "side": "bidirectional",
        "bin_range": {"lower": bin_range["lower"], "upper": bin_range["upper"]},
        "liquidity": {
            "amount_x": str(amount_x),
            "amount_y": str(amount_y),
        },
        "position_usd": position_usd,
        "max_slippage_bps": 100,
        "reason": (
            f"OPEN score={v.get('score')} dex={dex} "
            f"position_usd={position_usd:.2f} center={strategy['center']}"
        ),
        "created_at": _utc_iso(),
    }


def _build_dust_swap_signal(asset: dict, idx: int, base: int) -> dict:
    return {
        "signal_id": f"sheldon-{base}-{idx}",
        "action": "swap_to_usdc",
        "mint": asset["mint"],
        "symbol": asset["symbol"],
        "decimals": asset["decimals"],
        "amount_raw": asset["amount_raw"],
        "amount_ui": asset["amount_ui"],
        "value_usd": asset["value_usd"],
        "reason": (
            f"dust token: {asset.get('symbol') or 'unknown'} valued "
            f"${asset['value_usd']:.2f} (threshold ${DUST_MIN_USD})"
        ),
        "created_at": _utc_iso(),
    }


def write_signals(report: dict, signals_dir: str, positions_dir: str) -> tuple:
    """Convert verdicts into George-schema signal files.

    Returns:
        (created_paths, review_items)
        created_paths: list of files written to George's pending queue.
        review_items:  list of items that need human review.
    """
    os.makedirs(signals_dir, exist_ok=True)
    active = _load_active_positions(positions_dir)
    created = []
    review = []
    # Dedup/center-guard drops from main() surface as review items so the
    # report explains why a high-scoring pool produced no signal.
    for item in report.get("open_skipped", []):
        review.append({
            "pool_address": item.get("pool_address"),
            "dex": item.get("dex"),
            "score": item.get("score"),
            "evidence": None,
            "reason": item.get("reason", "open candidate skipped"),
        })
    base = int(time.time())
    idx = 1

    wallet = report.get("wallet") or {}
    strategies = {s["pool_address"]: s for s in report.get("strategies", [])}

    # 0. Capital prep swaps for the highest-scored unfunded strategy.
    # Gate before writing: no re-emit while the wallet rescan has not yet
    # caught up, and a hard stop when the loop stops converging.
    funding = report.get("funding")
    if funding is None:
        # Fail safe: without a funding plan, refuse to open blind.
        funding = {
            "funded": [],
            "unfunded": [
                {"pool_address": s.get("pool_address"),
                 "reason": "funding plan missing; refusing to open blind"}
                for s in report.get("strategies", [])
            ],
            "prep_swaps_allowed": [],
            "prep_swaps_blocked": [],
            "notes": [],
        }
    allowed_preps = funding.get("prep_swaps_allowed") or []

    # 0a. Blocked prep swaps surface as review items, never as signals.
    for b in funding.get("prep_swaps_blocked") or []:
        review.append({
            "pool_address": b.get("for_pool"),
            "dex": "jupiter",
            "score": None,
            "evidence": None,
            "reason": f"prep swap {b.get('direction')} blocked: {b.get('reason')}",
        })

    # 0b. Allowed prep swaps execute before any position signals.
    for spec in allowed_preps:
        signal = build_prep_swap_signal(spec, idx, base)
        path = os.path.join(signals_dir, f"{signal['signal_id']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(signal, fh, indent=2)
            fh.write("\n")
        created.append(path)
        idx += 1

    # 1. Dust swap signals (executed before any new positions).
    for asset in wallet.get("dust_assets", []):
        signal = _build_dust_swap_signal(asset, idx, base)
        path = os.path.join(signals_dir, f"{signal['signal_id']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(signal, fh, indent=2)
            fh.write("\n")
        created.append(path)
        idx += 1

    # 2. Position signals.
    for v in report.get("verdicts", []):
        action = v.get("action")
        pool_addr = v.get("pool_address")
        dex = v.get("dex") or "unknown"

        if action == "OPEN_CANDIDATE":
            # Only the top strategies chosen by build_strategies() get signals.
            if pool_addr not in strategies:
                review.append({
                    "pool_address": pool_addr,
                    "dex": dex,
                    "score": v.get("score"),
                    "evidence": v.get("evidence"),
                    "reason": "OPEN candidate skipped (capital or ranking gate)",
                })
                continue
            # Never emit an open when the strategy is not funded; the prep
            # swap above handles the shortfall and a later cycle opens.
            entry = next(
                (e for e in (funding.get("unfunded") or [])
                 if e.get("pool_address") == pool_addr),
                None,
            )
            if entry:
                review.append({
                    "pool_address": pool_addr,
                    "dex": dex,
                    "score": v.get("score"),
                    "evidence": v.get("evidence"),
                    "reason": f"OPEN deferred: {entry.get('reason') or 'tokens not funded'}",
                })
                continue
            signal = _build_open_signal(strategies[pool_addr], v, idx, base)
            if signal is None:
                review.append({
                    "pool_address": pool_addr,
                    "dex": dex,
                    "score": v.get("score"),
                    "evidence": v.get("evidence"),
                    "reason": "OPEN candidate; could not build signal (missing enrichment)",
                })
                continue
        elif action in {"CLOSE", "REBALANCE", "COLLECT_FEES"}:
            lower = v.get("lower_bound")
            upper = v.get("upper_bound")
            try:
                lower = int(lower) if lower is not None else 0
                upper = int(upper) if upper is not None else 0
            except (TypeError, ValueError):
                lower, upper = 0, 0
            signal = {
                "signal_id": f"sheldon-{base}-{idx}",
                "action": "claim_fees" if action == "COLLECT_FEES" else "close",
                "dex": dex,
                "pool_address": pool_addr,
                "position_id": v.get("position"),
                "side": "bidirectional",
                "bin_range": {"lower": lower, "upper": upper},
                "liquidity": {"amount_x": "0", "amount_y": "0"},
                "max_slippage_bps": 100,
                "reason": f"{action} score={v.get('score')}; evidence={json.dumps(v.get('evidence'))}",
                "created_at": _utc_iso(),
            }
        else:
            # HOLD, REVIEW, WATCH, IGNORE => no signal.
            continue

        # Only protocols George can execute get a queued signal.
        if dex not in SUPPORTED_DEXES:
            review.append({
                "pool_address": pool_addr,
                "dex": dex,
                "score": v.get("score"),
                "evidence": v.get("evidence"),
                "reason": f"{action} candidate ({dex}); unsupported protocol",
            })
            continue

        path = os.path.join(signals_dir, f"{signal['signal_id']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(signal, fh, indent=2)
            fh.write("\n")
        created.append(path)
        idx += 1

    return created, review


def _wake_george(signals_created: list, review: list):
    """Trigger the George signal doorbell immediately when signals are written.

    The doorbell automation checks signals/pending/ and sends George's main
    session a wake message only when fresh files are detected. The regular
    2-minute schedule remains as a fallback; this call shortens latency.
    """
    if not signals_created:
        return

    # Doorbell automation ID (george-signal-wake).
    doorbell_id = "26a163bb-5e85-4fe8-ba99-8e584bc2e09e"
    cmd = ["openclaw", "automations", "run", doorbell_id, "--wait"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        # Fallback doorbell will catch it on its next 2-minute tick; log failure.
        print(f"WAKE_GEORGE_FAILED: {exc}", file=sys.stderr)


def append_log(report: dict, memory_dir: str, signals_created: list, review: list):
    os.makedirs(memory_dir, exist_ok=True)
    log_path = os.path.join(memory_dir, time.strftime("%Y-%m-%d") + ".md")

    wallet = report.get("wallet", {})
    capital = report.get("capital_plan", {})
    funding = report.get("funding") or {}

    lines = [
        f"## Cycle — {_utc_iso()}",
        f"- sources: pools={report.get('sources', {}).get('pools')} positions={report.get('sources', {}).get('positions')} wallet={report.get('sources', {}).get('wallet_scan')}",
        f"- verdicts: {len(report.get('verdicts', []))}",
        f"- capital: idle={wallet.get('idle_usdc', 0.0):.2f} dust={wallet.get('dust_total_usdc', 0.0):.2f} deployable={wallet.get('deployable_usdc', 0.0):.2f}",
        f"- plan: suggested_position={capital.get('suggested_position_usdc', 0.0):.2f} open_eligible={capital.get('open_eligible', False)}",
    ]
    if funding:
        lines.append(
            f"- funding: funded={len(funding.get('funded') or [])} "
            f"unfunded={len(funding.get('unfunded') or [])} "
            f"prep_allowed={len(funding.get('prep_swaps_allowed') or [])} "
            f"prep_blocked={len(funding.get('prep_swaps_blocked') or [])} "
            f"sol_reserve={(funding.get('sol_reserve_lamports') or 0) / 1e9:.4f}"
        )
        for note in funding.get("notes") or []:
            lines.append(f"  - funding note: {note}")
    grace = report.get("out_of_range_grace") or {}
    if grace.get("deferred_positions"):
        lines.append(
            f"- out-of-range grace: {len(grace['deferred_positions'])} position(s) "
            f"held below {grace['allowed_runs']} consecutive out-of-range runs "
            f"({', '.join(grace['deferred_positions'])})"
        )
    if report.get("failures"):
        lines.append(f"- failures: {report['failures']}")
    if review:
        lines.append(f"- review needed: {len(review)}")
        for r in review:
            lines.append(f"  - `{r['pool_address']}` score={r.get('score')} — {r['reason']}")
    if signals_created:
        lines.append(f"- signals written: {len(signals_created)}")
        for p in signals_created:
            lines.append(f"  - `{p}`")
    else:
        lines.append("- signals written: 0 (no actionable verdicts)")
    lines.append("- summary: " + _short_summary(report, review))
    lines.append("")
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _short_summary(report: dict, review: list = None) -> str:
    parts = []
    opens = [v for v in report.get("verdicts", []) if v.get("action") == "OPEN_CANDIDATE"]
    closes = [v for v in report.get("verdicts", []) if v.get("action") == "CLOSE"]
    collects = [v for v in report.get("verdicts", []) if v.get("action") == "COLLECT_FEES"]
    wallet = report.get("wallet", {})
    capital = report.get("capital_plan", {})

    if opens or (review and len(review) > 0):
        parts.append(f"OPEN={len(opens)}")
    if closes:
        parts.append(f"CLOSE={len(closes)}")
    if collects:
        parts.append(f"COLLECT={len(collects)}")
    if wallet:
        parts.append(f"idle={wallet.get('idle_usdc', 0.0):.2f}")
        parts.append(f"dust={wallet.get('dust_total_usdc', 0.0):.2f}")
    if capital:
        parts.append(f"suggested={capital.get('suggested_position_usdc', 0.0):.2f}")
        parts.append(f"eligible={capital.get('open_eligible', False)}")
    if not parts:
        return "no actionable verdicts"
    return " | ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sheldon deterministic cycle runner")
    ap.add_argument("--pools-dir", default="/data/missy-data/pool_screens")
    ap.add_argument("--positions-dir", default="/data/missy-data/position_scans")
    ap.add_argument("--wallet-scans-dir", default="/data/missy-data/wallet_screens")
    ap.add_argument("--write-signals", action="store_true")
    ap.add_argument("--signals-dir", default=DEFAULT_SIGNALS_DIR)
    ap.add_argument("--memory-dir", default=DEFAULT_MEMORY_DIR)
    ap.add_argument("--state-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "state"))
    ap.add_argument("--json", action="store_true", help="print full JSON report to stdout")
    ap.add_argument("--max-age-seconds", type=float, default=3900.0)
    args = ap.parse_args()

    try:
        report = lp_scoring.run_cycle(
            args.pools_dir,
            args.positions_dir,
            args.wallet_scans_dir,
            args.max_age_seconds,
        )
    except lp_scoring.ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 1

    # Build capital plan and strategies from the loaded wallet scan.
    wallet = report.get("wallet") or lp_scoring._empty_wallet("wallet not loaded")
    report["capital_plan"] = capital_plan(wallet)

    active_positions = _load_active_positions(args.positions_dir)

    # Out-of-range grace (Mr. Man rail): a position that leaves its range
    # must survive ALLOWED_OUT_OF_RANGE_RUNS consecutive runs before a
    # CLOSE/REBALANCE verdict passes. Run 1 downgrades the verdict to HOLD
    # with a review reason (no close signal); run 2 lets it through.
    # Back in range resets the counter.
    grace_counts = range_state.update_out_of_range_counts(
        list(active_positions.values()),
        range_state.load_range_state(args.state_dir),
    )
    range_state.save_range_state(grace_counts, args.state_dir)
    report["verdicts"] = [
        range_state.out_of_range_position_verdict(v, grace_counts)
        for v in report.get("verdicts", [])
    ]
    report["out_of_range_grace"] = {
        "allowed_runs": range_state.ALLOWED_OUT_OF_RANGE_RUNS,
        "deferred_positions": sorted(
            addr for addr, n in grace_counts.items()
            if n < range_state.ALLOWED_OUT_OF_RANGE_RUNS
        ),
    }

    open_candidates = [v for v in report.get("verdicts", [])
                       if v.get("action") == "OPEN_CANDIDATE"]
    kept_candidates, open_skipped = _filter_open_candidates(
        open_candidates, active_positions
    )
    report["open_skipped"] = open_skipped
    report["strategies"] = build_strategies(kept_candidates, wallet)

    # Capital readiness: annotate strategies with their pool enrichment,
    # diff target token needs against the newest raw wallet scan, and gate
    # prep swaps (rescan-wait + hourly loop guard) before any signal write.
    pool_meta = {v.get("pool_address"): (v.get("_pool") or {}) for v in kept_candidates}
    for s in report["strategies"]:
        s.setdefault("_pool", pool_meta.get(s.get("pool_address")) or {})
    raw_wallet = load_raw_wallet(args.wallet_scans_dir)
    dust_mints = {a.get("mint") for a in (wallet.get("dust_assets") or []) if a.get("mint")}
    funding = plan_funding(
        report["strategies"], raw_wallet["assets"],
        wallet_path=raw_wallet["path"], wallet_mtime=raw_wallet["mtime"],
        dust_mints=dust_mints,
    )
    if raw_wallet.get("error"):
        funding["notes"].append(f"wallet scan unavailable: {raw_wallet['error']}")
    allowed_preps, blocked_preps = gate_prep_swaps(
        funding["prep_swaps"], args.signals_dir, funding["wallet_mtime"]
    )
    funding["prep_swaps_allowed"] = allowed_preps
    funding["prep_swaps_blocked"] = blocked_preps
    report["funding"] = funding

    if report.get("failures"):
        err = "; ".join(report["failures"])
        print(f"ERROR: {err}", file=sys.stderr)
        if args.json:
            json.dump(report, sys.stdout, indent=2)
            print()
        return 2

    signals_created = []
    review = []
    if args.write_signals:
        signals_created, review = write_signals(report, args.signals_dir, args.positions_dir)
        _wake_george(signals_created, review)

    append_log(report, args.memory_dir, signals_created, review)

    # Emit a machine-readable wake hint so the calling Sheldon session can
    # notify George immediately when actionable signals were written.
    if signals_created:
        summary = ", ".join(Path(p).name for p in signals_created)
        print(f"WAKE_GEORGE: {len(signals_created)} signal(s) -> {summary}")

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(_short_summary(report, review))

    return 0


if __name__ == "__main__":
    sys.exit(main())
