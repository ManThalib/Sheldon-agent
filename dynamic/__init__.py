"""Sheldon dynamic calibration module — modular package.

Split from the original dynamic.py into focused sub-modules:
  - regime: market regime classification (regime detection + weight adjustment)
  - thresholds: adaptive pool OPEN/WATCH cut-offs from quantiles
  - calibration: context assembly, norms building, expected-PnL verdicts

All original ``dynamic.X`` names are re-exported at the top level so that
``import dynamic`` (or ``from dynamic import ...``) continues to work
identically to the original monolith.
"""

from .helpers import percentile_rank, build_norms, RAW_METRICS, OFF_UNIVERSE, _min_open_score
from .regime import detect_regime, regime_weight_adjust
from .thresholds import adaptive_pool_thresholds
from .calibration import (
    build_context,
    build_context_from_prior,
    _load_prior_scans,
    expected_pnl_verdict,
)

# Backward-compatible re-exports (mirror original dynamic.py API)
detect_regime_func = detect_regime
regime_weight_adjust_func = regime_weight_adjust
adaptive_pool_thresholds_func = adaptive_pool_thresholds
build_context_func = build_context
build_context_from_prior_func = build_context_from_prior
_load_prior_scans_func = _load_prior_scans
expected_pnl_verdict_func = expected_pnl_verdict

# Expose key constants for CLI / test access
DEFAULT_DATA_DIR = "/data/missy-data"

# Helper aliases (moved from dynamic.py top-level)
# These are now imported from .helpers above, but also re-exported here
# for direct access as dynamic.percentile_rank etc.
_helpers_percentile_rank = percentile_rank
_helpers_build_norms = build_norms

__all__ = [
    "detect_regime",
    "regime_weight_adjust",
    "adaptive_pool_thresholds",
    "build_context",
    "build_context_from_prior",
    "_load_prior_scans",
    "expected_pnl_verdict",
    "percentile_rank",
    "build_norms",
    "RAW_METRICS",
    "OFF_UNIVERSE",
    "DEFAULT_DATA_DIR",
    "detect_regime_func",
    "regime_weight_adjust_func",
    "adaptive_pool_thresholds_func",
    "build_context_func",
    "build_context_from_prior_func",
    "_load_prior_scans_func",
    "expected_pnl_verdict_func",
]

# Backward compatibility: keep original top-level names accessible as
# dynamic.<name> so existing import dynamic; dynamic.<name> still works.
_original_dynamic_exports = {
    "percentile_rank": percentile_rank,
    "build_norms": build_norms,
    "RAW_METRICS": RAW_METRICS,
    "OFF_UNIVERSE": OFF_UNIVERSE,
    "_min_open_score": _min_open_score,
}

for name, val in _original_dynamic_exports.items():
    globals()[name] = val