"""Signal building and writing logic extracted from run_cycle.py.

Contains:
  - _build_open_signal: build a George-schema 'open' signal from a strategy and verdict
  - _build_dust_swap_signal: build a dust swap signal
  - write_signals: convert verdicts into George-schema signal files

Import build_prep_swap_signal from readiness module for prep swap signal building.
"""

from pathlib import Path
import json
import os
import time
from datetime import datetime, timedelta, timezone

# Import prep swap signal builder from readiness module
from readiness import build_prep_swap_signal
from readiness import prep_ledger


def _utc_iso():
    """Return current time in Asia/Shanghai (UTC+8) ISO format."""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _build_open_signal(strategy: dict, v: dict, pool: dict, idx: int, base: int,
                       min_score: float, supported_dexes: set) -> dict:
    """Build a George-schema 'open' signal from a strategy and verdict.

    Returns signal dict or None if signal cannot be built.
    """
    dex = v.get("dex") or "unknown"
    if dex not in supported_dexes or not pool:
        return None

    # George enforces bin_step rail fail-closed
    from run_cycle.gates import _meteora_bin_step_allowed
    bin_ok, _reason = _meteora_bin_step_allowed(pool, dex)
    if not bin_ok:
        return None

    # Score must be present and at/above the policy floor
    score = v.get("score")
    if score is None or float(score) < min_score:
        return None

    center = strategy.get("center")
    if center is None:
        return None

    # Pull prices and decimals.
    px_x = float(pool.get("token_x_price_usd") or 0.0)
    px_y = float(pool.get("token_y_price_usd") or 0.0)
    dec_x = int(pool.get("token_x_decimals") or 0)
    dec_y = int(pool.get("token_y_decimals") or 0)
    if px_x <= 0 or px_y <= 0 or dec_x <= 0 or dec_y <= 0:
        return None

    position_usd = strategy.get("suggested_usdc", 0.0)
    from run_cycle.gates import MIN_POSITION_USD
    if position_usd < MIN_POSITION_USD:
        return None

    # 50/50 USD split.
    half_usd = position_usd / 2.0
    amount_x = int((half_usd / px_x) * (10 ** dec_x))
    amount_y = int((half_usd / px_y) * (10 ** dec_y))
    if amount_x <= 0 or amount_y <= 0:
        return None

    bin_range = strategy.get("bin_range", {})
    if not bin_range:
        return None

    return {
        "signal_id": f"sheldon-{base}-{idx}",
        "action": "open",
        "dex": dex,
        "wallet_id": "main",
        "pool_address": pool.get("pool_address"),
        "side": "bidirectional",
        "bin_range": {"lower": bin_range.get("lower", 0), "upper": bin_range.get("upper", 0)},
        "liquidity": {
            "amount_x": str(amount_x),
            "amount_y": str(amount_y),
        },
        "position_usd": position_usd,
        "score": float(score),
        "score_policy": {"source": "missy", "version": 1, "min_open_score": min_score},
        "max_slippage_bps": 100,
        "reason": (
            f"OPEN score={v.get('score')} threshold={min_score} dex={dex} "
            f"position_usd={position_usd:.2f} center={strategy.get('center')}"
        ),
        "created_at": _utc_iso(),
    }


def _build_dust_swap_signal(asset: dict, idx: int, base: int) -> dict:
    return {
        "signal_id": f"sheldon-{base}-{idx}",
        "action": "swap_to_usdc",
        "wallet_id": "main",
        "mint": asset["mint"],
        "symbol": asset["symbol"],
        "decimals": asset["decimals"],
        "amount_raw": asset["amount_raw"],
        "amount_ui": asset["amount_ui"],
        "value_usd": asset["value_usd"],
        "reason": (
            f"dust token: {asset.get('symbol') or 'unknown'} valued "
            f"${asset['value_usd']:.2f} (threshold ${asset.get('dust_threshold_usd', 1)})"
        ),
        "created_at": _utc_iso(),
    }


def write_signals(report: dict, signals_dir: str, positions_dir: str,
                  min_open_score: float = 70.0,
                  min_position_usd: float = 20.0,
                  supported_dexes: set = None,
                  state_dir: str = None) -> tuple:
    """Convert verdicts into George-schema signal files.

    Returns:
        (created_paths, review_items)
        created_paths: list of files written to George's pending queue.
        review_items: list of items that need human review.

    When ``state_dir`` is set, each emitted prep swap is recorded in the prep
    ledger so the next cycle can suppress a duplicate by state, not by clock.
    """
    if supported_dexes is None:
        supported_dexes = {"meteora", "raydium", "orca"}

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
        if state_dir:
            prep_ledger.record_emitted(spec, signal["signal_id"], state_dir=state_dir)
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
            # Get pool info for signal building
            pool = v.get("_pool") or {}
            strategy = strategies.get(pool_addr, {})
            signal = _build_open_signal(strategy, v, pool, idx, base, min_open_score, supported_dexes)
            if signal is None:
                review.append({
                    "pool_address": pool_addr,
                    "dex": dex,
                    "score": v.get("score"),
                    "evidence": v.get("evidence"),
                    "reason": "OPEN candidate; could not build signal (missing enrichment)",
                })
                continue
        elif action in {"CLOSE", "REBALANCE", "COLLECT_FEES", "ROTATE"}:
            lower = v.get("lower_bound")
            upper = v.get("upper_bound")
            try:
                lower = int(lower) if lower is not None else 0
                upper = int(upper) if upper is not None else 0
            except (TypeError, ValueError):
                lower, upper = 0, 0
            if action == "ROTATE":
                sig_reason = (
                    f"ROTATE score={v.get('score')}; "
                    f"target={v.get('candidate_pool_address')}; "
                    f"evidence={json.dumps(v.get('evidence'))}"
                )
            else:
                sig_reason = (
                    f"{action} score={v.get('score')}; "
                    f"evidence={json.dumps(v.get('evidence'))}"
                )
            signal = {
                "signal_id": f"sheldon-{base}-{idx}",
                "action": "claim_fees" if action == "COLLECT_FEES" else "close",
                "dex": dex,
                "wallet_id": v.get("wallet_id") or "main",
                "pool_address": pool_addr,
                "position_id": v.get("position"),
                "side": "bidirectional",
                "bin_range": {"lower": lower, "upper": upper},
                "liquidity": {"amount_x": "0", "amount_y": "0"},
                "max_slippage_bps": 100,
                "reason": sig_reason,
                "created_at": _utc_iso(),
            }
        else:
            # HOLD, REVIEW, WATCH, IGNORE => no signal.
            continue

        # Only protocols George can execute get a queued signal.
        if dex not in supported_dexes:
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


def _load_active_positions(positions_dir: str) -> dict:
    """Return dict of pool_address -> position dict for positions not closed.

    Includes 'inactive' (zero-liquidity but still open on-chain) positions:
    re-opening a pool that already holds one creates duplicate positions,
    which is exactly the bug this dedup exists to prevent.
    """
    import lp_scoring
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