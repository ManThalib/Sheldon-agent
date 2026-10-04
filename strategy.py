"""Position sizing and range selection for Sheldon open signals.

All inputs come from Missy scans; no RPC calls are made here.
Policy values are loaded from sheldon_policy.json. Mechanical limits are
loaded from George's execution_limits.json so strategy and executor rails
stay in sync.
"""

import json
import math
import os
from typing import Any, Dict, List, Optional, Set


# --------------------------------------------------------------------------
# Load policy and mechanical limits from JSON single-sources.
# --------------------------------------------------------------------------
_SHELDON_DIR = os.path.dirname(os.path.abspath(__file__))
_GEORGE_DIR = "/data/.openclaw/workspace-agents/george/agents/meteora-dlmm"
_SHELDON_POLICY_PATH = os.path.join(_SHELDON_DIR, "sheldon_policy.json")
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
# Sizing and selection rails (single-sourced from policy JSON)
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


def get_policy() -> Dict[str, Any]:
    """Return a copy of the loaded Sheldon policy."""
    return _POLICY.copy()


def suggested_position_usd(deployable_usdc: float) -> float:
    """75% of deployable capital, capped at the per-position rail."""
    return min(deployable_usdc * 0.75, DEFAULT_MAX_POSITION_USD)


def open_eligible(wallet: Dict[str, Any]) -> bool:
    """Open gate: idle USDC must cover the minimum, and the suggested
    position must also clear the minimum.
    """
    deployable = float(wallet.get("deployable_usdc") or 0.0)
    idle = float(wallet.get("idle_usdc") or 0.0)
    suggested = suggested_position_usd(deployable)
    return idle >= MIN_POSITION_USD and suggested >= MIN_POSITION_USD


def _max_half_width_for_dex(dex: str) -> int:
    """Return the per-DEX maximum half-width in bins/ticks.

    Mirrors George's executor rails:
      - Meteora: max 70 bins inclusive per Meteora DLMM init-tx limits.
      - Raydium / Orca: use the generic cap.
    """
    if dex == "meteora":
        return (MAX_METEORA_RANGE_WIDTH - 1) // 2
    return MAX_HALF_WIDTH


def meteora_bin_step_allowed(pool: Dict[str, Any], dex: str = None) -> tuple:
    """Return (allowed, reason) for a Meteora pool's bin_step rail.

    Fail-closed: an unknown bin_step counts as disallowed. Non-Meteora
    pools are always allowed (bins are a DLMM concept). Callers that know
    the dex should pass it — Missy _pool records may lack a dex field.
    """
    dex = (dex or pool.get("dex") or "").lower()
    if dex != "meteora":
        return True, ""
    bin_step = pool.get("bin_step")
    if bin_step in (None, "", 0):
        return False, "bin_step unknown (fail-closed)"
    try:
        bin_step = int(bin_step)
    except (TypeError, ValueError):
        return False, "bin_step malformed"
    if bin_step not in ALLOWED_METEORA_BIN_STEPS:
        return False, (
            f"bin_step {bin_step} not in allowed set {sorted(ALLOWED_METEORA_BIN_STEPS)} "
            "(George rail: allowed_bin_steps)"
        )
    return True, ""


def adaptive_half_width(pool: Dict[str, Any]) -> int:
    """Return the number of bins/ticks to extend on each side of center.

    Uses the pool's 7-day volatility (already in percent) and bin/tick spacing:
      target_half_fraction = (volatility / 100) * WIDTH_FACTOR
      step_ratio = 1 + bin_step/10000           (Meteora DLMM)
      step_ratio = 1.0001 ^ tick_spacing       (Raydium CLMM / Orca Whirlpool)
      half_width = clamp(ceil(log(1 + f) / log(step_ratio)), MIN, MAX)
    """
    volatility = float(pool.get("volatility") or 0.0)
    if volatility <= 0:
        return MIN_HALF_WIDTH

    target_half_fraction = (volatility / 100.0) * WIDTH_FACTOR
    target_half_fraction = max(target_half_fraction, 1e-9)

    dex = (pool.get("dex") or "").lower()
    # Meteora DLMM bin_step is in basis points (e.g. 4 -> 0.04%).
    if dex == "meteora":
        bin_step = int(pool.get("bin_step") or 0)
        if bin_step <= 0:
            return MIN_HALF_WIDTH
        step_ratio = 1.0 + bin_step / 10000.0
    else:
        # Raydium CLMM and Orca Whirlpool use 1.0001 per tick.
        tick_spacing = int(pool.get("tick_spacing") or 0)
        if tick_spacing <= 0:
            return MIN_HALF_WIDTH
        step_ratio = math.pow(1.0001, tick_spacing)

    half_width = math.ceil(math.log1p(target_half_fraction) / math.log(step_ratio))
    max_half_width = _max_half_width_for_dex(dex)
    half_width = max(MIN_HALF_WIDTH, min(half_width, max_half_width))
    return int(half_width)


def build_strategies(open_candidates: List[Dict[str, Any]],
                    wallet: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return up to MAX_POSITION_OPEN_PER_CYCLE open strategies.

    Each strategy contains the pool, suggested USDC capital, and the
    adaptive bin/tick range.  Returns an deterministic empty list if the
    wallet is not open-eligible.
    """
    if not open_eligible(wallet):
        return []

    deployable = float(wallet.get("deployable_usdc") or 0.0)
    size = suggested_position_usd(deployable)

    # Sort by pool score descending so the best candidates are selected.
    ranked = sorted(
        open_candidates,
        key=lambda v: (v.get("score") or 0.0),
        reverse=True,
    )[:MAX_POSITION_OPEN_PER_CYCLE]

    strategies = []
    for v in ranked:
        pool = v.get("_pool") or {}
        center = _range_center(pool, v.get("dex"))
        half_width = adaptive_half_width(pool)
        if center is None:
            # Unknown tick/bin: do not emit a range strategy.
            continue
        lower = center - half_width
        upper = center + half_width

        strategies.append({
            "pool_address": v.get("pool_address"),
            "pool_name": v.get("pool"),
            "dex": v.get("dex"),
            "pair_class": v.get("pair_class"),
            "score": v.get("score"),
            "suggested_usdc": size,
            "center": center,
            "half_width": half_width,
            "bin_range": {"lower": lower, "upper": upper},
            "reason": (
                f"score={v.get('score')} {v.get('pair_class')} "
                f"vol={pool.get('volatility')}% width={half_width} center={center}"
            ),
        })
    return strategies


def _range_center(pool: Dict[str, Any], dex: str) -> Optional[int]:
    """Return the pool's current position index.

    Meteora uses DLMM bin IDs (``active_bin_id``); Raydium CLMM and Orca
    Whirlpool use ticks. Missy may supply ``current_tick``,
    ``current_tick_index`` or reuse ``active_bin_id`` for the tick value.
    Returns None when the tick/bin is unknown.
    """
    if dex == "meteora":
        val = pool.get("active_bin_id")
        if val is None:
            return None
        return int(val)
    for key in ("current_tick", "current_tick_index", "active_bin_id"):
        val = pool.get(key)
        if val is None or val in ("", "0"):
            continue
        return int(val)
    return None


def capital_plan(wallet: Dict[str, Any]) -> Dict[str, Any]:
    """Human-readable capital plan section for the report."""
    idle = float(wallet.get("idle_usdc") or 0.0)
    dust = float(wallet.get("dust_total_usdc") or 0.0)
    deployable = float(wallet.get("deployable_usdc") or 0.0)
    size = suggested_position_usd(deployable)
    return {
        "idle_usdc": idle,
        "dust_total_usdc": dust,
        "deployable_usdc": deployable,
        "suggested_position_usdc": size,
        "open_eligible": open_eligible(wallet),
        "min_position_usd": MIN_POSITION_USD,
        "max_position_usd": DEFAULT_MAX_POSITION_USD,
        "max_opens_per_cycle": MAX_POSITION_OPEN_PER_CYCLE,
    }
