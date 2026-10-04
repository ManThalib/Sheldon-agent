from typing import Tuple
import os
import json
import time
from datetime import datetime, timedelta, timezone

from .state_manager import save_add_state


def execute_sweep(signal: dict, signals_dir: str, state: dict,
                  state_dir: str = None, now: float = None) -> Tuple[bool, str]:
    """Write the sweep signal and its reservation as one atomic step."""
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