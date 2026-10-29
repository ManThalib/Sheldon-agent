"""Open candidate filtering and strategy building.

With ``pool_ranker.enabled`` false, ranks open candidates by score and
selects top N (historical behaviour).  With it true, v1 records are ranked
by expected net fee yield (``(-net_daily_pct, pool_address)``) and the
selector unlocks Curve/BidAsk on ``high_net_daily_pct``.

Dependencies (all from strategy package):
  - open_eligible(), suggested_position_usd() from sizing
  - MAX_POSITION_OPEN_PER_CYCLE from policy
  - _range_center(), adaptive_half_width() from range
"""

from typing import Any, List, Dict
from strategy.sheldon_policy import MAX_POSITION_OPEN_PER_CYCLE, get_policy
from strategy.position_sizing import open_eligible, suggested_position_usd, deployable_capital_usd
from strategy.range._7d_volatility_based import _range_center, adaptive_half_width
from strategy.sheldon_selector import select_strategy
import pool_features


def _rank_candidates(open_candidates: List[Dict[str, Any]]):
    """Return (ranked_candidates, metrics_by_addr, ranker_cfg).

    With ``pool_ranker.enabled`` false this is exactly the historical
    score-descending sort.  With it true, v1 records are ranked by
    expected net fee yield (``(-net_daily_pct, pool_address)``); v0 records
    keep score ordering and unrankable v1 records go last (or are dropped
    when ``unknown_rank == "skip"``).
    """
    ranker_cfg = pool_features.ranker_policy()
    ranker_enabled = (bool(ranker_cfg.get("enabled"))
                      and pool_features.contract_enabled())
    if not ranker_enabled:
        return (
            sorted(open_candidates,
                   key=lambda v: (v.get("score") or 0.0), reverse=True),
            {},
            ranker_cfg,
        )

    metrics: Dict[str, dict] = {}
    prepared = []
    unknown_policy = str(ranker_cfg.get("unknown_rank") or "last").lower()
    for v in open_candidates:
        pool = v.get("_pool") or {}
        if pool_features.is_v1(pool):
            m = pool_features.compute_net_yield(pool, ranker_cfg)
            metrics[v.get("pool_address")] = m
            if m.get("status") == "il_skip":
                continue
            if m.get("status") == "below_min_net":
                continue
            if not m.get("rankable") and unknown_policy == "skip":
                continue
        prepared.append(v)

    prepared.sort(
        key=lambda v: pool_features.rank_key(
            v, ranker_cfg, metrics.get(v.get("pool_address"))))
    return prepared, metrics, ranker_cfg


def build_strategies(open_candidates: List[Dict[str, Any]],
                    wallet: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return up to MAX_POSITION_OPEN_PER_CYCLE open strategies.

    Each strategy contains the pool, suggested USDC capital, and the
    adaptive bin/tick range.  Returns an deterministic empty list if the
    wallet is not open-eligible.
    """
    if not open_eligible(wallet):
        return []

    deployable = deployable_capital_usd(wallet)
    size = suggested_position_usd(deployable)

    ranked_all, metrics, ranker_cfg = _rank_candidates(open_candidates)
    ranked = ranked_all[:MAX_POSITION_OPEN_PER_CYCLE]

    strategies = []
    policy = get_policy()
    strategy_config = policy.get("strategy_config") or {}
    ranker_on = (bool(ranker_cfg.get("enabled"))
                 and pool_features.contract_enabled())
    for v in ranked:
        pool = v.get("_pool") or {}
        center = _range_center(pool, v.get("dex"))
        if center is None:
            # Unknown tick/bin: do not emit a range strategy.
            continue
        half_width = adaptive_half_width(pool)
        lower = center - half_width
        upper = center + half_width

        # Regime-based strategy selection: Spot / Curve / BidAsk.
        score = v.get("score") or 0.0
        volatility_pct = pool.get("volatility") or 0.0
        trend_sign = v.get("trend_sign")
        v1_rank = ranker_on and pool_features.is_v1(pool)
        if v1_rank:
            m = metrics.get(v.get("pool_address")) or {}
            metric_value = m.get("net_daily_pct")
            strategy_type = select_strategy(
                pool,
                metric_value if metric_value is not None else 0.0,
                volatility_pct,
                trend_sign,
                strategy_config,
                metric="net_daily_pct",
            )
        else:
            strategy_type = select_strategy(
                pool,
                score,
                volatility_pct,
                trend_sign,
                strategy_config,
            )

        strategy = {
            "pool_address": v.get("pool_address"),
            "pool_name": v.get("pool"),
            "dex": v.get("dex"),
            "pair_class": v.get("pair_class"),
            "score": v.get("score"),
            "suggested_usdc": size,
            "center": center,
            "half_width": half_width,
            "bin_range": {"lower": lower, "upper": upper},
            "strategy_type": strategy_type,
            "reason": (
                f"score={v.get('score')} {v.get('pair_class')} "
                f"vol={pool.get('volatility')}% width={half_width} center={center} "
                f"strategy={strategy_type}"
            ),
        }
        if v1_rank:
            # Additive audit fields; never used by George's signal schema.
            m = metrics.get(v.get("pool_address")) or {}
            strategy["net_daily_pct"] = m.get("net_daily_pct")
            strategy["ranker"] = m
            strategy["reason"] = (
                f"net_daily_pct={m.get('net_daily_pct')} "
                f"score={v.get('score')} {v.get('pair_class')} "
                f"vol={pool.get('volatility')}% width={half_width} center={center} "
                f"strategy={strategy_type}"
            )
        strategies.append(strategy)
    return strategies