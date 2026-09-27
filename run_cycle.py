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
from capital import DUST_MIN_USD
from strategy import (
    MIN_POSITION_USD,
    build_strategies,
    capital_plan,
)

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
    """Return dict of pool_address -> position dict for currently active positions."""
    pos_path = lp_scoring.newest_file(positions_dir, "position_scan")
    if not pos_path:
        return {}
    try:
        data = lp_scoring.load_json(pos_path)
    except Exception:
        return {}
    positions = data.get("positions", []) if isinstance(data, dict) else data
    return {p.get("pool_address"): p for p in positions if p.get("pool_address")}


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
    base = int(time.time())
    idx = 1

    wallet = report.get("wallet") or {}
    strategies = {s["pool_address"]: s for s in report.get("strategies", [])}

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

    lines = [
        f"## Cycle — {_utc_iso()}",
        f"- sources: pools={report.get('sources', {}).get('pools')} positions={report.get('sources', {}).get('positions')} wallet={report.get('sources', {}).get('wallet_scan')}",
        f"- verdicts: {len(report.get('verdicts', []))}",
        f"- capital: idle={wallet.get('idle_usdc', 0.0):.2f} dust={wallet.get('dust_total_usdc', 0.0):.2f} deployable={wallet.get('deployable_usdc', 0.0):.2f}",
        f"- plan: suggested_position={capital.get('suggested_position_usdc', 0.0):.2f} open_eligible={capital.get('open_eligible', False)}",
    ]
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
    ap.add_argument("--wallet-scans-dir", default="/data/missy-data/wallet_scans")
    ap.add_argument("--write-signals", action="store_true")
    ap.add_argument("--signals-dir", default=DEFAULT_SIGNALS_DIR)
    ap.add_argument("--memory-dir", default=DEFAULT_MEMORY_DIR)
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

    open_candidates = [v for v in report.get("verdicts", [])
                       if v.get("action") == "OPEN_CANDIDATE"]
    report["strategies"] = build_strategies(open_candidates, wallet)

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
