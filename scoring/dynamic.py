"""Dynamic calibration layer for Sheldon's LP scoring engine.

See parent package dynamic.py for full implementation.
"""


from bisect import bisect_left
from typing import List, Any


def percentile_rank(sorted_values: List[Any], value: Any) -> float:
    """Return the percentile rank of *value* in *sorted_values*.

    Values must be numeric and ``sorted_values`` should be sorted
    ascending.  Returns a float in [0, 1].
    """
    if not sorted_values:
        return 0.5
    if value <= sorted_values[0]:
        return 0.0
    if value >= sorted_values[-1]:
        return 1.0
    lo = bisect_left(sorted_values, value)
    hi = lo - 1
    if lo >= len(sorted_values):
        return 1.0
    # interpolate between lo-1 and lo
    lo_val = sorted_values[lo - 1] if lo > 0 else sorted_values[0]
    hi_val = sorted_values[lo]
    if hi_val == lo_val:
        return 0.5
    return (value - lo_val) / (hi_val - lo_val)


def build_context(*args, **kwargs):
    """Placeholder for dynamic context building.

    Callers should import from the top-level ``dynamic`` module for full
    functionality.
    """
    return None