#!/usr/bin/env python3
"""Idle-capital sweep: small idle USDC goes into the best tracked position.

Owner-approved design (2026-10-01): when the wallet holds idle USDC too
small to open a new position (< add_policy.idle_max_usd, default $20) but
worth a transaction (>= add_policy.min_add_usd, default $5), emit a George
``add_liquidity`` signal that sweeps it into the best-scoring tracked open
position. George (commit 523b79f) enforces the hard rails on its side:
tracked value + add <= max_position_usd, total exposure, daily loss,
drawdown, live-tick containment, on-chain bin_range == position bounds.

Producer-side rails enforced HERE (fail-safe: any missing data skips the
phase, never emits a guess):
  - wallet scan freshness (add_policy.max_wallet_scan_age_seconds)
  - idle window: min_add_usd <= idle_usd < idle_max_usd, net of everything
    already committed (pending open signals, pending prep swaps, this
    module's own reservations)
  - Y side is USDC (one-sided adds deepen the quote side; an X-side add
    would need a swap, which is out of scope)
  - headroom: max_position_usd - tracked_value >= the add
  - eligibility floors: pool passes bin_step rail and TVL/volume gates
    when present in this cycle's pool scan (absent pool => scan skipped it;
    last known-good pool stays eligible)
  - cooldown per pool, day cap across pools (state/add_state.json)
  - y-side room: >= y_side_room_pct of the range above the active bin
    (a USDC-only add lands above the active bin and needs room to spread)
  - reservation: pending_add_usd persisted atomically with the signal so
    the next cycle cannot double-count the same idle capital; released when
    George's queues show the signal done (processed/failed_verify/pending
    all count as committed) or when the reservation is stale
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import lp_scoring
from capital import USDC_MINT
from strategy import (
    ALLOWED_METEORA_BIN_STEPS,
    DEFAULT_MAX_POSITION_USD,
    get_policy,
    meteora_bin_step_allowed,
)

STATE_FILENAME = "add_state.json"

# George's signal queues (relative to his agent dir).
GEORGE_AGENT_DIR = "/data/.openclaw/workspace-agents/george/agents/meteora-dlmm"
SIGNAL_SUBDIRS = ("pending", "processed", "failed_verify")

# Floor score for sweep candidates whose pool did not appear in this
# cycle's pool scan: one tick below min_open_score, so an open-eligible
# pool always outranks a sweep target (the open path owns capital first;
# the sweep only spends the leftover). Applied uniformly to every
# candidate, so ranking stays deterministic and fair.
_FLOOR_SCORE_EPSILON = 0.01

# A reservation older than this is presumed dead (crashed cycle, lost
# signal file) and released; the wallet rescan is the backstop.
RESERVATION_MAX_AGE_SECONDS = 3600.0


def state_path(state_dir: str = None) -> str:
    if state_dir is None:
        state_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    return os.path.join(state_dir, STATE_FILENAME)


def load_add_state(state_dir: str = None) -> dict:
    """Load sweep state. Missing or malformed fails safe to empty state."""
    try:
        with open(state_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_add_state(state: dict, state_dir: str = None) -> bool:
    """Persist sweep state atomically (tmp + rename, like range_state).

    Returns True on success; a failure must abort the sweep (the caller
    must not emit a signal it could not reserve against).
    """
    try:
        path = state_path(state_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _created_epoch(created_at: str) -> float:
    try:
        dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError, TypeError):
        return 0.0


# --------------------------------------------------------------------------
# Committed capital: what this wallet's idle USDC is already promised to
# --------------------------------------------------------------------------
def _signal_usdc_committed(sig: dict) -> float:
    """USDC amount a queued Sheldon signal will consume, 0 if unknown."""
    action = sig.get("action")
    if action == "open":
        return float(sig.get("position_usd") or 0.0)
    if action == "add_liquidity":
        return float(sig.get("position_usd") or 0.0)
    if action == "swap":
        if sig.get("input_mint") != USDC_MINT:
            return 0.0
        try:
            return int(sig.get("amount") or 0) / 10**6
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def scan_committed_usdc(signals_dir: str, now: Optional[float] = None) -> Tuple[float, int]:
    """Sum USDC committed by Sheldon signals still in George's queues.

    Covers open (position_usd), swap-to-buy (amount when input is USDC),
    and this module's own add_liquidity signals. Any presence in
    pending/processed/failed_verify counts: processed means executed but
    possibly not yet reflected in a wallet rescan; failed_verify means the
    tx may still have landed (verification failed, not execution).

    Unreadable files are skipped. Signal age is not a filter here: George
    moves files out of pending on processing, and his own
    signal_max_age_seconds rail cleans stragglers. Signals older than 24h
    are ignored (a stale file George will never execute must not freeze
    capital forever; the wallet rescan is the real backstop).
    """
    now = now if now is not None else time.time()
    parent = os.path.dirname(signals_dir.rstrip("/"))
    roots = {signals_dir, os.path.join(parent, "pending"),
             os.path.join(parent, "processed"), os.path.join(parent, "failed_verify")}
    total = 0.0
    count = 0
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".json") or not name.startswith("sheldon-"):
                continue
            try:
                with open(os.path.join(root, name), "r", encoding="utf-8") as fh:
                    sig = json.load(fh)
            except (OSError, ValueError):
                continue
            created = _created_epoch(sig.get("created_at") or "")
            if created and now - created > 86400:
                continue
            amount = _signal_usdc_committed(sig)
            if amount > 0:
                total += amount
                count += 1
    return total, count


def release_confirmed_reservation(state: dict, signals_dir: str) -> dict:
    """Release the pending reservation once George's queues prove its fate.

    A reservation whose signal_id is found in any queue is confirmed
    committed (executed, failed, or still waiting) and is kept as
    ``last_add`` bookkeeping, but the pending hold is only cleared when the
    file is gone from pending (i.e. George finished with it) or the
    reservation is stale. Returns the (possibly) updated state dict.
    """
    res = (state or {}).get("pending_reservation")
    if not isinstance(res, dict) or not res.get("signal_id"):
        return state
    # Pure: mutate a copy so callers can compare released vs original and
    # persist only real changes.
    out = dict(state)
    state = out
    sid = res["signal_id"]
    parent = os.path.dirname(signals_dir.rstrip("/"))
    found = {}
    for sub in SIGNAL_SUBDIRS:
        path = os.path.join(parent, sub, f"{sid}.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                found[sub] = json.load(fh)
        except (OSError, ValueError):
            continue
    if not found:
        # Signal file gone entirely: executed long ago (George prunes
        # processed) or never written due to a crash. Age out either way.
        age = time.time() - float(res.get("reserved_at") or 0)
        if age > RESERVATION_MAX_AGE_SECONDS:
            state["pending_reservation"] = None
            state.setdefault("notes", []).append(
                f"reservation {sid} aged out without a trace after {age:.0f}s")
        return state

    if "pending" in found:
        # Still queued. Keep the hold, but do not hold forever.
        age = time.time() - float(res.get("reserved_at") or 0)
        if age > RESERVATION_MAX_AGE_SECONDS:
            state["pending_reservation"] = None
            state.setdefault("notes", []).append(
                f"reservation {sid} released: pending >{RESERVATION_MAX_AGE_SECONDS:.0f}s")
        return state

    # Moved out of pending: George finished with it (executed => wallet
    # rescan will reflect it; failed_verify/rejected => capital is back).
    state["last_add"] = {
        "signal_id": sid,
        "pool_address": res.get("pool_address"),
        "position_id": res.get("position_id"),
        "amount_usd": res.get("amount_usd"),
        "reserved_at": res.get("reserved_at"),
        "final_state": "processed" if "processed" in found else "failed_verify",
    }
    state["pending_reservation"] = None
    return state


# --------------------------------------------------------------------------
# Candidate selection
# --------------------------------------------------------------------------
def _y_side_room_ok(lower: int, upper: int, active_bin: int,
                    room_pct: float) -> Tuple[bool, str]:
    """True when >= room_pct of the range sits above the active bin.

    A one-sided USDC (quote/Y) add distributes above the active bin, so
    the upper half must have room for the add to actually land. When the
    active bin is unknown (0/None), fail closed: do not sweep blind.
    """
    if active_bin is None or int(active_bin) == 0:
        return False, f"active bin unknown ({active_bin})"
    active_bin = int(active_bin)
    if not (lower <= active_bin < upper):
        return False, f"active bin {active_bin} outside range [{lower},{upper})"
    total = upper - lower
    room = upper - active_bin
    share = 100.0 * room / total
    if share + 1e-9 < room_pct:
        return False, f"y-side room {share:.1f}% < policy {room_pct}%"
    return True, ""


def _pool_gate(pool: Optional[dict], dex: str) -> Tuple[bool, str]:
    """Eligibility floors for the target pool, using this cycle's scan.

    Fail-open on absent enrichment (pool not screened this cycle) ONLY for
    the data-dependent checks George does not re-enforce: the bin_step rail
    itself fails closed when the pool IS present but malformed.
    """
    if dex not in ("meteora", "raydium", "orca"):
        return False, f"dex {dex} unsupported by George"
    if dex == "meteora" and not ALLOWED_METEORA_BIN_STEPS:
        return False, "allowed_bin_steps rail missing (fail closed)"
    if pool is None:
        return True, ""
    ok, reason = meteora_bin_step_allowed(pool, dex)
    if not ok:
        return False, reason
    return True, ""


def select_add_candidate(tracked: List[dict], pool_scores: List[dict],
                         policy: dict) -> Tuple[Optional[dict], List[dict]]:
    """Pick the best add target among tracked open positions.

    tracked: HOLD/REBALANCE verdict rows (position bounds from the position
    scan; George re-verifies against the chain, so a stale range is a
    rejected signal, never a loss).

    Returns (best, skipped): best is a candidate dict with pool_address,
    position_id, dex, bin_range, active_bin, tracked_value, headroom,
    score, y_mint_is_usdc — or None when nothing qualifies.
    """
    min_score = float(policy.get("min_open_score", 70.0))
    floor_score = round(min_score - _FLOOR_SCORE_EPSILON, 2)
    scores = {s.get("pool_address"): float(s.get("score") or 0.0)
              for s in pool_scores or []}
    max_pos = float(policy.get("default_max_position_usd",
                               DEFAULT_MAX_POSITION_USD))
    room_pct = float(policy.get("add_y_side_room_pct", 25.0))

    skipped: List[dict] = []
    candidates = []
    for t in tracked or []:
        addr = t.get("pool_address")
        dex = t.get("dex") or "unknown"
        base = {"pool_address": addr, "dex": dex, "score": scores.get(addr),
                "position_id": t.get("position")}
        pool = t.get("_pool") or None
        lower, upper = t.get("lower_bound"), t.get("upper_bound")
        try:
            lower, upper = int(lower), int(upper)
        except (TypeError, ValueError):
            skipped.append({**base, "reason": "position bounds unknown"})
            continue
        if upper <= lower:
            skipped.append({**base, "reason": "degenerate bin_range"})
            continue

        y_mint = str(((pool or {}).get("token_y_address")) or "").strip()
        y_is_usdc = (y_mint == USDC_MINT) if y_mint else False
        if not y_is_usdc:
            skipped.append({**base,
                            "reason": (f"Y side is not USDC "
                                       f"(mint {y_mint or 'unknown'}); "
                                       f"one-sided USDC add unsafe")})
            continue

        ok, reason = _pool_gate(pool, dex)
        if not ok:
            skipped.append({**base, "reason": reason})
            continue

        active_bin = (pool or {}).get("active_bin_id")
        ok, reason = _y_side_room_ok(lower, upper, active_bin, room_pct)
        if not ok:
            skipped.append({**base, "reason": reason})
            continue

        tracked_value = float(t.get("tracked_value_usd") or 0.0)
        headroom = max_pos - tracked_value
        if headroom < float(policy.get("add_min_usd", 5.0)):
            skipped.append({**base,
                            "reason": (f"headroom ${headroom:.2f} < "
                                       f"min_add_usd ${policy.get('add_min_usd', 5.0):.2f}")})
            continue

        # Score: live pool score when screened this cycle, else the floor
        # just under min_open_score (last-known-good pool, loses to opens).
        candidates.append({
            **base,
            "bin_range": {"lower": lower, "upper": upper},
            "active_bin": int(active_bin),
            "tracked_value_usd": tracked_value,
            "headroom_usd": headroom,
            "score": scores.get(addr, floor_score),
        })

    if not candidates:
        return None, skipped
    best = max(candidates, key=lambda c: (c["score"], -c["tracked_value_usd"]))
    return best, skipped


# --------------------------------------------------------------------------
# Sweep planning and signal build
# --------------------------------------------------------------------------
def plan_sweep(report: dict, signals_dir: str, wallet_mtime: float,
               state: dict, state_dir: str = None,
               now: Optional[float] = None) -> Tuple[Optional[dict], dict]:
    """Decide whether this cycle should emit an idle-capital sweep.

    Returns (signal, info). signal is a George-schema add_liquidity dict
    or None; info explains the decision for the report/log. NEVER writes
    files: the caller writes signal + reservation atomically via
    execute_sweep().
    """
    now = now if now is not None else time.time()
    policy = get_policy()
    info: Dict[str, Any] = {"phase": "idle_sweep", "decision": "skip", "reason": ""}

    if not policy.get("add_enabled"):
        info["reason"] = "add_policy.enabled is false"
        return None, info

    # Reservation bookkeeping first: release/keep, then read the hold.
    # Persist the release immediately: a confirmed-finished reservation
    # must not linger in add_state.json when no new signal is emitted.
    released = release_confirmed_reservation(state, signals_dir)
    if released != state:
        if not save_add_state(released, state_dir):
            info["reason"] = "could not persist reservation release; sweep skipped"
            return None, info
    state = released
    res = state.get("pending_reservation")
    reserved_usd = float(res.get("amount_usd") or 0.0) if res else 0.0
    info["reserved_usd"] = reserved_usd

    # Staleness guard: sweep judges REAL wallet capital; a stale scan lies.
    max_age = float(policy.get("add_max_wallet_scan_age_seconds", 900.0))
    scan_age = (now - wallet_mtime) if wallet_mtime else None
    if scan_age is None or scan_age > max_age:
        info["reason"] = (f"wallet scan stale (age "
                          f"{'unknown' if scan_age is None else f'{scan_age:.0f}s'} "
                          f"> {max_age:.0f}s)")
        return None, info

    # Cooldown and day cap from recorded sweep history.
    cooldown_h = float(policy.get("add_cooldown_hours", 6.0))
    max_per_day = int(policy.get("add_max_per_day", 6))
    last = state.get("last_add") or {}
    last_epoch = float(last.get("reserved_at") or 0)
    if last_epoch and now - last_epoch < cooldown_h * 3600:
        info["reason"] = (f"cooldown: last add {last.get('signal_id')} "
                          f"{(now - last_epoch) / 3600:.1f}h ago < {cooldown_h}h")
        return None, info
    day_key = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    day_counts = state.get("day_counts") or {}
    adds_today = int(day_counts.get(day_key) or 0)
    if adds_today >= max_per_day:
        info["reason"] = f"day cap: {adds_today} adds already on {day_key}"
        return None, info

    # Idle math (approved design): wallet USDC minus every queued Sheldon
    # signal that consumes USDC — opens, prep-swap buys, prior sweeps —
    # plus this module's own reservation. The sweep runs after open
    # signals are written, so the queue itself carries them.
    wallet = report.get("wallet") or {}
    idle_raw = float(wallet.get("idle_usdc") or 0.0)
    committed, committed_n = scan_committed_usdc(signals_dir, now=now)
    idle_usd = idle_raw - committed - reserved_usd
    info.update({
        "wallet_idle_usdc": round(idle_raw, 2),
        "queued_committed_usdc": round(committed, 2),
        "queued_committed_signals": committed_n,
        "reserved_usd": round(reserved_usd, 2),
        "idle_net_usd": round(idle_usd, 2),
    })

    idle_max = float(policy.get("add_idle_max_usd", 20.0))
    min_add = float(policy.get("add_min_usd", 5.0))
    if idle_usd >= idle_max:
        info["reason"] = (f"idle ${idle_usd:.2f} >= idle_max_usd ${idle_max:.2f}; "
                          f"normal open path owns the capital")
        return None, info
    if idle_usd < min_add:
        info["reason"] = (f"idle ${idle_usd:.2f} < min_add_usd ${min_add:.2f}; "
                          f"not worth a tx")
        return None, info

    # Target selection among tracked open positions (HOLD/REBALANCE rows).
    tracked = [v for v in report.get("verdicts") or []
               if v.get("action") in ("HOLD", "REBALANCE")]
    best, skipped = select_add_candidate(tracked, report.get("pool_scores") or [],
                                         policy)
    info["skipped"] = skipped
    if best is None:
        info["reason"] = "no qualifying add target (see skipped)"
        return None, info

    add_amount = round(min(idle_usd, float(best["headroom_usd"])), 2)
    if add_amount < min_add:
        info["reason"] = (f"add ${add_amount:.2f} shrank below min_add_usd "
                          f"after headroom cap")
        return None, info

    # Raw USDC amount (6 decimals); one-sided add: X stays zero.
    amount_y_raw = str(int(round(add_amount * (10**6))))
    base = int(now)
    signal = {
        "signal_id": f"sheldon-sweep-{base}",
        "action": "add_liquidity",
        "dex": best["dex"],
        "pool_address": best["pool_address"],
        "position_id": best["position_id"],
        "side": "quote_only",
        "bin_range": best["bin_range"],
        "liquidity": {"amount_x": "0", "amount_y": amount_y_raw},
        "position_usd": add_amount,
        "score": best["score"],
        "max_slippage_bps": int(policy.get("default_max_slippage_bps", 100)),
        "reason": (
            f"IDLE SWEEP ${add_amount:.2f} into tracked position "
            f"(score={best['score']}, tracked=${best['tracked_value_usd']:.2f}, "
            f"headroom=${best['headroom_usd']:.2f}, active_bin={best['active_bin']})"
        ),
        "created_at": datetime.fromtimestamp(
            now, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }
    info["decision"] = "emit"
    info["signal_id"] = signal["signal_id"]
    info["target"] = {k: best[k] for k in
                      ("pool_address", "position_id", "dex", "bin_range",
                       "active_bin", "tracked_value_usd", "headroom_usd", "score")}
    info["add_amount_usd"] = add_amount
    return signal, info


def execute_sweep(signal: dict, signals_dir: str, state: dict,
                  state_dir: str = None, now: Optional[float] = None) -> Tuple[bool, str]:
    """Write the sweep signal and its reservation as one atomic step.

    Order matters: reserve first (state save must succeed), then write the
    signal. If the signal write fails after the reservation landed, the
    reservation ages out via RESERVATION_MAX_AGE_SECONDS — idle capital is
    frozen briefly, never double-spent.

    Returns (ok, error).
    """
    now = now if now is not None else time.time()
    day_key = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    updated = dict(state or {})
    updated["pending_reservation"] = {
        "signal_id": signal["signal_id"],
        "pool_address": signal["pool_address"],
        "position_id": signal.get("position_id"),
        "amount_usd": float(signal["position_usd"]),
        "reserved_at": now,
    }
    counts = dict(updated.get("day_counts") or {})
    counts[day_key] = int(counts.get(day_key) or 0) + 1
    # Keep only the last 3 UTC days of counters (state hygiene).
    keep = sorted(counts.keys())[-3:]
    updated["day_counts"] = {k: counts[k] for k in keep}
    if not save_add_state(updated, state_dir):
        return False, "could not persist add_state.json; sweep aborted"
    try:
        os.makedirs(signals_dir, exist_ok=True)
        path = os.path.join(signals_dir, f"{signal['signal_id']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(signal, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        return False, f"signal write failed after reservation: {exc}"
    return True, ""
