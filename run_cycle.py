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
from typing import Optional

import lp_scoring
import range_state
from capital import DUST_MIN_USD
from idle_sweep import execute_sweep, load_add_state, plan_sweep
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
    scoring_policy,
)

from run_cycle.gates import _in_trading_window, _filter_open_candidates, _range_center
from run_cycle.signals import write_signals
from run_cycle.report import append_log, _short_summary, _wake_george

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

    # Versioned scoring identity: which policy produced this cycle's verdicts.
    report["scoring_policy"] = scoring_policy()

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
        open_candidates, active_positions,
        policy_windows={
            "open_window_utc": _POLICY.get("open_window_utc"),
            "close_window_utc": _POLICY.get("close_window_utc"),
            "blackout_dates": _POLICY.get("blackout_dates"),
        },
        min_score=_POLICY.get("min_open_score", 70.0),
        min_liquidity=_POLICY.get("min_pool_liquidity_usd", 25000.0),
        min_volume=_POLICY.get("min_24h_volume_usd", 5000.0),
        allowed_bin_steps=_POLICY.get("allowed_bin_steps", {4, 10, 20, 25, 50, 100}),
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
        # Approved design: open signals are written FIRST, then the
        # idle-capital sweep reads the queue (pending opens/prep swaps are
        # committed capital) and sweeps only the leftover below the open
        # floor into the best tracked position. A failure here degrades to
        # the old behavior (no sweep), never blocks opens.
        signals_created, review = write_signals(
            report, args.signals_dir, args.positions_dir,
            min_open_score=_POLICY.get("min_open_score", 70.0),
            min_position_usd=_POLICY.get("min_position_usd", 20.0),
            supported_dexes=SUPPORTED_DEXES,
        )

        add_state = load_add_state(args.state_dir)
        sweep_signal, sweep_info = plan_sweep(
            report, args.signals_dir, report_runtime_mtime(report), add_state,
            state_dir=args.state_dir,
        )
        report["idle_sweep"] = sweep_info
        if sweep_signal is not None:
            ok, err = execute_sweep(
                sweep_signal, args.signals_dir, add_state,
                state_dir=args.state_dir,
            )
            if ok:
                sweep_path = os.path.join(
                    args.signals_dir, f"{sweep_signal['signal_id']}.json")
                signals_created.append(sweep_path)
                report["idle_sweep"]["decision"] = "emitted"
            else:
                report["idle_sweep"]["decision"] = "write_failed"
                report["idle_sweep"]["reason"] = err
                review.append({
                    "pool_address": sweep_signal.get("pool_address"),
                    "dex": sweep_signal.get("dex"),
                    "score": sweep_signal.get("score"),
                    "evidence": None,
                    "reason": f"idle sweep write failed: {err}",
                })
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


def report_runtime_mtime(report: dict) -> float:
    """Wallet scan mtime as reported by readiness.load_raw_wallet."""
    funding = report.get("funding") or {}
    return float(funding.get("wallet_mtime") or 0.0)


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

    # Versioned scoring identity: which policy produced this cycle's verdicts.
    report["scoring_policy"] = scoring_policy()

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
        open_candidates, active_positions,
        policy_windows={
            "open_window_utc": _POLICY.get("open_window_utc"),
            "close_window_utc": _POLICY.get("close_window_utc"),
            "blackout_dates": _POLICY.get("blackout_dates"),
        },
        min_score=_POLICY.get("min_open_score", 70.0),
        min_liquidity=_POLICY.get("min_pool_liquidity_usd", 25000.0),
        min_volume=_POLICY.get("min_24h_volume_usd", 5000.0),
        allowed_bin_steps=_POLICY.get("allowed_bin_steps", {4, 10, 20, 25, 50, 100}),
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
        # Approved design: open signals are written FIRST, then the
        # idle-capital sweep reads the queue (pending opens/prep swaps are
        # committed capital) and sweeps only the leftover below the open
        # floor into the best tracked position. A failure here degrades to
        # the old behavior (no sweep), never blocks opens.
        signals_created, review = write_signals(
            report, args.signals_dir, args.positions_dir,
            min_open_score=_POLICY.get("min_open_score", 70.0),
            min_position_usd=_POLICY.get("min_position_usd", 20.0),
            supported_dexes=SUPPORTED_DEXES,
        )

        add_state = load_add_state(args.state_dir)
        sweep_signal, sweep_info = plan_sweep(
            report, args.signals_dir, report_runtime_mtime(report), add_state,
            state_dir=args.state_dir,
        )
        report["idle_sweep"] = sweep_info
        if sweep_signal is not None:
            ok, err = execute_sweep(
                sweep_signal, args.signals_dir, add_state,
                state_dir=args.state_dir,
            )
            if ok:
                sweep_path = os.path.join(
                    args.signals_dir, f"{sweep_signal['signal_id']}.json")
                signals_created.append(sweep_path)
                report["idle_sweep"]["decision"] = "emitted"
            else:
                report["idle_sweep"]["decision"] = "write_failed"
                report["idle_sweep"]["reason"] = err
                review.append({
                    "pool_address": sweep_signal.get("pool_address"),
                    "dex": sweep_signal.get("dex"),
                    "score": sweep_signal.get("score"),
                    "evidence": None,
                    "reason": f"idle sweep write failed: {err}",
                })
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