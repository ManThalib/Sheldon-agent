#!/usr/bin/env python3
"""Adaptive pool thresholds for Sheldon's dynamic scoring engine.

Derive OPEN/WATCH cut-offs from the quantiles of the scored universe
instead of fixed constants.  Falls back to static thresholds when there
are too few investable pools to calibrate.

Mirrors the adaptive-thresholds functions from the original dynamic.py.
"""

from typing import Any, Dict, List, Optional
from .helpers import _quantile, _min_open_score


def adaptive_pool_thresholds(scores: List[float], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Derive OPEN/WATCH cut-offs from the quantiles of the scored universe.

    Returns ``None`` when there are too few investable pools to calibrate,
    so the caller keeps the static thresholds.

    When the v1 safety gate is enabled, v1 verdicts are pass/fail and must
    not be re-tagged by a quantile of the universe: the re-tag is disabled
    entirely (v0 records keep static thresholds too, which is the safe
    direction).  See docs/sheldon_contract_v1.md.
    """
    try:
        import pool_features
        if (pool_features.gate_policy().get("enabled")
                and pool_features.contract_enabled()):
            return None
    except Exception:
        pass
    thr_cfg = (cfg.get("dynamic") or {}).get("thresholds") or {}
    if not thr_cfg.get("enabled", True):
        return None
    values = sorted(float(s) for s in scores if s is not None)
    if len(values) < int(thr_cfg.get("min_pools", 20)):
        return None

    # Floor both cut-offs at the policy min_open_score: a quantile of a
    # weak universe must never let pools below the static rail through.
    from .helpers import _min_open_score as _min_score
    floor_open = max(float(thr_cfg.get("floor_open", 55.0)),
                     _min_score())
    open_ = _quantile(values, float(thr_cfg.get("open_quantile", 0.90)))
    watch = _quantile(values, float(thr_cfg.get("watch_quantile", 0.70)))
    open_ = min(max(open_, floor_open),
                float(thr_cfg.get("ceil_open", 90.0)))
    watch = min(max(watch, float(thr_cfg.get("floor_watch", 40.0))),
                float(thr_cfg.get("ceil_watch", 80.0)))
    if watch > open_:
        watch = open_
    return {
        "open": round(open_, 2),
        "watch": round(watch, 2),
        "source": "adaptive",
        "n": len(values),
    }