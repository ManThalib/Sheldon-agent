#!/usr/bin/env python3
"""Dynamic calibration layer for Sheldon's LP scoring engine.

The static profiles in ``profiles.json`` are absolute: a fee APR is scored
against a fixed cap, volatility against a fixed peak, and verdicts against
fixed thresholds. That loses signal when the market regime shifts (fee
drought, vol spike, a stable cracking) and it cannot rank pools against each
other, only against constants.

This module derives calibration from the Missy scan archive instead:

1. **Rolling percentile normalisation** — score ``fee_yield`` and ``turnover``
   as a percentile of the same pair class over a trailing window, blended
   with the absolute score (``blend_alpha``).
2. **Regime detection** — classify the current market as ``neutral``,
   ``low_vol``, ``high_vol``, ``fee_boom`` or ``cracked_peg`` and shift the
   base component weights accordingly.
3. **Adaptive thresholds** — derive pool OPEN/WATCH cut-offs from the
   quantiles of the scored universe instead of fixed constants.
4. **Expected-PnL position verdicts** — compare the expected value of
   HOLD vs CLOSE vs REBALANCE over a short horizon instead of a static score
   band.
5. **Feedback** — ``tuner.py`` uses the historical outcome data this module
   exposes to nudge base weights (see that module).

Everything fails soft. With too little history, callers get ``None`` and the
engine falls back to the static ``profiles.json`` behaviour, so tests and
cold-start cycles stay deterministic.

Stdlib only.
"""

import bisect
import glob
import json
import math
import os
import statistics


def _min_open_score() -> float:
    """Policy score floor, loaded lazily to avoid import cycles."""
    from strategy import get_policy
    try:
        return float(get_policy().get("min_open_score", 70.0))
    except Exception:
        return 70.0

# Components that have a defined raw metric for percentile normalisation.
RAW_METRICS = {
    "fee_yield": lambda p: float(p.get("realized_fee_apr") or 0.0),
    "turnover": lambda p: (
        (float(p.get("volume_window") or 0.0) / float(p.get("tvl") or 1.0))
        if float(p.get("tvl") or 0.0) > 0 else 0.0
    ),
}

OFF_UNIVERSE = ("off_universe", "unknown")


def _classify(pool):
    """Lazy import to avoid a circular import with lp_scoring."""
    from lp_scoring import classify_pair
    return classify_pair(pool)


def _stablecoins():
    from lp_scoring import STABLECOINS
    return STABLECOINS


def _load_pools(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("pools", [])
    return data if isinstance(data, list) else []


def list_scan_paths(pools_dir, prefix="pool_scan"):
    return sorted(
        p for p in glob.glob(os.path.join(pools_dir, prefix + "-*.json"))
        if not p.endswith((".failed", ".invalid"))
    )


def load_prior_scans(pools_dir, current_path, limit):
    """Return up to ``limit`` pool scans strictly older than ``current_path``.

    ``current_path`` is excluded by identity so the newest scan never leaks
    into its own calibration window (no look-ahead).
    """
    paths = [p for p in list_scan_paths(pools_dir) if p != current_path]
    paths = paths[-int(limit):] if limit else paths
    return [_load_pools(p) for p in paths]


# --------------------------------------------------------------------------
# 1. Rolling percentile normalisation
# --------------------------------------------------------------------------
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
    acc = {}
    for pools in scans:
        for pool in pools:
            if not isinstance(pool, dict):
                continue
            pair_class, _, _ = _classify(pool)
            if pair_class in OFF_UNIVERSE:
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


# --------------------------------------------------------------------------
# 2. Regime detection
# --------------------------------------------------------------------------
def _class_medians(pools, key):
    by_class = {}
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        pair_class, _, _ = _classify(pool)
        if pair_class in OFF_UNIVERSE:
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


def detect_regime(prior_scans, current_pools, cfg):
    """Return a regime descriptor derived from trailing versus current stats."""
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


def regime_weight_adjust(base_weights, regime_name, cfg):
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


# --------------------------------------------------------------------------
# 3. Adaptive pool thresholds
# --------------------------------------------------------------------------
def _quantile(sorted_values, q):
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    idx = int(round(float(q) * (len(sorted_values) - 1)))
    idx = min(max(idx, 0), len(sorted_values) - 1)
    return float(sorted_values[idx])


def adaptive_pool_thresholds(scores, cfg):
    """Derive OPEN/WATCH cut-offs from the quantiles of the scored universe.

    Returns ``None`` when there are too few investable pools to calibrate,
    so the caller keeps the static thresholds.
    """
    thr_cfg = (cfg.get("dynamic") or {}).get("thresholds") or {}
    if not thr_cfg.get("enabled", True):
        return None
    values = sorted(float(s) for s in scores if s is not None)
    if len(values) < int(thr_cfg.get("min_pools", 20)):
        return None

    # Floor both cut-offs at the policy min_open_score: a quantile of a
    # weak universe must never let pools below the static rail through.
    floor_open = max(float(thr_cfg.get("floor_open", 55.0)),
                     _min_open_score())
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


# --------------------------------------------------------------------------
# 4. Expected-PnL position verdicts
# --------------------------------------------------------------------------
def _concentration(pos, cfg):
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


def expected_pnl_verdict(pos, pool, cfg):
    """Compare expected value of HOLD vs CLOSE vs REBALANCE over a horizon.

    Returns a dict with per-action expected USD and the chosen action, or
    ``None`` when inputs are insufficient or the decision is not decisive
    enough to override the score-based verdict.
    """
    pnl_cfg = (cfg.get("dynamic") or {}).get("position_pnl") or {}
    if not pnl_cfg.get("enabled", False):
        return None

    value = pos.get("current_value_usd")
    if value is None or float(value) <= 0:
        return None
    value = float(value)

    apr = float((pool or {}).get("realized_fee_apr") or 0.0)
    vol = float((pool or {}).get("volatility") or 0.0)
    if apr <= 0 or vol <= 0:
        return None

    lower = pos.get("lower_price")
    upper = pos.get("upper_price")
    current = pos.get("current_price")
    if lower is None or upper is None or current is None:
        return None
    lower, upper, current = float(lower), float(upper), float(current)
    if upper <= lower:
        return None
    in_range = lower <= current <= upper

    horizon = float(pnl_cfg.get("horizon_days", 1.0))
    entry_bps = float(pnl_cfg.get("entry_cost_bps", 50.0))
    exit_bps = float(pnl_cfg.get("exit_cost_bps", 50.0))
    claim_usd = float(pnl_cfg.get("claim_cost_usd", 0.02))

    # Fees accrue only while in range; a fresh centred range starts at ~0 IL.
    fee_mult = 1.0 if in_range else 0.0
    fwd_fees = value * (apr / 100.0) * horizon / 365.0 * fee_mult
    concentration = _concentration(pos, cfg)
    il_hold = value * _il_pct(vol, concentration, horizon) / 100.0

    exit_cost = value * exit_bps / 10000.0 + claim_usd
    entry_cost = value * entry_bps / 10000.0

    expected_hold = fwd_fees - il_hold
    expected_close = -exit_cost
    expected_rebalance = fwd_fees - exit_cost - entry_cost

    options = [("HOLD", expected_hold),
               ("CLOSE", expected_close),
               ("REBALANCE", expected_rebalance)]
    ordered = sorted(options, key=lambda kv: kv[1], reverse=True)
    action, best = ordered[0]
    margin = best - ordered[1][1]

    return {
        "action": action,
        "expected_hold_usd": round(expected_hold, 4),
        "expected_close_usd": round(expected_close, 4),
        "expected_rebalance_usd": round(expected_rebalance, 4),
        "margin_usd": round(margin, 4),
        "in_range": in_range,
        "forward_fees_usd": round(fwd_fees, 4),
        "il_hold_usd": round(il_hold, 4),
        "horizon_days": horizon,
        "decisive": margin >= float(pnl_cfg.get("min_pnl_margin_usd", 0.01)),
    }


# --------------------------------------------------------------------------
# Context assembly
# --------------------------------------------------------------------------
def build_context(pools_dir, current_path, current_pools, cfg):
    """Assemble the dynamic scoring context for a cycle.

    Returns a dict (possibly with empty ``norms``) or ``None`` when the
    dynamic layer is disabled. An empty context is harmless: every consumer
    falls back to static behaviour when its slice is missing.
    """
    dyn = cfg.get("dynamic") or {}
    if not dyn.get("enabled", True):
        return None

    pct_cfg = dyn.get("percentile") or {}
    components = pct_cfg.get("components") or ["fee_yield", "turnover"]
    prior = load_prior_scans(pools_dir, current_path,
                             int(pct_cfg.get("history_scans", 60)))
    norms = build_norms(prior, components,
                        int(pct_cfg.get("min_samples", 20)))

    regime = detect_regime(prior, current_pools, cfg)

    # Regime-shifted weights per pair class.
    weights = {}
    for pair_class, profile in (cfg.get("pool_profiles") or {}).items():
        weights[pair_class] = regime_weight_adjust(
            profile.get("weights") or {}, regime["name"], cfg)

    return {
        "regime": regime,
        "norms": norms,
        "blend_alpha": float(pct_cfg.get("blend_alpha", 0.7)),
        "weights": weights,
        "position_pnl": dyn.get("position_pnl") or {},
        "thresholds": None,  # filled in after the first scoring pass
    }


def build_context_from_prior(current_pools: list, prior_scans: list, cfg: dict):
    """Build a dynamic context from an already-assembled history list.

    ``prior_scans`` is a list of pool lists (oldest -> newest), typically a
    rolling window of scans preceding the current one.
    """
    dyn = cfg.get("dynamic") or {}
    if not dyn.get("enabled", True):
        return None

    pctl_cfg = dyn.get("percentile") or {}
    components = pctl_cfg.get("components") or ["fee_yield", "turnover"]
    norms = build_norms(prior_scans, components,
                        int(pctl_cfg.get("min_samples", 20)))

    regime = detect_regime(prior_scans, current_pools, cfg)

    weights = {}
    for pair_class, profile in (cfg.get("pool_profiles") or {}).items():
        weights[pair_class] = regime_weight_adjust(
            profile.get("weights") or {}, regime["name"], cfg)

    return {
        "regime": regime,
        "norms": norms,
        "blend_alpha": float(pctl_cfg.get("blend_alpha", 0.7)),
        "weights": weights,
        "position_pnl": dyn.get("position_pnl") or {},
        "thresholds": None,
    }
