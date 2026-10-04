"""Strategy package — modular split of the original strategy.py.

Re-exports key symbols for backward compatibility with run_cycle.py.
Each sub-module has a clear responsibility:
  - sheldon_policy.py — policy loading and constants
  - position_sizing.py — position sizing
  - range — volatility-adaptive range width (_7d_volatility_based.py)
  - candidate_filtering.py — open candidate filtering and strategy building
  - capital_plan.py — capital plan
"""

from strategy.sheldon_policy import (
    MIN_POSITION_USD,
    DEFAULT_MAX_POSITION_USD,
    MAX_POSITION_OPEN_PER_CYCLE,
    ALLOWED_METEORA_BIN_STEPS,
    SCORING_SOURCE,
    SCORING_VERSION,
    MIN_OPEN_SCORE,
    get_policy,
    scoring_policy,
)
from strategy.position_sizing import suggested_position_usd, open_eligible
from strategy.range import (
    adaptive_half_width,
    _range_center,
    meteora_bin_step_allowed,
    _max_half_width_for_dex,
)
from strategy.candidate_filtering import build_strategies
from strategy.capital_plan import capital_plan

__all__ = [
    "MIN_POSITION_USD",
    "DEFAULT_MAX_POSITION_USD",
    "MAX_POSITION_OPEN_PER_CYCLE",
    "ALLOWED_METEORA_BIN_STEPS",
    "SCORING_SOURCE",
    "SCORING_VERSION",
    "MIN_OPEN_SCORE",
    "get_policy",
    "scoring_policy",
    "suggested_position_usd",
    "open_eligible",
    "adaptive_half_width",
    "_range_center",
    "meteora_bin_step_allowed",
    "_max_half_width_for_dex",
    "build_strategies",
    "capital_plan",
]