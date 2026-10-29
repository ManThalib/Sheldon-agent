"""Regime-based strategy selector for Spot / Curve / BidAsk.

Decides which strategy_type to emit on a signal based on regime parameters
loaded from sheldon_policy.json `strategy_config`:

  - min_tvl_usd: pool must clear this TVL to be eligible for any strategy
  - trend_threshold: absolute active-bin drift (bins) that counts as trending
  - vol_threshold_pct: volatility percentile threshold; above it Curve/BidAsk
    are favoured over Spot
  - high_score_threshold: legacy v0 score above which Curve/BidAsk become available
  - high_net_daily_pct: v1 expected net fee yield (%/day) above which
    Curve/BidAsk become available
  - curve_half_width_bins: half-width (bins) for Curve ranges centred on active bin
  - bidask_gap_bins: gap between bid_range.upper and ask_range.lower
  - bidask_half_width_bins: half-width (bins) for each side of a BidAsk range

Selection rules (fail-closed on missing data):
  - Spot: default when nothing else applies; always available.
  - Curve: when vol >= vol_threshold_pct AND score >= high_score_threshold AND trend is clear.
  - BidAsk: when vol >= vol_threshold_pct AND score >= high_score_threshold AND trend is flat/choppy.
  - If TVL < min_tvl_usd: only Spot is allowed (conservative).
"""

from typing import Any, Dict, Optional


def select_strategy(
    pool: Dict[str, Any],
    score: float,
    volatility_pct: float,
    trend_sign: Optional[int],
    config: Dict[str, Any],
    metric: str = "score",
) -> str:
    """Return one of "Spot", "Curve", "BidAsk".

    ``metric="net_daily_pct"`` (v1 ranker path) compares the candidate's
    expected net fee yield against ``high_net_daily_pct`` (default 0.10
    %/day).  ``metric="score"`` keeps the legacy v0 path on
    ``high_score_threshold``.  Volatility/trend logic is identical.

    Fail-closed: any missing or unusable input returns "Spot".
    """
    # Fail-closed on missing config
    if not isinstance(config, dict):
        return "Spot"

    min_tvl = config.get("min_tvl_usd")
    try:
        min_tvl = float(min_tvl) if min_tvl is not None else 0.0
    except (TypeError, ValueError):
        min_tvl = 0.0

    tvl = pool.get("tvl_usd") or pool.get("liquidity_usd") or 0.0
    try:
        tvl = float(tvl)
    except (TypeError, ValueError):
        tvl = 0.0

    # Conservative: low TVL pools only get Spot
    if min_tvl > 0 and tvl < min_tvl:
        return "Spot"

    trend_threshold = config.get("trend_threshold")
    try:
        trend_threshold = int(trend_threshold) if trend_threshold is not None else 8
    except (TypeError, ValueError):
        trend_threshold = 8

    vol_threshold_pct = config.get("vol_threshold_pct")
    try:
        vol_threshold_pct = float(vol_threshold_pct) if vol_threshold_pct is not None else 0.0
    except (TypeError, ValueError):
        vol_threshold_pct = 0.0

    if metric == "net_daily_pct":
        high_metric_threshold = config.get("high_net_daily_pct")
        try:
            high_metric_threshold = (float(high_metric_threshold)
                                     if high_metric_threshold is not None else 0.10)
        except (TypeError, ValueError):
            high_metric_threshold = 0.10
    else:
        high_metric_threshold = config.get("high_score_threshold")
        try:
            high_metric_threshold = (float(high_metric_threshold)
                                     if high_metric_threshold is not None else 90.0)
        except (TypeError, ValueError):
            high_metric_threshold = 90.0

    try:
        score = float(score)
    except (TypeError, ValueError):
        return "Spot"

    try:
        volatility_pct = float(volatility_pct)
    except (TypeError, ValueError):
        return "Spot"

    # High volatility + high metric → Curve or BidAsk depending on trend
    if volatility_pct >= vol_threshold_pct and score >= high_metric_threshold:
        if trend_sign is not None and abs(trend_sign) >= trend_threshold:
            # Clear trend → Curve (centred on active bin, riding the trend)
            return "Curve"
        # Flat/choppy → BidAsk (bid below, ask above, capturing both sides)
        return "BidAsk"

    # Default: Spot
    return "Spot"


def build_bidask_ranges(
    active_bin: int,
    config: Dict[str, Any],
) -> Optional[Dict[str, Dict[str, int]]]:
    """Build bid_range and ask_range centred on the active bin.

    Returns {"bid_range": {"lower":..., "upper":...}, "ask_range": {...}}
    or None when inputs are unusable.
    """
    if not isinstance(config, dict):
        return None

    gap_bins = config.get("bidask_gap_bins")
    try:
        gap_bins = int(gap_bins) if gap_bins is not None else 4
    except (TypeError, ValueError):
        gap_bins = 4

    half_width = config.get("bidask_half_width_bins")
    try:
        half_width = int(half_width) if half_width is not None else 10
    except (TypeError, ValueError):
        half_width = 10

    try:
        active_bin = int(active_bin)
    except (TypeError, ValueError):
        return None

    bid_lower = active_bin - half_width
    bid_upper = active_bin - gap_bins
    ask_lower = active_bin + gap_bins
    ask_upper = active_bin + half_width

    if bid_lower >= bid_upper or ask_lower >= ask_upper:
        return None

    return {
        "bid_range": {"lower": bid_lower, "upper": bid_upper},
        "ask_range": {"lower": ask_lower, "upper": ask_upper},
    }


def build_curve_range(
    active_bin: int,
    config: Dict[str, Any],
) -> Optional[Dict[str, int]]:
    """Build a Curve range centred on the active bin.

    Returns {"lower":..., "upper":...} or None when inputs are unusable.
    """
    if not isinstance(config, dict):
        return None

    half_width = config.get("curve_half_width_bins")
    try:
        half_width = int(half_width) if half_width is not None else 20
    except (TypeError, ValueError):
        half_width = 20

    try:
        active_bin = int(active_bin)
    except (TypeError, ValueError):
        return None

    return {"lower": active_bin - half_width, "upper": active_bin + half_width}
