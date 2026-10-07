#!/usr/bin/env python3
"""Calibration logic for Sheldon's dynamic scoring engine.

Assembles the full dynamic scoring context (norms, regime, weights, thresholds)
from prior scans and current pool data.  Contains expected-PnL position verdicts
and weight-adjustment logic.

Mirrors the context-assembly and calibration functions from the original dynamic.py.
"""

import json
import math
import os
from typing import Any, Dict, List, Optional

from .helpers import percentile_rank, build_norms, _concentration, _il_pct
from .regime import detect_regime, regime_weight_adjust
from .thresholds import adaptive_pool_thresholds


def _quantile(sorted_values, q):
    """Quantile helper (same logic as dynamic.py)."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    idx = int(round(float(q) * (len(sorted_values) - 1)))
    idx = min(max(idx, 0), len(sorted_values) - 1)
    return float(sorted_values[idx])


def _class_medians(pools, key):
    """Median-per-pair-class helper."""
    by_class = {}
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        pair_class, _, _ = _classify(pool)
        if pair_class in ("off_universe", "unknown"):
            continue
        val = float(pool.get(key) or 0.0)
        if val > 0:
            by_class.setdefault(pair_class, []).append(val)
    return {k: statistics.median(v) for k, v in by_class.items()}


def _max_stable_deviation(pools):
    """Largest stablecoin distance from $1 across the pool universe."""
    from .helpers import _stablecoins
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


def _classify(pool):
    """Lazy classify to avoid circular imports."""
    from .helpers import classify_pair
    return classify_pair(pool)


def _stablecoins():
    from .helpers import STABLECOINS
    return STABLECOINS


def build_context(pools_dir: str, current_path: str,
                  current_pools: List[Dict[str, Any]],
                  cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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
    prior = _load_prior_scans(pools_dir, current_path,
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


def build_context_from_prior(current_pools: List[Dict[str, Any]],
                             prior_scans: List[List[Dict[str, Any]]],
                             cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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


def _load_prior_scans(pools_dir: str, current_path: str, limit: int) -> List[List[Dict[str, Any]]]:
    """Return up to ``limit`` pool scans strictly older than ``current_path``."""
    import glob as glob_mod
    paths = [p for p in glob_mod.glob(os.path.join(pools_dir, "pool_scan-*.json"))
             if not p.endswith((".failed", ".invalid"))]
    paths = [p for p in paths if p != current_path]
    paths = paths[-int(limit):] if limit else paths
    scans = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        if isinstance(data, dict):
            data = data.get("pools", [])
        scans.append(data if isinstance(data, list) else [])
    return scans


def expected_pnl_verdict(pos, pool, cfg, candidate_pool=None) -> Optional[Dict[str, Any]]:
    """Compare expected value of HOLD vs CLOSE vs REBALANCE vs ROTATE over a horizon.

    Returns a dict with per-action expected USD and the chosen action, or
    ``None`` when inputs are insufficient or the decision is not decisive
    enough to override the score-based verdict.

    If ``candidate_pool`` is provided, a fourth ``ROTATE`` option is computed
    as:  C_yield - entry_cost - exit_cost - swap_cost - claim_cost
    where C_yield is the candidate pool's fee yield projected over the horizon.
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
    max_slippage_bps = float(pnl_cfg.get("default_max_slippage_bps", 100.0))

    # Fees accrue only while in range; a fresh centred range starts at ~0 IL.
    fee_mult = 1.0 if in_range else 0.0
    fwd_fees = value * (apr / 100.0) * horizon / 365.0 * fee_mult
    concentration = _concentration(pos, cfg)
    il_hold = value * _il_pct(vol, concentration, horizon) / 100.0

    exit_cost = value * exit_bps / 10000.0 + claim_usd
    entry_cost = value * entry_bps / 10000.0
    swap_cost = value * max_slippage_bps / 10000.0

    expected_hold = fwd_fees - il_hold
    expected_close = -exit_cost
    expected_rebalance = fwd_fees - exit_cost - entry_cost

    options = [("HOLD", expected_hold),
               ("CLOSE", expected_close),
               ("REBALANCE", expected_rebalance)]

    # ROTATE: rotate from current position into a candidate pool
    rotate_value = None
    if candidate_pool is not None:
        cand_apr = float(candidate_pool.get("realized_fee_apr") or 0.0)
        cand_vol = float(candidate_pool.get("volatility") or 0.0)
        if cand_apr > 0 and cand_vol > 0 and value > 0:
            value_f = float(value)
            # Candidate yield over horizon (projected from pool APR)
            fwd_fees_cand = value_f * (cand_apr / 100.0) * horizon / 365.0
            expected_rotate = fwd_fees_cand - entry_cost - exit_cost - swap_cost - claim_usd
            rotate_value = expected_rotate
            options.append(("ROTATE", expected_rotate))

    ordered = sorted(options, key=lambda kv: kv[1], reverse=True)
    action, best = ordered[0]
    margin = best - ordered[1][1]

    return {
        "action": action,
        "expected_hold_usd": round(expected_hold, 4),
        "expected_close_usd": round(expected_close, 4),
        "expected_rebalance_usd": round(expected_rebalance, 4),
        "expected_rotate_usd": (
            round(rotate_value, 4) if rotate_value is not None else None
        ),
        "margin_usd": round(margin, 4),
        "in_range": in_range,
        "forward_fees_usd": round(fwd_fees, 4),
        "il_hold_usd": round(il_hold, 4),
        "horizon_days": horizon,
        "decisive": margin >= float(pnl_cfg.get("min_pnl_margin_usd", 0.01)),
    }