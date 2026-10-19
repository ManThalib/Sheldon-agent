"""Volatility-adaptive range width and bin-step validation.

Uses the pool's 7-day volatility (already in percent) and bin/tick spacing
to compute the number of bins/ticks to extend on each side of center.

All inputs come from Missy scans; no RPC calls are made here.
Policy values are loaded from strategy.policy.
Mechanical limits are loaded from George's execution_limits.json.
"""

import math
from typing import Any, Dict, Optional, Set

from strategy.bin_step_rail import meteora_bin_step_allowed as _rail_step_allowed
from strategy.sheldon_policy import (
    WIDTH_FACTOR,
    MIN_HALF_WIDTH,
    MIN_HALF_FRACTION,
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
    ok, reason = _rail_step_allowed(bin_step)
    if not ok:
        return False, f"{reason} (George rail)"
    return True, ""


def adaptive_half_width(pool: Dict[str, Any]) -> int:
    """Return the number of bins/ticks to extend on each side of center.

    Units match what the pipeline consumes (candidate_filtering, backtest,
    George's builders): Meteora DLMM bins, Orca/Raydium raw 1bp ticks.

    The width is the volatility target held to a price-fraction floor:
      target_half_fraction = (volatility / 100) * WIDTH_FACTOR
      floor_half_fraction  = MIN_HALF_FRACTION (per side)
      step_ratio per unit:
        Meteora: 1 + bin_step/10000           (bin = bin_step basis points)
        Orca/Raydium: 1.0001                  (one raw tick = 1 basis point)
      half = max(target, floor, MIN_HALF_WIDTH), capped per DEX.

    The floor is a price fraction, not a tick count: a tick-count floor
    means different price widths per pool (10 raw ticks on a ts=4 Orca
    Whirlpool is +/-0.1%, 10 bins at Meteora bin_step 10 is +/-1%). The
    2026-10-14 Czfq3xZZ incident opened SOL-USDC at the tick-count floor
    and sat 5 ticks from its edge within 45 minutes.
    """
    dex = (pool.get("dex") or "").lower()
    volatility = float(pool.get("volatility") or 0.0)

    if dex == "meteora":
        # DLMM bin_step is in basis points (e.g. 4 -> 0.04% per bin).
        bin_step = int(pool.get("bin_step") or 0)
        if bin_step <= 0:
            # Cannot translate fractions to bins honestly; fail closed to
            # the structural floor (the bin_step rail disallows opens here).
            return MIN_HALF_WIDTH
        step_ratio = 1.0 + bin_step / 10000.0
    else:
        # Orca Whirlpool / Raydium CLMM: one raw tick = 1 basis point, and
        # half_width is applied as raw ticks downstream.
        step_ratio = 1.0001

    max_half_width = _max_half_width_for_dex(dex)

    floor_fraction = max(MIN_HALF_FRACTION, 1e-9)
    floor_half = math.ceil(math.log1p(floor_fraction) / math.log(step_ratio))

    target_half = 0
    if volatility > 0:
        target_half_fraction = max((volatility / 100.0) * WIDTH_FACTOR, 1e-9)
        target_half = math.ceil(math.log1p(target_half_fraction) / math.log(step_ratio))

    half_width = max(target_half, floor_half, MIN_HALF_WIDTH)
    return int(max(MIN_HALF_WIDTH, min(half_width, max_half_width)))


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