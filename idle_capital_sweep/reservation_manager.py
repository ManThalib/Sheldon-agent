import os
import time
import json

RESERVATION_MAX_AGE_SECONDS = 3600.0
SIGNAL_SUBDIRS = ("pending", "processed", "failed_verify")


def release_confirmed_reservation(state: dict, signals_dir: str) -> dict:
    """Release the pending reservation once George's queues prove its fate."""
    res = (state or {}).get("pending_reservation")
    if not isinstance(res, dict) or not res.get("signal_id"):
        return state
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
        age = time.time() - float(res.get("reserved_at") or 0)
        if age > RESERVATION_MAX_AGE_SECONDS:
            state["pending_reservation"] = None
            state.setdefault("notes", []).append(
                f"reservation {sid} aged out without a trace after {age:.0f}s")
        return state

    if "pending" in found:
        age = time.time() - float(res.get("reserved_at") or 0)
        if age > RESERVATION_MAX_AGE_SECONDS:
            state["pending_reservation"] = None
            state.setdefault("notes", []).append(
                f"reservation {sid} released: pending >{RESERVATION_MAX_AGE_SECONDS:.0f}s")
        return state

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