"""Policy loading and constants for the strategy module.

Loads sheldon_policy.json and george's execution_limits.json.
All policy values are single-sourced from sheldon_policy.json.
Mechanical limits are loaded from George's execution_limits.json
so strategy and executor rails stay in sync.
"""

import json
import os
from typing import Any, Dict, Set


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELDON_DIR = os.path.dirname(os.path.abspath(__file__))
_GEORGE_DIR = "/data/.openclaw/workspace-agents/george/agents/meteora-dlmm"
_SHELDON_POLICY_PATH = os.path.join(_PROJECT_ROOT, "sheldon_policy.json")
_GEORGE_LIMITS_PATH = os.path.join(_GEORGE_DIR, "execution_limits.json")


def _load_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _load_sheldon_policy() -> Dict[str, Any]:
    data = _load_json(_SHELDON_POLICY_PATH)
    policy: Dict[str, Any] = {}
    sizing = data.get("position_sizing") or {}
    policy["min_position_usd"] = float(sizing.get("min_position_usd", 20.0))
    policy["default_max_position_usd"] = float(sizing.get("default_max_position_usd", 100.0))
    policy["max_opens_per_cycle"] = int(sizing.get("max_opens_per_cycle", 20))

    scoring = data.get("scoring") or {}
    policy["scoring_source"] = str(scoring.get("source", "local")).lower()
    policy["scoring_version"] = int(scoring.get("version") or 1)
    policy["min_open_score"] = float(scoring.get("min_open_score") or 70.0)

    pool = data.get("pool_eligibility") or {}
    policy["allowed_bin_steps"] = set(pool.get("allowed_bin_steps", [10, 20, 25, 50, 100]))
    # Deprecated: the following gates are now owned by Missy. Kept only as
    # fallback when scanning older Missy outputs that lack `eligible`.
    policy["min_pool_liquidity_usd"] = float(pool.get("min_pool_liquidity_usd", 25000.0))
    policy["min_24h_volume_usd"] = float(pool.get("min_24h_volume_usd", 5000.0))

    windows = data.get("windows") or {}
    policy["open_window_utc"] = windows.get("open_window_utc", "00:00-23:59")
    policy["close_window_utc"] = windows.get("close_window_utc", "00:00-23:59")
    policy["blackout_dates"] = list(windows.get("blackout_dates", []))

    intent = data.get("execution_intent") or {}
    policy["default_max_slippage_bps"] = int(intent.get("default_max_slippage_bps", 100))

    add = data.get("add_policy") or {}
    policy["add_enabled"] = bool(add.get("enabled", False))
    policy["add_idle_max_usd"] = float(add.get("idle_max_usd", 20.0))
    policy["add_min_usd"] = float(add.get("min_add_usd", 5.0))
    policy["add_cooldown_hours"] = float(add.get("cooldown_hours", 6.0))
    policy["add_max_per_day"] = int(add.get("max_adds_per_day", 6))
    policy["add_y_side_room_pct"] = float(add.get("y_side_room_pct", 25.0))
    policy["add_max_wallet_scan_age_seconds"] = float(
        add.get("max_wallet_scan_age_seconds", 900.0))

    return policy


def _load_execution_limits() -> Dict[str, Any]:
    data = _load_json(_GEORGE_LIMITS_PATH)
    limits: Dict[str, Any] = {}
    for group in ("position_sizing", "exposure_and_loss", "circuit_breakers",
                  "execution_guards", "range_limits"):
        limits.update(data.get(group) or {})
    return limits


_POLICY = _load_sheldon_policy()
_LIMITS = _load_execution_limits()


# --------------------------------------------------------------------------
# Mirrors of George's SAFETY_RAILS.md — loaded from policy JSON
# --------------------------------------------------------------------------
MIN_POSITION_USD: float = _POLICY["min_position_usd"]
DEFAULT_MAX_POSITION_USD: float = _POLICY["default_max_position_usd"]
MAX_POSITION_OPEN_PER_CYCLE: int = _POLICY["max_opens_per_cycle"]

# Mirror of George's SAFETY_RAILS.md `allowed_bin_steps` (minimum 10). A
# Meteora pool whose bin_step is not in this list must never emit an OPEN
# signal: George hard-rejects it at the executor, so the open would just
# burn a cycle (and near-1-bp pools earn nothing worth the round trip).
ALLOWED_METEORA_BIN_STEPS: Set[int] = _POLICY["allowed_bin_steps"]

SCORING_SOURCE: str = _POLICY["scoring_source"]
SCORING_VERSION: int = _POLICY["scoring_version"]
MIN_OPEN_SCORE: float = _POLICY["min_open_score"]


def scoring_policy() -> Dict[str, Any]:
    """Versioned scoring-policy identity for audit trails and signals."""
    return {
        "source": SCORING_SOURCE,
        "version": SCORING_VERSION,
        "min_open_score": MIN_OPEN_SCORE,
    }


def get_policy() -> Dict[str, Any]:
    """Return a copy of the loaded Sheldon policy."""
    return _POLICY.copy()


# --------------------------------------------------------------------------
# Volatility-adaptive range constants (centered range)
# --------------------------------------------------------------------------
WIDTH_FACTOR = 0.5        # cover +/- volatility * WIDTH_FACTOR on each side
MIN_HALF_WIDTH = 10       # minimum bins/ticks on each side
MAX_HALF_WIDTH = 1000     # maximum bins/ticks on each side (default cap)
# George's Meteora DLMM executor rail: a single Meteora init tx can only
# create ~70 bins, so Meteora ranges must be capped at 70 bins inclusive.
# inclusive width = upper - lower + 1  =>  max half-width = (70 - 1) // 2
MAX_METEORA_RANGE_WIDTH: int = int(_LIMITS.get("max_meteora_range_width", 70))