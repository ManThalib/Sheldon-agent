"""Range module — volatility-adaptive range width.

Re-exports symbols from ``_7d_volatility_based.py`` so they can be imported
via ``from strategy.range import ...``.
"""

from strategy.range._7d_volatility_based import (
    adaptive_half_width,
    _range_center,
    meteora_bin_step_allowed,
    _max_half_width_for_dex,
)

__all__ = [
    "adaptive_half_width",
    "_range_center",
    "meteora_bin_step_allowed",
    "_max_half_width_for_dex",
]