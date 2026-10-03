#!/usr/bin/env python3
"""Out-of-range grace state for open LP positions.

Policy (Mr. Man, 2026-09-29): a position that leaves its range must survive
TWO consecutive automation runs before Sheldon emits a close. The first
out-of-range run downgrades the CLOSE/REBALANCE verdict to HOLD with a
review reason (no close signal); the second consecutive out-of-range run
lets the verdict pass. Returning in range resets the counter.

The counter is keyed by pool address (Sheldon enforces one open position
per pool, so pool and position address are 1:1) and kept in a tiny JSON
state file under scoring/state/. It is derived from the raw Missy position
records (lower_price/upper_price/current_price, or Missy's in_range flag)
— not from scored components, so it does not depend on scoring weights.

Missing or malformed state fails safe to "no runs counted yet": the worst
case is one extra grace run, never an early close.
"""

import json
import os

# Grace policy: consecutive out-of-range runs that must be observed before
# a close passes. 2 means: run 1 holds, run 2 closes.
ALLOWED_OUT_OF_RANGE_RUNS = 2

STATE_FILENAME = "out_of_range.json"


def state_path(state_dir: str = None) -> str:
    if state_dir is None:
        state_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    return os.path.join(state_dir, STATE_FILENAME)


def load_range_state(state_dir: str = None) -> dict:
    """Load out-of-range counters: {pool_address: consecutive_runs}. Missing,
    malformed, or junk entries yield an empty dict."""
    try:
        with open(state_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    counts = data.get("out_of_range_counts") if isinstance(data, dict) else None
    if not isinstance(counts, dict):
        return {}
    return {
        str(addr): int(n) for addr, n in counts.items()
        if isinstance(n, (int, float)) and n > 0
    }


def save_range_state(counts: dict, state_dir: str = None) -> None:
    """Persist counters atomically. Failures are swallowed: an unwritable
    state file must not fail the scoring cycle."""
    try:
        path = state_path(state_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "out_of_range_counts": {str(k): int(v) for k, v in (counts or {}).items()},
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def position_out_of_range(pos: dict) -> bool:
    """True when the raw position record is verifiably out of range.

    Uses explicit price bounds when present; falls back to Missy's
    in_range flag. Unknown bounds and flag count as in-range (never punish
    a position on absent data — missing data caps verdicts at REVIEW
    elsewhere anyway).
    """
    lower = pos.get("lower_price")
    upper = pos.get("upper_price")
    current = pos.get("current_price")
    if lower is not None and upper is not None and current is not None:
        try:
            lower = float(lower)
            upper = float(upper)
            current = float(current)
        except (TypeError, ValueError):
            return False
        if upper > lower:
            return not (lower <= current <= upper)
        return False  # degenerate bounds: treat as unknown => in-range
    if pos.get("in_range") is False and (
        pos.get("lower_bound") is None or pos.get("upper_bound") is None
    ):
        # Fallback/historical record with undecodable tick bounds: not
        # verifiably out of range, so never start the grace counter on it.
        return False
    return pos.get("in_range") is False


def update_out_of_range_counts(positions: list, counts: dict) -> dict:
    """Update counters from this run's raw position records.

    Out-of-range positions increment (first observation starts at 1);
    in-range positions are removed. Returns a new dict containing only
    currently out-of-range positions.
    """
    updated = dict(counts or {})
    tracked = set()
    for pos in positions or []:
        addr = pos.get("pool_address")
        if not addr:
            continue
        if position_out_of_range(pos):
            updated[str(addr)] = int(updated.get(str(addr), 0)) + 1
            tracked.add(str(addr))
        else:
            updated.pop(str(addr), None)
    # Drop counters for positions that vanished from the scan (closed or
    # unscanned): no live record, nothing to keep grace for.
    for addr in list(updated.keys()):
        if addr not in tracked:
            updated.pop(addr, None)
    return updated


def out_of_range_position_verdict(s: dict, counts: dict,
                                  allowed: int = ALLOWED_OUT_OF_RANGE_RUNS) -> dict:
    """Apply the grace policy to one verdict dict.

    CLOSE/REBALANCE on a position with 1..allowed-1 consecutive
    out-of-range runs is downgraded to HOLD with a review reason. Verdicts
    without a tracked counter (in range, unknown data, closed positions,
    non-position verdicts) pass through untouched.
    """
    verdict = s.get("verdict") if "verdict" in s else s.get("action")
    if verdict not in ("CLOSE", "REBALANCE"):
        return s
    addr = str(s.get("pool_address") or "")
    seen = int((counts or {}).get(addr, 0))
    if seen <= 0 or seen >= allowed:
        return s
    out = dict(s)
    if "verdict" in s:
        out["verdict"] = "HOLD"
    else:
        out["action"] = "HOLD"
    reason = (
        f"out-of-range grace: {seen}/{allowed} consecutive out-of-range runs; "
        f"{verdict} deferred to the next run (evidence: {s.get('reason')})"
    )
    out["reason"] = reason
    if "note" in s:
        out["note"] = reason
    return out
