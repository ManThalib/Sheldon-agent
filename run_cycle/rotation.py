"""Rotation state transitions extracted from the run_cycle CLI script.

Rotation is a two-cycle workflow: cycle N emits a close for the source
position and reserves the candidate; cycle N+1 settles once the source is
gone from the position scan. This module owns the state machine so the
transitions are unit-testable without the CLI.

Additional feature: turnover-based rotation signals. Pools with
high turnover_penalty_score (unsustainable turnover >20xTVL/day) are
flagged for rotation consideration alongside the standard close workflow.
"""


import time
from datetime import datetime, timedelta, timezone

def _utc_iso():
    """Return current time in Asia/Shanghai (UTC+8) ISO format."""
    return datetime.now(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%dT%H:%M:%S+08:00")


def _settle_rotations(rotations: dict, active_positions: dict,
                      close_resolutions: dict, now_ts: float = None,
                      position_mtime: float = None,
                      wallet_mtime: float = None,
                      rotation_policy: dict = None,
                      max_position_scan_age: float = None) -> list:
    """Advance pending rotation entries and return review items.

    Transitions (only entries in ``proposed``/``awaiting_close`` move):

      * source position gone from the position scan -> ``settled``
        (George's close landed on-chain). With the Phase 2 rotation policy
        this additionally requires the absence proof to come from a current
        position scan (within ``max_position_scan_age``), and — when the
        close's journal confirmation time is known — a wallet scan newer
        than that confirmation. A stale scan or an unverified wallet keeps
        the entry ``awaiting_close``; it can never settle on stale evidence.
      * George journalled the close signal as rejected/failed/failed_verify
        -> ``failed``; the candidate reservation is released because only
        proposed/awaiting_close entries reserve a target
      * pending longer than 7 days -> ``expired`` (a lost close can never
        wedge the reservation forever)

    Note: settling the rotation entry only releases the candidate
    reservation. The candidate's open is still gated by the full close
    settlement machine (FUNDS_VERIFIED) plus the Phase 1 readiness gate —
    settling can never open anything by itself.

    Mutates ``rotations`` in place. The returned list feeds the cycle's
    review surface so a human sees a rejected rotation close.
    """
    now_ts = time.time() if now_ts is None else now_ts
    policy = rotation_policy or {}
    require_scan = bool(
        policy.get("require_chain_close_verification", True)
        or policy.get("require_position_scan_confirmation", True))
    require_wallet = policy.get("require_wallet_capital_confirmation", True)
    scan_current = True
    if require_scan:
        if not position_mtime:
            scan_current = False
        elif (max_position_scan_age is not None
              and now_ts - position_mtime > max_position_scan_age):
            scan_current = False
    review = []
    for src_addr, entry in list(rotations.items()):
        if not isinstance(entry, dict):
            rotations.pop(src_addr, None)
            continue
        if entry.get("status") not in ("proposed", "awaiting_close"):
            continue
        if src_addr not in active_positions:
            sid = entry.get("close_signal_id")
            resolution = close_resolutions.get(sid) or {}
            conf = resolution.get("confirmed_at") or 0.0
            if not scan_current:
                entry["settlement_note"] = (
                    "source absent but position scan not current; "
                    "settlement deferred")
            elif require_wallet and conf and (wallet_mtime or 0.0) <= conf:
                entry["settlement_note"] = (
                    "source absent but wallet scan predates the close "
                    "confirmation; settlement deferred")
            else:
                entry.pop("settlement_note", None)
                entry["status"] = "settled"
                entry["settled_at"] = _utc_iso()
            # Absent source: the entry either settled or deferred on stale
            # evidence — never fall through to the rejection branch. A
            # rejected close whose position is gone is the ambiguous case;
            # the close settlement machine flags it CLOSE_REVIEW_REQUIRED
            # and the global open gate blocks. The reservation itself
            # follows the position evidence, not the failure.
            continue
        sid = entry.get("close_signal_id")
        resolution = close_resolutions.get(sid) or {}
        if resolution.get("status") in ("rejected", "failed", "failed_verify"):
            entry["status"] = "failed"
            entry["failed_at"] = _utc_iso()
            entry["reason"] = (
                f"Rotation close {sid} {resolution['status']}: "
                f"{resolution.get('reason') or 'no details'}")
            review.append({"pool_address": src_addr, "reason": entry["reason"]})
            continue
        try:
            day_ts = time.mktime(
                time.strptime(entry.get("cycle_day") or "", "%Y-%m-%d"))
        except ValueError:
            day_ts = 0.0
        if day_ts and now_ts - day_ts > 7 * 24 * 3600:
            entry["status"] = "expired"
            entry["expired_at"] = _utc_iso()
    return review


def _release_unemitted_rotations(rotations: dict, cycle_day: str = None) -> list:
    """Fail this cycle's ``awaiting_close`` entries with no close signal.

    A ROTATE verdict can fail to produce a close signal (unsupported dex,
    write error). Without this, the entry would hold the candidate
    reservation until the 7-day expiry even though nothing is pending.
    Returns review items.
    """
    review = []
    for addr, entry in rotations.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "awaiting_close":
            continue
        if entry.get("close_signal_id"):
            continue
        if cycle_day is not None and entry.get("cycle_day") != cycle_day:
            continue
        entry["status"] = "failed"
        entry["failed_at"] = _utc_iso()
        entry["reason"] = (
            "Rotation close signal was never emitted; reservation released")
        review.append({"pool_address": addr, "reason": entry["reason"]})
    return review


def check_turnover_rotation_signals(pool_scores: list, threshold: float = 8.0) -> list:
    """Check pool scores for turnover-based rotation signals.

    Pools with ``turnover_penalty_score`` above ``threshold`` (default 8.0,
    i.e. near the 20×TVL/day cap) are flagged for rotation consideration.

    Returns list of dicts with pool_address, score, turnover_penalty_score,
    and reason for rotation consideration.
    """
    signals = []
    for v in pool_scores:
        components = v.get("components", {})
        tp_score = components.get("turnover_penalty_score", 0)
        if tp_score >= threshold:
            addr = v.get("pool_address")
            score = v.get("score")
            new_fc = v.get("new_fc_score")
            reason = (
                f"turnover_penalty_score={tp_score:.1f} >= {threshold} "
                f"(approaching 20xTVL/day cap); "
                f"new_fc={new_fc}"
                if new_fc else
                f"turnover_penalty_score={tp_score:.1f} >= {threshold} "
                f"(approaching 20xTVL/day cap)")
            signals.append({
                "pool_address": addr,
                "missy_score": score,
                "turnover_penalty_score": tp_score,
                "new_fc_score": new_fc,
                "reason": reason,
                "action": "consider_rotation",
            })
    return signals
