"""Sheldon LP Scoring Engine — modular package.

Split from the original lp_scoring.py into focused modules:
  - config: configuration loading and validation
  - universe: stablecoin/high-cap policy
  - helpers: general utilities
  - pool: pool LP opportunity scoring
  - position: position health scoring
  - policy: verdict logic and thresholds
"""

from scoring.config import load_config, set_config, get_config, DEFAULT_CONFIG, ConfigError
from scoring.universe import STABLECOINS, HIGH_CAPS, SUPPORTED_DEXES, classify_pair, is_off_universe
from scoring.helpers import (
    newest_file, load_wallet_scan, load_json, clamp, is_fresh,
    _empty_wallet, pair_symbols,
)

# Note: individual scoring functions are in the submodules:
#   scoring.pool.score, scoring.position.score
# Import them on demand to avoid circular imports at package level.

__all__ = [
    "load_config", "set_config", "get_config", "DEFAULT_CONFIG", "ConfigError",
    "STABLECOINS", "HIGH_CAPS", "SUPPORTED_DEXES",
    "classify_pair", "is_off_universe",
    "newest_file", "load_wallet_scan", "load_json", "clamp", "is_fresh",
    "_empty_wallet", "pair_symbols",
]