"""Open candidate filtering and strategy building.

Ranks open candidates by score, selects top N, and builds volatility-adaptive
range strategies for the top candidates.

Dependencies (all from strategy package):
  - open_eligible(), suggested_position_usd() from sizing
  - MAX_POSITION_OPEN_PER_CYCLE from policy
  - _range_center(), adaptive_half_width() from range
"""

from typing import Any, List, Dict
from strategy.sheldon_policy import MAX_POSITION_OPEN_PER_CYCLE
from strategy.position_sizing import open_eligible, suggested_position_usd
from strategy.width._7d_volatility_based import _range_center, adaptive_half_width


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
        if center is None:
            # Unknown tick/bin: do not emit a range strategy.
            continue
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