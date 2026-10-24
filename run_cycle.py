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
from typing import Optional, Dict, Any

import lp_scoring
import range_state
from capital import DUST_MIN_USD, USDC_MINT
from idle_sweep import execute_sweep, load_add_state, plan_sweep
from readiness import (
    PREP_OSCILLATION_WINDOW_SECONDS,
    build_prep_swap_signal,
    build_readiness,
    gate_prep_swaps,
    load_raw_wallet,
    plan_funding,
    plan_sol_top_up,
    sol_overhead_lamports,
    prep_ledger,
)
from readiness.prep_ledger import read_journal
import dynamic
import datetime

def _get_manual_close_state(signals_dir: str) -> Dict[str, Dict[str, Any]]:
    """Return a close_state-like dict from executed close signals in signals/processed/.
    Each entry has status CLOSE_RESOLVED and confirmed_at set to the signal's created_at.
    """
    processed_dir = Path(signals_dir) / "processed"
    if not processed_dir.is_dir():
        return {}
    manual_state: Dict[str, Dict[str, Any]] = {}
    for path in processed_dir.iterdir():
        if not path.is_file() or not path.name.endswith(".json"):
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                sig = json.load(fh)
        except Exception:
            continue
        if sig.get("action") != "close":
            continue
        if sig.get("decision") != "executed":
            continue
        signal_id = sig.get("signal_id")
        if not signal_id:
            continue
        created = sig.get("created_at")
        if not created:
            continue
        try:
            # Remove trailing Z and convert
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            ts = dt.timestamp()
        except Exception:
            continue
        pool = sig.get("pool_address")
        if not pool:
            continue
        entry = {
            "status": "CLOSE_RESOLVED",
            "confirmed_at": created,
            "resolved_at": created,
            "funds_verified_at": created,
            "chain_verified_at": created,
            "pool_address": pool,
            "signal_id": signal_id,
        }
        manual_state[signal_id] = entry
    return manual_state

from strategy.bin_step_rail import allowed_bin_step_set
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
from run_cycle.rotation import (
    _release_unemitted_rotations,
    _settle_rotations,
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


def _utc_oday():
    """Return today's date in Asia/Shanghai timezone, for rotation state tracking."""
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _load_rotation_state(state_dir: str) -> dict:
    """Load rotation state from JSON file, persist across cycles."""
    path = os.path.join(state_dir, "rotation_state.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {"rotations": {}, "last_rotation_cycle": {}}


def _save_rotation_state(state_dir: str, state: dict) -> None:
    """Persist rotation state atomically."""
    path = os.path.join(state_dir, "rotation_state.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


# ----- Close settlement state -----
# Phase 2: the full state machine lives in run_cycle/close_settlement.py
# (states, journal folding, retry/review classification, settlement
# advance). run_cycle.py keeps thin aliases so existing call sites and any
# external callers of the historical helpers keep working.
from run_cycle.close_settlement import (  # noqa: E402
    CLOSE_STATE_FILENAME,
    check_settlement,
    close_cooldown_pools,
    load_close_state as _load_close_state,
    mark_emitted,
    record_emitted_closes,
    save_close_state as _save_close_state,
)




def _agent_mode() -> str:
    """Executor mode, as exported by the driver after the shared config gate.

    Deliberately reports ``"unknown"`` when unset rather than assuming
    ``dry_run``: chain-truth reconciliation must never turn itself on because
    an environment variable was forgotten.
    """
    return (os.environ.get("PIPELINE_AGENT_MODE") or "unknown").strip()


def _pending_close_positions(signals_dir: str) -> set:
    """Position ids whose close signal is still queued (in flight).

    An in-flight close must never be resolved on chain absence: the position
    may be gone because the close landed, but the queue entry still needs the
    executor's journal verdict. Returns an empty set when the directory is
    unreadable — which, combined with the mode check, keeps reconciliation
    conservative (it can only ever resolve less).
    """
    pending = set()
    try:
        names = os.listdir(signals_dir)
    except OSError:
        return pending
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(signals_dir, name),
                      encoding="utf-8") as fh:
                sig = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(sig, dict):
            continue
        if sig.get("action") not in ("close", "rebalance", "rotate"):
            continue
        pid = sig.get("position_id")
        if pid:
            pending.add(str(pid))
    return pending


def resolve_close_entry(state_dir: str, signal_id: str, note: str = "") -> int:
    """Mark one close-settlement entry CLOSE_RESOLVED (manual:true).
    Owner-approved 2026-10-10 (plan question #19): supersedes the
    edit-the-JSON-only design. Returns a process exit code.
    """
    from run_cycle.close_settlement import resolve_entry
    return resolve_entry(state_dir, signal_id, note)


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
    ap.add_argument("--resolve-close", metavar="SIGNAL_ID",
                    help="mark one close-settlement entry CLOSE_RESOLVED "
                         "(manual:true) and exit without running a cycle; "
                         "combine with --resolve-note. Owner-approved "
                         "2026-10-10 (plan question #19) — supersedes the "
                         "edit-the-JSON-only design.")
    ap.add_argument("--resolve-note", default="",
                    help="human note recorded with --resolve-close")
    args = ap.parse_args()

    if args.resolve_close:
        return resolve_close_entry(args.state_dir, args.resolve_close,
                                   args.resolve_note)

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
    report["scoring_profile_version"] = lp_scoring.scoring_profile_version()

    # Load rotation state persist across cycles
    rot_state = _load_rotation_state(args.state_dir)

    # Build capital plan and strategies from the loaded wallet scan.
    wallet = report.get("wallet") or lp_scoring._empty_wallet("wallet not loaded")
    report["capital_plan"] = capital_plan(wallet)

    active_positions = _load_active_positions(args.positions_dir)

    # Scan currency evidence, loaded once and shared by the rotation settle
    # and the close settlement gate (Phase 2): the newest position scan
    # mtime and the raw wallet scan (path, mtime, assets).
    position_path = lp_scoring.newest_file(args.positions_dir, "position_scan")
    try:
        position_mtime = os.path.getmtime(position_path) if position_path else 0.0
    except OSError:
        position_mtime = 0.0
    raw_wallet = load_raw_wallet(args.wallet_scans_dir)

    # Out-of-range side policy (Mr. Man, 2026-10-13):
    #   full-right OOR (price/bin above the range)  -> close immediately;
    #   full-left  OOR (price/bin below the range)  -> close after two
    #                                                 consecutive verified runs.
    # Unknown or malformed range data never counts and never closes. The
    # state is side-aware, so switching sides restarts the counter, and an
    # in-range run drops the record.
    oor_state = range_state.update_out_of_range_state(
        list(active_positions.values()),
        range_state.load_range_state(args.state_dir),
    )
    range_state.save_range_state(oor_state, args.state_dir)
    report["verdicts"] = [
        range_state.out_of_range_position_verdict(v, oor_state)
        for v in report.get("verdicts", [])
    ]
    report["out_of_range_grace"] = {
        "allowed_runs": range_state.ALLOWED_OUT_OF_RANGE_RUNS,
        "right_closes_immediately": True,
        "deferred_positions": sorted(
            addr for addr, rec in oor_state.items()
            if rec.get("side") == "left"
            and rec.get("consecutive_runs", 0) < range_state.ALLOWED_OUT_OF_RANGE_RUNS
        ),
        "positions": {
            addr: {
                "side": rec.get("side"),
                "consecutive_runs": rec.get("consecutive_runs"),
                "close_eligible": (
                    rec.get("side") == "right"
                    or rec.get("consecutive_runs", 0) >= range_state.ALLOWED_OUT_OF_RANGE_RUNS
                ),
            }
            for addr, rec in sorted(oor_state.items())
        },
    }

    # ----- Rotation detection ------------------------------------------------
    # After out-of-range grace, check if any position should be rotated to a
    # better candidate pool. Rotation is a two-cycle workflow:
    #   cycle N:   ROTATE verdict -> close signal for the source position;
    #              the candidate is reserved and never opened this cycle.
    #   cycle N+1: the close-settlement gate blocks opens until George's
    #              journal confirms the close; once the source position is
    #              gone from the position scan the reservation settles and
    #              the candidate re-enters the normal open flow.
    # -----------------------------------------------------------------------
    _policy = get_policy()
    rotate_margin = float(_policy.get("rotate_margin", 5.0))
    max_rotations_per_day = int(_policy.get("max_rotations_per_day", 2))
    min_hold_hours = float(_policy.get("min_hold_hours", 12.0))

    # Settle finished rotations: a pending entry whose source position no
    # longer appears in the position scan is settled (close landed on-chain).
    # Entries older than 7 days expire, so a lost close can never wedge the
    # reservation forever.
    now_ts = time.time()
    rotations = rot_state.setdefault("rotations", {})
    close_resolutions = read_journal(prep_ledger.DEFAULT_JOURNAL_DIR)
    rotation_review = _settle_rotations(
        rotations, active_positions, close_resolutions, now_ts=now_ts,
        position_mtime=position_mtime,
        wallet_mtime=raw_wallet.get("mtime", 0.0),
        rotation_policy=(_POLICY.get("rotation") or {}),
        max_position_scan_age=float(
            (_POLICY.get("readiness") or {}).get(
                "max_position_scan_age_seconds", 900.0)),
    )
    pending_targets = {
        e.get("candidate_pool") for e in rotations.values()
        if isinstance(e, dict)
        and e.get("status") in ("proposed", "awaiting_close")
        and e.get("candidate_pool")
    }

    report["rotations"] = rotations
    report["rotation_review"] = rotation_review

    # Count rotations today from state (settled ones still count toward the cap)
    rotations_today = len([
        n for n in rotations.values()
        if isinstance(n, dict) and n.get("cycle_day") == _utc_oday()
    ])

    # Build position -> pool mapping from verdicts
    pos_by_addr = {}
    for v in report.get("verdicts", []):
        addr = v.get("pool_address")
        if addr:
            pos_by_addr[addr] = v

    # Collect OPEN_CANDIDATE pools as rotation candidates
    open_candidates = [v for v in report.get("verdicts", [])
                       if v.get("action") == "OPEN_CANDIDATE"]
    # Index candidates by pool_address
    cand_by_addr = {v.get("pool_address"): v for v in open_candidates}

    # Rotate detection: for each position with verdict in {REVIEW, CLOSE, REBALANCE},
    # check if rotating to a candidate pool improves expected PnL.
    rotation_actions = []  # list of (position_index, candidate, pnl_result)
    pool_scores_by_addr = {
        ps.get("pool_address"): ps for ps in report.get("pool_scores", [])
        if ps.get("pool_address")
    }
    for i, v in enumerate(report.get("verdicts", [])):
        action = v.get("action")
        if action not in ("REVIEW", "CLOSE", "REBALANCE"):
            continue
        addr = v.get("pool_address")
        if not addr or addr not in pos_by_addr:
            continue
        pos_v = pos_by_addr[addr]
        # Get position data from the position scan
        # (the report's position_scores have the detailed data)
        pos_score = pos_v.get("score", 0.0)
        # Find best candidate: candidate score must be >= pos_score + rotate_margin
        best_cand = None
        best_cand_score = -1e9
        for cand_v in open_candidates:
            cand_addr = cand_v.get("pool_address")
            if cand_addr == addr:
                # Skip self (would be rebalancing, not rotating)
                continue
            cand_score = cand_v.get("score", 0.0)
            if cand_score >= pos_score + rotate_margin and cand_score > best_cand_score:
                best_cand = cand_v
                best_cand_score = cand_score
        if best_cand is None:
            continue

        # Pre-screen rotation candidate through policy gates so the reservation
        # does not block valid opens and the rotation is actually viable.
        cand_addr = best_cand.get("pool_address")
        cand_pool = best_cand.get("_pool") or {}
        cand_score = best_cand.get("score", 0.0)
        src_pool = pos_v.get("_pool") or {}
        # Check candidate against policy gates (same checks as _filter_open_candidates)
        cand_checks = {}
        # Liquidity
        cand_checks["liquidity"] = float(cand_pool.get("tvl", 0) or 0) >= _POLICY.get("min_pool_liquidity_usd", 25000.0)
        # Volume
        cand_checks["volume"] = True  # will check below if data available
        # Bin step (Meteora only)
        cand_bin_step = float(cand_pool.get("bin_step") or 0)
        cand_checks["bin_step"] = cand_bin_step in _POLICY.get("allowed_bin_steps", set())
        # TVL/volume ratio
        cand_tvl = float(cand_pool.get("tvl") or 0)
        cand_volume = float(cand_pool.get("volume_window") or 0)
        cand_turnover_ratio = (cand_volume / cand_tvl) if cand_tvl > 0 else 0
        cand_checks["turnover_ratio"] = cand_turnover_ratio <= _POLICY.get("max_turnover_ratio", 50.0)
        cand_checks["max_volatility"] = float(cand_pool.get("volatility") or 0) <= _POLICY.get("max_volatility_pct", 50)
        # If any gate fails, skip this rotation candidate
        if not all(cand_checks.values()):
            # Log which gate failed for diagnostics
            failed = [k for k, v in cand_checks.items() if not v]
            pos_v["reason"] = (
                f"rotation blocked: candidate failed policy gate(s): {', '.join(failed)}"
            )
            continue

        # Get position data from report position_scores
        pos_data = None
        for ps in report.get("position_scores", []):
            if ps.get("pool_address") == addr:
                pos_data = ps
                break
        if pos_data is None:
            continue

        # Minimum hold period (policy): never rotate a position younger than
        # min_hold_hours. Unknown age fails safe (no rotation).
        days_open = pos_data.get("days_open")
        if days_open is None:
            continue
        try:
            hours_open = float(days_open) * 24.0
        except (TypeError, ValueError):
            continue
        if hours_open < min_hold_hours:
            continue

        # Per-pool cooldown: at most one rotation per source pool per day,
        # plus a global daily cap across all pools.
        last_cycle = (rot_state.get("last_rotation_cycle") or {}).get(addr) or {}
        if last_cycle.get("day") == _utc_oday():
            continue
        if rotations_today >= max_rotations_per_day:
            continue

        # Pool facts for the PnL comparison: the CURRENT position's pool
        # drives hold economics; the candidate drives ROTATE economics.
        # Prefer the verdict's embedded pool record, fall back to the
        # scored pool entry.
        src_pool = pos_v.get("_pool") or pool_scores_by_addr.get(addr) or {}
        cand_pool = best_cand.get("_pool") or pool_scores_by_addr.get(
            best_cand.get("pool_address")) or {}

        pnl = dynamic.expected_pnl_verdict(
            pos_data, src_pool, lp_scoring.get_config(),
            candidate_pool=cand_pool,
        )
        if pnl is None or not pnl.get("decisive"):
            continue
        if pnl["action"] != "ROTATE":
            continue

        rotation_actions.append((i, best_cand, pnl))

    # Apply rotation actions: emit close for the source, reserve the target.
    for pos_idx, cand, pnl in rotation_actions:
        pos_v = report["verdicts"][pos_idx]
        pos_addr = pos_v.get("pool_address")
        cand_addr = cand.get("pool_address")
        today = _utc_oday()

        # Record rotation in state (rotations is rot_state["rotations"])
        rotations[pos_addr] = {
            "candidate_pool": cand_addr,
            "cycle_day": today,
            "status": "awaiting_close",
        }
        rot_state.setdefault("last_rotation_cycle", {})[pos_addr] = {
            "day": today,
            "candidate_pool": cand_addr,
        }
        pending_targets.add(cand_addr)
        _save_rotation_state(args.state_dir, rot_state)

        # The verdict becomes ROTATE; write_signals() maps it to a close
        # signal for the source position. The candidate is NOT opened this
        # cycle: it is marked reserved here and blocked by the gate, and it
        # re-enters the open flow only after the close settles.
        pos_v["action"] = "ROTATE"
        pos_v["reason"] = (
            f"ROTATE: close {pos_v.get('pool')} ({pos_addr}) -> target "
            f"{cand.get('pool')} ({cand_addr}) "
            f"(PNL: HOLD={pnl['expected_hold_usd']}, "
            f"ROTATE={pnl['expected_rotate_usd']}, "
            f"margin={pnl['margin_usd']})"
        )
        pos_v["candidate_pool_address"] = cand_addr
        pos_v["candidate_pool"] = cand.get("pool")
        pos_v["candidate_score"] = cand.get("score")

        if cand.get("action") == "OPEN_CANDIDATE":
            cand["action"] = "RESERVED_ROTATION_TARGET"
            cand["reason"] = (
                f"reserved as rotation target of {pos_addr}; "
                f"opens after the source close settles"
            )

    report["verdicts"] = [
        range_state.out_of_range_position_verdict(v, oor_state)
        for v in report.get("verdicts", [])
    ]

    # ----- Close settlement gate (light pre-check) -----
    # Check that no position has an unresolved close before any open
    # candidates are considered. This prevents opening against closes
    # that never landed, or spending proceeds not yet in the wallet.
    close_state = _load_close_state(args.state_dir)
    light_block_reasons = []
    for v in report.get("verdicts", []):
        if v.get("action") in ("CLOSE", "REBALANCE"):
            addr = v.get("pool_address")
            if not addr:
                continue
            for sid, sstatus in close_state.items():
                if sstatus.get("status") == "CLOSE_EMITTED":
                    light_block_reasons.append(
                        f"pool {addr}: close still EMITTED (signal {sid})"
                    )
                    break
    if light_block_reasons:
        report["notes"] = report.get("notes", []) + [
            f"Close settlement light check: {'; '.join(light_block_reasons)}"
        ]

    # Full close settlement gate runs after funding (wallet_mtime available).
    # We defer the heavy gate to the funding section below.

    open_candidates = [v for v in report.get("verdicts", [])
                       if v.get("action") == "OPEN_CANDIDATE"]
    # Augment close_state with manually executed close signals for cooldown.
    manual_state = _get_manual_close_state(args.signals_dir)
    if manual_state:
        close_state.update(manual_state)
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
        allowed_bin_steps=_POLICY.get("allowed_bin_steps", allowed_bin_step_set()),
        reserved_pools=pending_targets,
        cooldown_pools=close_cooldown_pools(
            close_state, now=now_ts,
            cooldown_seconds=_POLICY.get("reopen_cooldown_seconds", 2700.0)),
    )
    report["open_skipped"] = open_skipped
    report["strategies"] = build_strategies(kept_candidates, wallet)

    # Capital readiness: annotate strategies with their pool enrichment,
    # diff target token needs against the newest raw wallet scan, and gate
    # prep swaps (rescan-wait + hourly loop guard) before any signal write.
    pool_meta = {v.get("pool_address"): (v.get("_pool") or {}) for v in kept_candidates}
    for s in report["strategies"]:
        s.setdefault("_pool", pool_meta.get(s.get("pool_address")) or {})
    # Anti-oscillation: a mint bought for an open within the last window must
    # not be round-tripped back to USDC as surplus until that open lands.
    prep_ledger_state = prep_ledger.load_and_resolve(
        args.state_dir, prep_ledger.DEFAULT_JOURNAL_DIR,
    )
    recent_buy_mints = prep_ledger.recent_buy_mints(
        prep_ledger_state, now=time.time(), window=PREP_OSCILLATION_WINDOW_SECONDS,
    )

    dust_mints = {a.get("mint") for a in (wallet.get("dust_assets") or []) if a.get("mint")}
    # Keep the complete non-position SOL overhead unavailable to the funding
    # walk. This replaces the old flat 0.02 SOL spend cap; readiness then
    # checks the candidate-specific SOL requirement below.
    funding_sol_reserve = sol_overhead_lamports(
        report["strategies"][0].get("dex") if report["strategies"] else None,
        sol_policy=(_POLICY.get("sol") or {}),
    )
    funding = plan_funding(
        report["strategies"], raw_wallet["assets"],
        wallet_path=raw_wallet["path"], wallet_mtime=raw_wallet["mtime"],
        dust_mints=dust_mints,
        recent_buy_mints=recent_buy_mints,
        sol_reserve_lamports=funding_sol_reserve,
    )
    if raw_wallet.get("error"):
        funding["notes"].append(f"wallet scan unavailable: {raw_wallet['error']}")
    allowed_preps, blocked_preps = gate_prep_swaps(
        funding["prep_swaps"], args.signals_dir, funding["wallet_mtime"],
        state_dir=args.state_dir,
        journal_dir=prep_ledger.DEFAULT_JOURNAL_DIR,
        ledger=prep_ledger_state,
    )
    funding["prep_swaps_allowed"] = allowed_preps
    funding["prep_swaps_blocked"] = blocked_preps
    report["funding"] = funding
    # ----- Close settlement gate (full, after funding) -----
    # Phase 2 state machine: a close is settled only on FUNDS_VERIFIED or
    # CLOSE_RESOLVED — journal-executed, source absent from a position scan
    # newer than the close confirmation and within scan-age policy, and a
    # wallet scan newer than the confirmation. Anything else blocks opens.
    # This verdict never gates closes/claims — only opens.
    close_state = _load_close_state(args.state_dir)
    wallet_mtime = raw_wallet.get("mtime", 0.0)
    rotation_policy = (_POLICY.get("rotation") or {})
    max_position_scan_age = float(
        (_POLICY.get("readiness") or {}).get(
            "max_position_scan_age_seconds", 900.0))
    close_ttl = float(_POLICY.get("close_state_ttl_seconds", 3600.0))
    allowed, reason, updated_state, close_details = check_settlement(
        active_positions, wallet_mtime, position_mtime, close_state,
        now=time.time(), policy=rotation_policy,
        max_scan_age_seconds=max_position_scan_age,
        close_ttl_seconds=close_ttl,
        mode=_agent_mode(),
        chain_rpc_url=os.environ.get("SHELDON_CHAIN_RPC_URL") or None,
        pending_position_ids=_pending_close_positions(args.signals_dir),
    )
    # Propagate updated close state
    _save_close_state(args.state_dir, updated_state)
    report["close_settlement"] = {
        "allowed": allowed, "reason": reason, **close_details,
    }

    # ----- Structured pre-open readiness (Phase 1) ------------------------
    # Every strategy gets one additive readiness object.  The object is built
    # after the close gate so its close_settled check is from this cycle, and
    # before signals are written so an OPEN can never bypass it.
    readiness_by_pool = {}
    funding_entries = {
        e.get("pool_address"): e
        for e in (funding.get("funded") or []) + (funding.get("unfunded") or [])
        if e.get("pool_address")
    }
    readiness_policy = (_POLICY.get("readiness") or {})
    sol_policy = (_POLICY.get("sol") or {})
    for strategy in report.get("strategies") or []:
        pool_addr = strategy.get("pool_address")
        readiness = build_readiness(
            strategy,
            assets=raw_wallet.get("assets") or [],
            wallet_mtime=raw_wallet.get("mtime", 0.0),
            position_mtime=position_mtime,
            now=time.time(),
            funding_entry=funding_entries.get(pool_addr),
            close_settled=allowed,
            close_reason=reason,
            exposure_ready=bool(report.get("exposure_ready", True)),
            readiness_policy=readiness_policy,
            sol_policy=sol_policy,
        )
        readiness_by_pool[pool_addr] = readiness

        # A top-up is actionable only for this specific candidate and only
        # when every non-SOL readiness check already passes. The open remains
        # blocked in this cycle; after the swap, the next fresh scan reruns the
        # complete gate from the beginning.
        if not readiness["ready"]:
            top_up = plan_sol_top_up(
                readiness,
                assets=raw_wallet.get("assets") or [],
                required_usdc_raw=(
                    (funding_entries.get(pool_addr) or {}).get("required", {})
                    .get("y_raw", 0)
                    if ((funding_entries.get(pool_addr) or {}).get("required", {})
                        .get("y_mint") == USDC_MINT)
                    else 0
                ),
                sol_policy=sol_policy,
            )
            if top_up.get("allowed") and top_up.get("spec"):
                funding.setdefault("prep_swaps_allowed", []).append(top_up["spec"])
                readiness.setdefault("blocking_reasons", []).append(
                    "SOL top-up planned; fresh wallet scan required before open"
                )
                readiness["top_up"] = top_up["spec"]

    report["readiness"] = readiness_by_pool

    # Enforce the gate: no open strategy survives without a passing
    # readiness object. Blocked candidates become review items so the report
    # explains why a high-scoring pool produced no open signal.
    ready_strategies = []
    for strategy in report.get("strategies") or []:
        r = readiness_by_pool.get(strategy.get("pool_address"))
        if r and r.get("ready"):
            # Graduated wallet freshness penalty: if the wallet scan is stale
            # but not completely blocked, proportionally reduce the suggested
            # position size so the open still happens with less capital at risk.
            wallet_ff = r.get("wallet_freshness_factor", 1.0)
            if wallet_ff < 1.0:
                # Reduce suggested USDC by the freshness factor
                strategy["suggested_usdc"] = max(
                    strategy.get("suggested_usdc", 0.0) * wallet_ff,
                    MIN_POSITION_USD  # never reduce below minimum
                )
            ready_strategies.append(strategy)
        else:
            reasons = "; ".join((r or {}).get("blocking_reasons") or [])
            open_skipped.append({
                "pool_address": strategy.get("pool_address"),
                "dex": strategy.get("dex"),
                "score": strategy.get("score"),
                "reason": ("readiness gate: "
                           + ((r or {}).get("status") or "no readiness object")
                           + (f" ({reasons})" if reasons else "")),
            })
    # Capital allocation across ready strategies.
    # build_strategies() sizes every candidate as if it were the only open
    # (75% of deployable), so two ready candidates each claim the full budget
    # and the second open cannot be funded. Split the deployable stablecoin
    # budget across the candidates instead: fund as many opens as the minimum
    # allows, best score first, share the budget evenly, and never exceed the
    # per-strategy cap. A strategy that the split cannot fund is a review item,
    # not a silent overspend.
    if ready_strategies:
        budget = float((report.get("capital_plan") or {}).get("deployable_usdc") or 0.0)
        if budget <= 0:
            budget = float((report.get("wallet") or {}).get("deployable_usdc") or 0.0)
        funded = sorted(ready_strategies,
                        key=lambda s: s.get("score") or 0.0, reverse=True)
        opens_allowed = int(budget // MIN_POSITION_USD) if budget > 0 else 0
        if opens_allowed < len(funded):
            for strategy in funded[opens_allowed:]:
                open_skipped.append({
                    "pool_address": strategy.get("pool_address"),
                    "dex": strategy.get("dex"),
                    "score": strategy.get("score"),
                    "reason": (f"capital split: deployable ${budget:.2f} funds "
                               f"{opens_allowed} open(s) at min_position_usd "
                               f"${MIN_POSITION_USD:.2f}"),
                })
            funded = funded[:opens_allowed]
        if funded:
            share = budget / len(funded)
            allocated = []
            for strategy in funded:
                amount = min(float(strategy.get("suggested_usdc") or 0.0), share)
                if amount < MIN_POSITION_USD:
                    open_skipped.append({
                        "pool_address": strategy.get("pool_address"),
                        "dex": strategy.get("dex"),
                        "score": strategy.get("score"),
                        "reason": (f"capital split: share ${amount:.2f} below "
                                   f"min_position_usd ${MIN_POSITION_USD:.2f}"),
                    })
                    continue
                strategy["suggested_usdc"] = int(amount * 100) / 100  # never round up past the budget
                allocated.append(strategy)
            ready_strategies = allocated
        else:
            if budget > 0 and opens_allowed < 1:
                open_skipped.append({
                    "pool_address": None, "dex": "any", "score": None,
                    "reason": (f"capital split: deployable ${budget:.2f} below "
                               f"min_position_usd ${MIN_POSITION_USD:.2f}"),
                })
            ready_strategies = []

    report["strategies"] = ready_strategies
    report["open_skipped"] = open_skipped

    if not allowed:
        # Normal rail outcome, not a fatal error: drop every open strategy so
        # no open signal can be written, hold prep swaps, keep the
        # close/review flow alive, and surface the block reason.
        kept_candidates = []
        block_skip = {"pool_address": None, "dex": "any", "score": None,
                      "reason": f"close settlement gate: {reason}"}
        open_skipped = (report.get("open_skipped") or []) + [block_skip]
        report["open_skipped"] = open_skipped
        report["strategies"] = []
        report["readiness"] = {}
        for spec in funding.get("prep_swaps_allowed") or []:
            funding.setdefault("prep_swaps_blocked", []).append({
                **spec, "reason": "close settlement gate blocks opens",
            })
        funding["prep_swaps_allowed"] = []
        report["notes"] = report.get("notes", []) + [
            f"Close settlement gate blocked opens: {reason}"
        ]

    if report.get("failures"):
        err = "; ".join(report["failures"])
        print(f"ERROR: {err}", file=sys.stderr)
        if args.json:
            json.dump(report, sys.stdout, indent=2)
            print()
        return 2
    signals_created = []
    review = list(rotation_review)
    if args.write_signals:
        # Approved design: open signals are written FIRST, then the
        # idle-capital sweep reads the queue (pending opens/prep swaps are
        # committed capital) and sweeps only the leftover below the open
        # floor into the best tracked position. A failure here degrades to
        # the old behavior (no sweep), never blocks opens.
        signals_created, signal_review = write_signals(
            report, args.signals_dir, args.positions_dir,
            min_open_score=_POLICY.get("min_open_score", 70.0),
            min_position_usd=_POLICY.get("min_position_usd", 20.0),
            supported_dexes=SUPPORTED_DEXES,
            state_dir=args.state_dir,
            close_emission=_POLICY.get("close_emission") or {},
        )

        review.extend(signal_review)
        # Link emitted close signals back to their awaiting rotation
        # entries so the reservation tracks which close settles it.
        for path in signals_created:
            try:
                with open(path, encoding="utf-8") as fh:
                    emitted = json.load(fh)
            except (OSError, ValueError):
                continue
            entry = rotations.get(emitted.get("pool_address"))
            if (entry and entry.get("status") == "awaiting_close"
                    and emitted.get("action") == "close"):
                entry["close_signal_id"] = emitted["signal_id"]
        # Change G: record CLOSE_EMITTED the moment a close signal is
        # written, so the settlement state machine tracks it from emit time.
        # A new emit also supersedes that pool's earlier rejected attempts.
        emitted_closes = record_emitted_closes(args.state_dir, signals_created)
        if emitted_closes:
            cs = report.setdefault("close_settlement", {})
            cs["emitted"] = emitted_closes
        unemitted = _release_unemitted_rotations(
            rotations, cycle_day=_utc_oday())
        rotation_review.extend(unemitted)
        review.extend(unemitted)
        _save_rotation_state(args.state_dir, rot_state)

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
        _wake_george(signals_created, review, args.signals_dir)

    append_log(report, args.memory_dir, signals_created, review)
    # Surface signal review items (suppressed closes, skipped opens, deferred
    # preps) in the JSON report too — previously only the non-JSON summary
    # printed them, so --json consumers never saw why a verdict stayed
    # signal-less (observed 2026-10-16 Cooper run).
    if args.write_signals and review:
        report["signals_review"] = review

    # Emit a machine-readable wake hint so the calling Sheldon session can
    # notify George immediately when actionable signals were written.
    if signals_created:
        summary = ", ".join(Path(p).name for p in signals_created)
        # stderr: --json writes the report to stdout, and a banner line here
        # corrupted the sheldon_report-*.json artifact.
        print(f"WAKE_GEORGE: {len(signals_created)} signal(s) -> {summary}",
              file=sys.stderr)

    _save_rotation_state(args.state_dir, rot_state)

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


if __name__ == "__main__":
    sys.exit(main())