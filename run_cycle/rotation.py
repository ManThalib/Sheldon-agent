"""Rotation state transitions extracted from the run_cycle CLI script.

Rotation is a two-cycle workflow: cycle N emits a close for the source
position and reserves the candidate; cycle N+1 settles once the source is
gone from the position scan. This module owns the state machine so the
transitions are unit-testable without the CLI.
"""

import time
from datetime import datetime, timedelta, timezone


def _utc_iso():
    """Return current time in Asia/Shanghai (UTC+8) ISO format."""
    return datetime.now(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%dT%H:%M:%S+08:00")


def _settle_rotations(rotations: dict, active_positions: dict,
                      close_resolutions: dict, now_ts: float = None) -> list:
    """Advance pending rotation entries and return review items.

    Transitions (only entries in ``proposed``/``awaiting_close`` move):

      * source position gone from the position scan -> ``settled``
        (George's close landed on-chain)
      * George journalled the close signal as rejected/failed/failed_verify
        -> ``failed``; the candidate reservation is released because only
        proposed/awaiting_close entries reserve a target
      * pending longer than 7 days -> ``expired`` (a lost close can never
        wedge the reservation forever)

    Mutates ``rotations`` in place. The returned list feeds the cycle's
    review surface so a human sees a rejected rotation close.
    """
    now_ts = time.time() if now_ts is None else now_ts
    review = []
    for src_addr, entry in list(rotations.items()):
        if not isinstance(entry, dict):
            rotations.pop(src_addr, None)
            continue
        if entry.get("status") not in ("proposed", "awaiting_close"):
            continue
        if src_addr not in active_positions:
            entry["status"] = "settled"
            entry["settled_at"] = _utc_iso()
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
