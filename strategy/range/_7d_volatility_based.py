"""Volatility-adaptive range width and bin-step validation.

Uses the pool's 7-day volatility (already in percent) and bin/tick spacing
to compute the number of bins/ticks to extend on each side of center.

All inputs come from Missy scans; no RPC calls are made here.
Policy values are loaded from strategy.policy.
Mechanical limits are loaded from George's execution_limits.json.
"""

import math
from typing import Any, Dict, Optional, Set

from strategy.sheldon_policy import (
    WIDTH_FACTOR,
    MIN_HALF_WIDTH,
    MAX_HALF_WIDTH,
    MAX_METEORA_RANGE_WIDTH,
    ALLOWED_METEORA_BIN_STEPS,
    SCORING_SOURCE,
)


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
      half_width = clamp(ceil(log(1 + f) / log(step_ratio)), MIN, MAX
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