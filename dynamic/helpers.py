#!/usr/bin/env python3
"""Shared helpers for Sheldon's dynamic module.

Contains utilities used by regime.py, thresholds.py, and calibration.py
to avoid circular imports and duplicated logic.

All imports from lp_scoring are lazy (inside functions) to avoid circular
import issues with the top-level `import dynamic` in lp_scoring.py.
"""

import bisect
import glob
import json
import math
import os
import statistics
from typing import Any, Dict, List, Optional

# No top-level imports from lp_scoring - use lazy imports inside functions


# ---------------------------------------------------------------------------
# Core percentile logic
# ---------------------------------------------------------------------------
def percentile_rank(sorted_values, value):
    """Fraction of the reference window at or below ``value`` (0.0-1.0)."""
    if not sorted_values:
        return 0.5
    return bisect.bisect_right(sorted_values, value) / float(len(sorted_values))


def build_norms(scans, components, min_samples):
    """Build per-pair-class percentile reference windows from prior scans.

    Returns ``{pair_class: {component: sorted values}}``. A class/component
    with fewer than ``min_samples`` observations is omitted (caller falls
    back to absolute scoring).
    """
    from lp_scoring import classify_pair

    acc = {}
    for pools in scans:
        for pool in pools:
            if not isinstance(pool, dict):
                continue
            pair_class, _, _ = classify_pair(pool)
            if pair_class in ("off_universe", "unknown"):
                continue
            bucket = acc.setdefault(pair_class, {c: [] for c in components})
            for comp in components:
                extract = RAW_METRICS.get(comp)
                if extract:
                    bucket[comp].append(extract(pool))
    norms = {}
    for pair_class, bucket in acc.items():
        kept = {c: sorted(vals) for c, vals in bucket.items()
                if len(vals) >= int(min_samples)}
        if kept:
            norms[pair_class] = kept
    return norms


# ---------------------------------------------------------------------------
# Raw metric extractors for percentile normalisation.
# ---------------------------------------------------------------------------
OFF_UNIVERSE = ("off_universe", "unknown")
RAW_METRICS = {
    "fee_yield": lambda p: float(p.get("realized_fee_apr") or 0.0),
    "turnover": lambda p: (
        (float(p.get("volume_window") or 0.0) / float(p.get("tvl") or 1.0))
        if float(p.get("tvl") or 0.0) > 0 else 0.0
    ),
}


# ---------------------------------------------------------------------------
# Regime-detection helpers (also used by calibration.py)
# ---------------------------------------------------------------------------
def _class_medians(pools, key):
    from lp_scoring import classify_pair, STABLECOINS

    by_class = {}
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        pair_class, _, _ = classify_pair(pool)
        if pair_class in ("off_universe", "unknown"):
            continue
        val = float(pool.get(key) or 0.0)
        if val > 0:
            by_class.setdefault(pair_class, []).append(val)
    return {k: statistics.median(v) for k, v in by_class.items()}


def _max_stable_deviation(pools):
    """Largest stablecoin distance from $1 across the pool universe."""
    stables = _stablecoins()
    worst = 0.0
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        for sym_key, px_key in (("token_x_symbol", "token_x_price_usd"),
                                ("token_y_symbol", "token_y_price_usd")):
            sym = (pool.get(sym_key) or "").upper()
            if sym not in stables:
                continue
            price = pool.get(px_key)
            if price is None:
                continue
            worst = max(worst, abs(float(price) - 1.0))
    return worst


def _stablecoins():
    from lp_scoring import STABLECOINS
    return STABLECOINS


def _classify(pool):
    """Lazy import to avoid circular import with lp_scoring."""
    from lp_scoring import classify_pair
    return classify_pair(pool)


# ---------------------------------------------------------------------------
# Quantile helper (shared between thresholds and calibration)
# ---------------------------------------------------------------------------
def _quantile(sorted_values, q):
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    idx = int(round(float(q) * (len(sorted_values) - 1)))
    idx = min(max(idx, 0), len(sorted_values) - 1)
    return float(sorted_values[idx])


# ---------------------------------------------------------------------------
# Min open score helper
# ---------------------------------------------------------------------------
def _min_open_score():
    """Policy score floor, loaded lazily to avoid import cycles."""
    from strategy import get_policy
    try:
        return float(get_policy().get("min_open_score", 70.0))
    except Exception:
        return 70.0


def _concentration(pos, cfg):
    """Concentration multiplier from range width in ticks/bins."""
    c = cfg.get("constants") or {}
    full = float(c.get("il_conc_full_width_ticks", 2000))
    conc_max = float(c.get("il_conc_max", 4.0))
    lower = pos.get("lower_bound")
    upper = pos.get("upper_bound")
    if lower is None or upper is None:
        return conc_max
    width = float(upper) - float(lower)
    if width <= 0:
        return conc_max
    return min(max(full / width, 1.0), conc_max)


def _il_pct(vol_pct, concentration, days):
    """Concentrated-LP quadratic IL approximation (same form as lp_scoring)."""
    return concentration * (vol_pct / 100.0) ** 2 / 8.0 * 100.0 * days


def _load_pools(path):
    """Load pools from a JSON file, same logic as dynamic.py._load_pools."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("pools", [])
    return data if isinstance(data, list) else []