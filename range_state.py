#!/usr/bin/env python3
"""Persistent out-of-range side state and close policy.

Policy:
- full-right OOR (current price/bin above the upper bound): close immediately;
- full-left OOR (current price/bin below the lower bound): close after two
  consecutive verified runs;
- in-range or unknown/malformed data: reset/do not count.

The state file remains backward compatible with the historical
``out_of_range_counts`` mapping.
"""

import json
import os

ALLOWED_OUT_OF_RANGE_RUNS = 2
STATE_FILENAME = "out_of_range.json"

# Malformed-bound guard: a live price more than this many times outside a
# decoded bound is a scanner scale/decoding fault, not a real excursion (a
# Meteora range is at most ~70 bins wide, i.e. well under 2x). Such rows are
# treated as unknown so bad data can never trigger a close.
MAX_PRICE_SCALE_RATIO = 10.0
# A Meteora init tx can create only ~70 bins, so a wider decoded range is
# invalid geometry (matches the builder's MAX_METEORA_RANGE_WIDTH clamp).
MAX_RANGE_BINS = 200


def state_path(state_dir: str = None) -> str:
    if state_dir is None:
        state_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    return os.path.join(state_dir, STATE_FILENAME)


def _valid_number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    # Zero is a common scanner unknown sentinel for prices/bins.
    return value if value != 0 else None


def classify_oor_side(pos: dict):
    """Return ``right``, ``left``, ``inside``, or ``unknown``.

    Prefer normalized price bounds, then decoded integer/bin bounds. A bare
    ``in_range=false`` is never enough to infer a side. This prevents a
    malformed or mis-scaled scanner row from triggering a close.
    """
    lower = _valid_number(pos.get("lower_price"))
    upper = _valid_number(pos.get("upper_price"))
    current = _valid_number(pos.get("current_price"))
    if lower is not None and upper is not None and current is not None:
        if upper <= lower:
            return "unknown"
        if not _scale_plausible(lower, upper, current):
            return "unknown"
        if current > upper:
            return "right"
        if current < lower:
            return "left"
        return "inside"

    lower = _valid_number(pos.get("lower_bound"))
    upper = _valid_number(pos.get("upper_bound"))
    current = _valid_number(
        pos.get("current_bin_id") if pos.get("current_bin_id") is not None
        else pos.get("current_tick_index")
    )
    if lower is not None and upper is not None and current is not None:
        if upper <= lower or (upper - lower) > MAX_RANGE_BINS:
            return "unknown"
        if current > upper:
            return "right"
        if current < lower:
            return "left"
        return "inside"
    return "unknown"


def _scale_plausible(lower: float, upper: float, current: float) -> bool:
    """Reject bound/current sets that differ by more than a plausible factor."""
    if lower <= 0 or upper <= 0 or current <= 0:
        return False
    ratio = max(upper / current, current / lower)
    return ratio <= MAX_PRICE_SCALE_RATIO


def position_out_of_range(pos: dict) -> bool:
    return classify_oor_side(pos) in ("left", "right")


def load_range_state(state_dir: str = None) -> dict:
    """Load normalized state records.

    Historical ``{out_of_range_counts: {pool: n}}`` is converted to left-side
    records conservatively. The next verified scan replaces the side.
    """
    try:
        with open(state_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    records = data.get("positions")
    if isinstance(records, dict):
        out = {}
        for addr, rec in records.items():
            if not isinstance(rec, dict):
                continue
            try:
                n = int(rec.get("consecutive_runs", 0))
            except (TypeError, ValueError):
                continue
            if n > 0 and rec.get("side") in ("left", "right"):
                item = dict(rec)
                item["consecutive_runs"] = n
                out[str(addr)] = item
        return out
    # Compatibility with the old pool->count format. Do not treat it as a
    # right-side count: right-side immediate close must be newly verified.
    counts = data.get("out_of_range_counts")
    if not isinstance(counts, dict):
        return {}
    return {
        str(addr): {"pool_address": str(addr), "side": "left",
                    "consecutive_runs": int(n)}
        for addr, n in counts.items()
        if isinstance(n, (int, float)) and n > 0
    }


def save_range_state(state: dict, state_dir: str = None) -> None:
    try:
        path = state_path(state_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        positions = {}
        for addr, rec in (state or {}).items():
            if isinstance(rec, (int, float)) and not isinstance(rec, bool):
                # Legacy pool->count callers: treat as a left-side counter.
                rec = {"side": "left", "consecutive_runs": int(rec)}
            if isinstance(rec, dict) and rec.get("side") in ("left", "right"):
                item = dict(rec)
                item["consecutive_runs"] = int(item.get("consecutive_runs", 0))
                if item["consecutive_runs"] > 0:
                    positions[str(addr)] = item
        payload = {"version": 2, "positions": positions}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def update_out_of_range_state(positions: list, previous: dict) -> dict:
    """Update side-aware counters from verified raw position records."""
    updated = {}
    for pos in positions or []:
        pool = pos.get("pool_address")
        if not pool:
            continue
        side = classify_oor_side(pos)
        if side not in ("left", "right"):
            continue
        key = str(pool)
        old = (previous or {}).get(key) or {}
        old_side = old.get("side") if isinstance(old, dict) else "left"
        runs = int(old.get("consecutive_runs", 0)) if old_side == side else 0
        updated[key] = {
            "pool_address": key,
            "position_address": pos.get("position_address") or pos.get("position_id"),
            "side": side,
            "consecutive_runs": runs + 1,
        }
    return updated


def update_out_of_range_counts(positions: list, counts: dict) -> dict:
    """Legacy pool->count API retained for callers/tests."""
    previous = {}
    for addr, n in (counts or {}).items():
        if isinstance(n, dict):
            previous[str(addr)] = n
        else:
            previous[str(addr)] = {"side": "left", "consecutive_runs": int(n)}
    state = update_out_of_range_state(positions, previous)
    return {addr: rec["consecutive_runs"] for addr, rec in state.items()}


def _record_for(addr, state):
    rec = (state or {}).get(addr)
    if isinstance(rec, dict):
        return rec
    if isinstance(rec, (int, float)):
        return {"side": "left", "consecutive_runs": int(rec)}
    return None


def out_of_range_position_verdict(s: dict, state: dict,
                                  allowed: int = ALLOWED_OUT_OF_RANGE_RUNS) -> dict:
    """Apply side-aware OOR close policy to one cycle verdict.

    Full-right OOR promotes any non-close verdict (HOLD/REVIEW/COLLECT_FEES)
    to CLOSE immediately. Full-left OOR defers a close-equivalent verdict
    (CLOSE/REBALANCE/ROTATE) to HOLD until ``allowed`` consecutive verified
    runs. Untracked pools (in range or unknown data) pass through untouched.
    """
    close_actions = ("CLOSE", "REBALANCE", "ROTATE")
    addr = str(s.get("pool_address") or "")
    rec = _record_for(addr, state)
    if not rec:
        return s
    side = rec.get("side")
    runs = int(rec.get("consecutive_runs", 0))
    out = dict(s)
    key = "verdict" if "verdict" in s else "action"
    current = s.get(key)
    eligible = side == "right" or runs >= allowed
    if not eligible:
        out[key] = "HOLD"
        out["reason"] = (
            f"full-left out-of-range grace: {runs}/{allowed} consecutive "
            f"verified runs; close deferred"
        )
        out["oor_side"] = side
        out["oor_consecutive_runs"] = runs
        out["oor_close_eligible"] = False
        return out
    # This is an explicit OOR exit rail. Do not preserve REBALANCE here:
    # the requested action is to close the position, not close-and-reopen it.
    out[key] = "CLOSE"
    out["reason"] = ("full-right out-of-range: immediate close"
                     if side == "right"
                     else f"full-left out-of-range: {runs} consecutive runs; close")
    out["oor_side"] = side
    out["oor_consecutive_runs"] = runs
    out["oor_close_eligible"] = True
    return out
