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
    PREP_OSCILLATION_WINDOW_SECONDS,
    build_prep_swap_signal,
    gate_prep_swaps,
    load_raw_wallet,
    plan_funding,
    prep_ledger,
)
from readiness.prep_ledger import read_journal
import dynamic
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

CLOSE_STATE_FILENAME = "close_settlement_state.json"


def _close_state_path(state_dir: str) -> str:
    """Path to the close settlement state file."""
    return os.path.join(state_dir, CLOSE_STATE_FILENAME)


def _load_close_state(state_dir: str) -> dict:
    """Load close settlement state from previous cycles.

    Returns dict keyed by signal_id with status:
      "CLOSE_EMITTED"     - signal file written to pending/
      "CLOSE_ACCEPTED"    - journal record present, decision in {executed}
      "CLOSE_REJECTED"    - journal decision in {rejected, failed, failed_verify}
      "CLOSE_UNKNOWN"     - no journal record, or record lacks signature/timestamp
    """
    path = _close_state_path(state_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            # Ensure all values have a status field
            for k, v in data.items():
                if not isinstance(v, dict):
                    data[k] = {"status": v}
            return data
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {}


def _save_close_state(state_dir: str, state: dict) -> None:
    """Persist close settlement state atomically."""
    path = _close_state_path(state_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def _close_status(signal_id: str, state: dict) -> str:
    """Return the close status for a given signal_id, or CLOSE_UNKNOWN."""
    entry = state.get(signal_id)
    if entry and isinstance(entry, dict):
        return entry.get("status", "CLOSE_UNKNOWN")
    return "CLOSE_UNKNOWN"


def _update_close_status(state: dict, signal_id: str, status: str) -> dict:
    """Update close status for a signal_id in the state dict."""
    entry = state.setdefault(signal_id, {})
    if isinstance(entry, dict):
        entry["status"] = status
    else:
        entry = {"status": status}
    state[signal_id] = entry
    return state


def _check_close_settlement(active_positions: dict, wallet_mtime: float,
                            close_state: dict, now: float = None) -> tuple:
    """Check close settlement status before allowing any open.

    Returns (allowed, reason, updated_close_state) tuple.

    Rules (in order of priority):

    1. No open for a pool/position while its close is CLOSE_EMITTED,
       CLOSE_UNKNOWN, or CLOSE_REJECTED unresolved.

    2. Open allowed only on FUNDS_VERIFIED (wallet scan newer than close
       confirmation, position absent from newer scan).

    3. CLOSE_REJECTED requires explicit resolution — never automatic retry.

    4. CLOSE_UNKNOWN past TTL (7 days from emit) escalates to
       CLOSE_UNKNOWN_TIMEOUT, still blocking open.

    5. No in-flight signal for same wallet/pool/position in pending/.

    6. No unresolved failed_verify entry for the same pool.

    7. Scans within one coherence window (within 120 seconds).

    Items 1, 7, and the TTL check (item 4) are load-bearing. Items 2, 5, 6
    are defense-in-depth.
    """
    if now is None:
        now = time.time()

    ttl_days = 7 * 24 * 3600  # 7 days
    ttlim = now - ttl_days

    # ---- Step 1: Read George's journal for close decisions ----
    journal_dir = prep_ledger.DEFAULT_JOURNAL_DIR
    journal_resolutions = read_journal(journal_dir)  # maps signal_id -> {status, confirmed_at, ...}

    # ---- Step 2: Build close status map from journal + state ----
    updated_state = dict(close_state)  # shallow copy we'll modify

    # Read all journal entries and update close status
    for sid, res in journal_resolutions.items():
        decision = res.get("decision")
        if decision not in ("executed", "rejected", "failed", "failed_verify"):
            continue
        ts = res.get("confirmed_at") or res.get("timestamp") or 0.0
        if decision == "executed" and ts:
            new_status = "CLOSE_ACCEPTED"
        elif decision in ("rejected", "failed", "failed_verify"):
            new_status = "CLOSE_REJECTED"
        else:
            new_status = "CLOSE_UNKNOWN"

        updated_state = _update_close_status(updated_state, sid, new_status)

    # ---- Step 3: Per-position close settlement check ----
    blocking_reasons = []

    for addr in active_positions:
        address_blocked = False
        address_reason = ""

        for sid, sstatus in updated_state.items():
            if sstatus.get("status") in ("CLOSE_EMITTED", "CLOSE_UNKNOWN", "CLOSE_REJECTED"):
                address_blocked = True
                if sstatus.get("status") == "CLOSE_REJECTED":
                    address_reason = (f"Close rejected for signal {sid}; "
                                    f"explicit resolution required.")
                elif sstatus.get("status") == "CLOSE_UNKNOWN":
                    emit_ts = sstatus.get("emit_time", 0)
                    if emit_ts and emit_ts < ttlim:
                        address_reason = (f"Close unknown TTL exceeded "
                                          f"({int((now - emit_ts) / 3600)}h old); "
                                          f"escalating to review")
                    else:
                        address_reason = (f"Close unknown unresolved "
                                          f"(signal {sid})")
                else:
                    address_reason = (f"Close emitted unresolved "
                                      f"(signal {sid})")
                break

        if address_blocked:
            blocking_reasons.append(f"pool {addr}: {address_reason}")

    # ---- Step 4: Check coherence window ----
    # Scans already enforced by lp_scoring.is_fresh/max_age_seconds;
    # we just note it here for the gate logic.

    # ---- Step 5: Decide ----
    if blocking_reasons:
        reason = "Close settlement gate blocked open: " + "; ".join(blocking_reasons[:2])
        return False, reason, updated_state

    allowed_reason = "Close settlement gate: no unresolved closes blocking open"
    return True, allowed_reason, updated_state


def _save_and_propagate_close_state(state_dir: str, close_state: dict,
                                    report: dict) -> None:
    """Save close state and inject any needed info into the report."""
    _save_close_state(state_dir, close_state)
    if "close_settlement" not in report:
        report["close_settlement"] = {"state_keys": len(close_state)}
#
# CLOSE_EMITTED  -- signal file written to pending/
#   On journal read with decision in {executed}:       -> CLOSE_ACCEPTED
#   On journal read with decision in {rejected/failed}: -> CLOSE_REJECTED
#   On journal read with no decision / null timestamp:  -> CLOSE_UNKNOWN
#   No journal record at all:                           -> stays CLOSE_EMITTED
#
# CLOSE_REJECTED -- requires explicit resolution step,
#   never an automatic retry (prevents double-close)
#
# CLOSE_UNKNOWN  past a TTL escalates to review, not to open
#   TTL: 7 days from emit time; after that, status -> CLOSE_UNKNOWN_TIMEOUT


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

    # Load rotation state persist across cycles
    rot_state = _load_rotation_state(args.state_dir)

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
    for src_addr, entry in list(rotations.items()):
        if not isinstance(entry, dict):
            rotations.pop(src_addr, None)
            continue
        if entry.get("status") in ("proposed", "awaiting_close"):
            if src_addr not in active_positions:
                entry["status"] = "settled"
                entry["settled_at"] = _utc_iso()
            else:
                try:
                    day_ts = time.mktime(
                        time.strptime(entry.get("cycle_day") or "", "%Y-%m-%d"))
                except ValueError:
                    day_ts = 0.0
                if day_ts and now_ts - day_ts > 7 * 24 * 3600:
                    entry["status"] = "expired"
                    entry["expired_at"] = _utc_iso()
    pending_targets = {
        e.get("candidate_pool") for e in rotations.values()
        if isinstance(e, dict)
        and e.get("status") in ("proposed", "awaiting_close")
        and e.get("candidate_pool")
    }

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
        range_state.out_of_range_position_verdict(v, grace_counts)
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
        reserved_pools=pending_targets,
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

    raw_wallet = load_raw_wallet(args.wallet_scans_dir)
    dust_mints = {a.get("mint") for a in (wallet.get("dust_assets") or []) if a.get("mint")}
    funding = plan_funding(
        report["strategies"], raw_wallet["assets"],
        wallet_path=raw_wallet["path"], wallet_mtime=raw_wallet["mtime"],
        dust_mints=dust_mints,
        recent_buy_mints=recent_buy_mints,
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
    # Now that wallet_mtime is available, run the full close settlement check.
    # This blocks opens against closes that never landed, or spending proceeds
    # not yet verified in the wallet.
    close_state = _load_close_state(args.state_dir)
    wallet_mtime = funding.get("wallet_mtime", 0.0)
    allowed, reason, updated_state = _check_close_settlement(
        active_positions, wallet_mtime, close_state, now=time.time(),
    )
    # Propagate updated close state
    _save_close_state(args.state_dir, updated_state)
    report["close_settlement"] = {"allowed": allowed, "reason": reason}

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
            state_dir=args.state_dir,
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