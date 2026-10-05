#!/usr/bin/env python3
"""Regime detection for Sheldon's dynamic scoring engine.

Classifies the current market as ``neutral``, ``low_vol``, ``high_vol``,
``fee_boom`` or ``cracked_peg`` and shifts the base component weights
accordingly.

Mirrors the regime-detection functions from the original dynamic.py.
"""

import bisect
import glob
import json
import math
import os
import statistics
from typing import Any, Dict, List, Optional

from .helpers import _class_medians, _max_stable_deviation, _classify, _stablecoins, _load_pools, percentile_rank, build_norms


OFF_UNIVERSE = ("off_universe", "unknown")


def detect_regime(prior_scans: List[List[Dict[str, Any]]],
                  current_pools: List[Dict[str, Any]],
                  cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return a regime descriptor derived from trailing versus current stats.

    Classify the current market as ``neutral``, ``low_vol``, ``high_vol``,
    ``fee_boom`` or ``cracked_peg`` and shift the base component weights
    accordingly.

    Returns a dict with ``name``, ``vol_ratio``, ``peak_apr``, ``stable_dev``,
    ``source``, and ``history_scans``.
    """
    reg_cfg = (cfg.get("dynamic") or {}).get("regime") or {}
    if not reg_cfg.get("enabled", True):
        return {"name": "neutral", "source": "disabled"}

    window = prior_scans[-int(reg_cfg.get("history_scans", 30)):]
    history_vols = []
    history_aprs = []
    for pools in window:
        history_vols.extend(_class_medians(pools, "volatility").values())
        history_aprs.extend(_class_medians(pools, "realized_fee_apr").values())

    current_vols = list(_class_medians(current_pools, "volatility").values())
    current_aprs = [float(p.get("realized_fee_apr") or 0.0)
                    for p in current_pools if isinstance(p, dict)
                    and float(p.get("realized_fee_apr") or 0.0) > 0]

    vol_ratio = None
    if history_vols and current_vols:
        base = statistics.median(history_vols)
        if base > 0:
            vol_ratio = statistics.median(current_vols) / base

    peak_apr = max(current_aprs) if current_aprs else None
    stable_dev = _max_stable_deviation(current_pools)

    depeg_zero = float((cfg.get("constants") or {}).get("depeg_zero_dist", 0.005))
    if stable_dev > depeg_zero:
        name = "cracked_peg"
    elif peak_apr is not None and peak_apr > float(reg_cfg.get("fee_boom_apr_pct", 100.0)):
        name = "fee_boom"
    elif vol_ratio is not None and vol_ratio > float(reg_cfg.get("high_vol_mult", 2.0)):
        name = "high_vol"
    elif vol_ratio is not None and vol_ratio < float(reg_cfg.get("low_vol_mult", 0.5)):
        name = "low_vol"
    else:
        name = "neutral"

    return {
        "name": name,
        "vol_ratio": round(vol_ratio, 3) if vol_ratio is not None else None,
        "peak_apr": round(peak_apr, 2) if peak_apr is not None else None,
        "stable_dev": round(stable_dev, 6),
        "source": "adaptive",
        "history_scans": len(window),
    }


def regime_weight_adjust(base_weights: Dict[str, float],
                         regime_name: str,
                         cfg: Dict[str, Any]) -> Dict[str, float]:
    """Multiplicatively shift base weights for a regime, renormalised to 100.

    Unknown regimes (and ``neutral``) return the base weights unchanged; a
    component missing from a profile is never added.
    """
    shifts = ((cfg.get("dynamic") or {}).get("regime") or {}).get("shifts") or {}
    factors = shifts.get(regime_name) or {}
    if not factors:
        return dict(base_weights)
    adjusted = {}
    for comp, weight in base_weights.items():
        adjusted[comp] = max(0.0, float(weight) * float(factors.get(comp, 1.0)))
    total = sum(adjusted.values())
    if total <= 0:
        return dict(base_weights)
    return {comp: round(w * 100.0 / total, 4) for comp, w in adjusted.items()}