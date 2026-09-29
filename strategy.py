"""Position sizing and range selection for Sheldon open signals.

All inputs come from Missy scans; no RPC calls are made here.
"""

import math
from typing import Any, Dict, List

# --------------------------------------------------------------------------
# Sizing and selection rails
# --------------------------------------------------------------------------
MIN_POSITION_USD = 15.0
DEFAULT_MAX_POSITION_USD = 100.0
MAX_POSITION_OPEN_PER_CYCLE = 3

# --------------------------------------------------------------------------
# Volatility-adaptive range constants (centered range)
# --------------------------------------------------------------------------
WIDTH_FACTOR = 0.5        # cover +/- volatility * WIDTH_FACTOR on each side
MIN_HALF_WIDTH = 10       # minimum bins/ticks on each side
MAX_HALF_WIDTH = 1000     # maximum bins/ticks on each side (default cap)
# George's Meteora DLMM executor rail: a single Meteora init tx can only
# create ~70 bins, so Meteora ranges must be capped at 70 bins inclusive.
# inclusive width = upper - lower + 1  =>  max half-width = (70 - 1) // 2
MAX_METEORA_RANGE_WIDTH = 70


def suggested_position_usd(deployable_usdc: float) -> float:
    """25% of deployable capital, capped at the per-position rail."""
    return min(deployable_usdc * 0.25, DEFAULT_MAX_POSITION_USD)


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


def _range_center(pool: Dict[str, Any], dex: str) -> int:
    """Return the pool's current position index.

    Meteora uses DLMM bin IDs (``active_bin_id``); Raydium CLMM and Orca
    Whirlpool use ticks. Missy may supply ``current_tick``,
    ``current_tick_index`` or reuse ``active_bin_id`` for the tick value.
    """
    if dex == "meteora":
        return int(pool.get("active_bin_id") or 0)
    for key in ("current_tick", "current_tick_index", "active_bin_id"):
        val = pool.get(key)
        if val not in (None, "", 0, "0"):
            return int(val)
    return 0


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
