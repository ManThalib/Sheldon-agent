"""Policy loading and constants for the strategy module.

Loads sheldon_policy.json and george's execution_limits.json.
All policy values are single-sourced from sheldon_policy.json.
Mechanical limits are loaded from George's execution_limits.json
so strategy and executor rails stay in sync.
"""

import json
import os
from typing import Any, Dict, Set

from strategy.bin_step_rail import (
    allowed_bin_step_set,
    meteora_bin_step_allowed,
    normalize_rule,
)


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
    policy["scoring_contract_version"] = int(scoring.get("contract_version") or 0)
    policy["scoring_gate"] = str(scoring.get("gate", "score")).lower()
    policy["min_open_score"] = float(scoring.get("min_open_score") or 70.0)

    # Missy pool_features v1 consumption (both flags default false).
    policy["pool_gate"] = dict(data.get("pool_gate") or {})
    policy["pool_ranker"] = dict(data.get("pool_ranker") or {})

    pool = data.get("pool_eligibility") or {}
    # bin_step is a RULE, not a list (owner ruling 2026-10-19): every Meteora
    # step >= 4 is allowed, > 400 is protocol-invalid. `bin_step_rule` is the
    # configured shape; a legacy `allowed_bin_steps` list is accepted only as
    # an envelope so old manifests cannot silently re-narrow the rail.
    policy["bin_step_rule"] = normalize_rule(
        pool.get("bin_step_rule", pool.get("allowed_bin_steps")))
    policy["allowed_bin_steps"] = allowed_bin_step_set(policy["bin_step_rule"])
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

    # Pre-open readiness, SOL policy and rotation requirements (Phase 1,
    # 2026-10-09). Passed through verbatim; readiness.readiness_gate merges
    # its own defaults, so a missing block simply yields the defaults.
    policy["readiness"] = dict(data.get("readiness") or {})
    policy["sol"] = dict(data.get("sol") or {})
    policy["rotation"] = dict(data.get("rotation") or {})

    # Duplicate-close ghost guards and reopen cooldown (2026-10-16).
    # close_emission rails defer automated close emission; the cooldown
    # suppresses re-opening a pool right after its close settles.
    policy["close_emission"] = dict(data.get("close_emission") or {})
    policy["reopen_cooldown_seconds"] = float(
        data.get("reopen_cooldown_seconds", 2700.0))

    # Phase 2 strategy selection config (2026-10-23). Regime-based
    # Spot/Curve/BidAsk selector consumes these parameters.
    policy["strategy_config"] = dict(data.get("strategy_config") or {})

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
# Owner ruling 2026-10-25: swappable idle SOL. Reserve is the SAME value
# readiness already passes to sol_overhead_lamports() (60M calibrated worst
# case), so sizing and funding can never disagree about the reserve.
SOL_RESERVE_LAMPORTS: int = int((_POLICY.get("sol") or {}).get("reserve_lamports", 60_000_000))
SWAPPABLE_SOL_HAIRCUT: float = float((_POLICY.get("sol") or {}).get("swappable_haircut", 0.98))

# Single source: strategy/bin_step_rail.py (owner ruling 2026-10-19).
# Derived from the same rule the predicate uses, so this set can never
# disagree with meteora_bin_step_allowed().
METEORA_BIN_STEP_RULE: dict = _POLICY["bin_step_rule"]
ALLOWED_METEORA_BIN_STEPS: frozenset = _POLICY["allowed_bin_steps"]

SCORING_SOURCE: str = _POLICY["scoring_source"]
SCORING_VERSION: int = _POLICY["scoring_version"]
SCORING_CONTRACT_VERSION: int = _POLICY["scoring_contract_version"]
MIN_OPEN_SCORE: float = _POLICY["min_open_score"]
POOL_GATE_ENABLED: bool = bool((_POLICY.get("pool_gate") or {}).get("enabled"))
POOL_RANKER_ENABLED: bool = bool((_POLICY.get("pool_ranker") or {}).get("enabled"))


def scoring_policy() -> Dict[str, Any]:
    """Versioned scoring-policy identity for audit trails and signals.

    ``contract_version`` / ``gate`` are additive report metadata: they record
    whether this build consumes Missy pool_features v1 and which gate it
    intends to use.  The pre-existing keys are unchanged; the signal schema
    (``run_cycle/signals.py``) still emits its historical shape.
    """
    return {
        "source": SCORING_SOURCE,
        "version": SCORING_VERSION,
        "contract_version": SCORING_CONTRACT_VERSION,
        "gate": _POLICY.get("scoring_gate", "score"),
        "min_open_score": MIN_OPEN_SCORE,
    }


def get_policy() -> Dict[str, Any]:
    """Return a copy of the loaded Sheldon policy."""
    return _POLICY.copy()


# --------------------------------------------------------------------------
# Volatility-adaptive range constants (centered range)
# Policy values live in sheldon_policy.json "range"; the constants below are
# the fallback defaults when the file lacks the block.
# --------------------------------------------------------------------------
_range_policy = _POLICY.get("range") or {}
# Cover +/- volatility * WIDTH_FACTOR on each side.
WIDTH_FACTOR = float(_range_policy.get("width_factor", 0.5))
# Absolute structural floor, in bins (Meteora) / raw ticks (Orca, Raydium).
MIN_HALF_WIDTH = int(_range_policy.get("min_half_width", 10))
# Price-fraction floor per side (0.01 = +/-1%). A tick/bin count floor means
# different price widths per pool: 10 raw ticks on a ts=4 Whirlpool is
# +/-0.1% while 10 bins at bin_step 10 is +/-1%. The 2026-10-14 Czfq3xZZ
# incident opened SOL-USDC at the tick-count floor and reached the range
# edge within 45 minutes, so the floor is expressed in price fraction.
MIN_HALF_FRACTION = float(_range_policy.get("min_half_fraction", 0.01))
MAX_HALF_WIDTH = 1000     # maximum bins/ticks on each side (default cap)
# George's Meteora DLMM executor rail: a single Meteora init tx can only
# create ~70 bins, so Meteora ranges must be capped at 70 bins inclusive.
# inclusive width = upper - lower + 1  =>  max half-width = (70 - 1) // 2
MAX_METEORA_RANGE_WIDTH: int = int(_LIMITS.get("max_meteora_range_width", 70))