"""Sheldon's LP scoring engine.

Deterministic scoring for LP positions on Meteora DLMM, Raydium CLMM, and
Orca Whirlpool. Universe policy: stablecoin and high-cap pairs only — no
memecoins. Off-universe pools are hard-gated out before scoring.

Reads the newest Missy outputs (pool scan + position scan) and produces:
  - Pool LP Opportunity Score (0-100): is this pool worth an LP position?
  - Position Health Score (0-100): should an open LP position stay open?
  - Verdicts: OPEN_CANDIDATE / WATCH / IGNORE / HOLD / REVIEW / CLOSE /
    REBALANCE / COLLECT_FEES

Pair classes (auto-detected from the pair symbols in the pool name):
  - stable_stable:   e.g. USDC-USDT. Risk = depeg, not volatility.
  - stable_bluechip: e.g. SOL-USDC, cbBTC-SOL. Risk = IL / trend out of range.
  - bluechip_bluechip: e.g. SOL-cbBTC. Same risk model as stable_bluechip.
  - off_universe / unknown: pools are IGNOREd; existing positions are CLOSEd.

Stdlib only. Run:
    python3 lp_scoring.py [--pools-dir D] [--positions-dir D] [--json]
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from models import PoolScore, PositionScore
from profiles import (
    COLLECT_MIN_USD,
    COLLECT_PCT_OF_VALUE,
    DEPEG_ZERO_DIST,
    DEPTH_MAX_USD,
    DEPTH_MIN_USD,
    FEE_SENTINELS,
    HIGH_CAPS,
    MISSING_DEFAULT_DEPEG_EXPOSURE,
    MISSING_DEFAULT_FEE_CAPTURE,
    MISSING_DEFAULT_IL_RISK,
    MISSING_DEFAULT_RANGE_STATUS,
    MISSING_DEFAULT_STALENESS,
    POOL_PROFILES,
    POOL_THRESHOLDS,
    POSITION_PROFILES,
    POSITION_THRESHOLDS,
    STABLECOINS,
    STALE_GRACE_DAYS,
    STALE_ZERO_DAYS,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def newest_file(directory: str, prefix: str) -> Optional[str]:
    paths = sorted(
        p for p in glob.glob(os.path.join(directory, prefix + "-*.json"))
        if not p.endswith((".failed", ".invalid"))
    )
    return paths[-1] if paths else None


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def is_fresh(path: str, max_age_seconds: float) -> bool:
    try:
        return (time.time() - os.path.getmtime(path)) <= max_age_seconds
    except OSError:
        return False


_PAIR_SPLIT_RE = re.compile(r"[-/]")


def pair_symbols(name: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract (symbol_x, symbol_y) from a pool name like 'SOL-USDC (bin 4)'."""
    if not name:
        return None, None
    base = name.split("(", 1)[0].strip()
    parts = [p.strip().upper() for p in _PAIR_SPLIT_RE.split(base) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


def classify_pair(record: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (pair_class, sym_x, sym_y).

    pair_class in: stable_stable, stable_bluechip, bluechip_bluechip,
    off_universe, unknown.
    """
    sym_x = (record.get("token_x_symbol") or "").upper() or None
    sym_y = (record.get("token_y_symbol") or "").upper() or None
    if not sym_x or not sym_y:
        name_x, name_y = pair_symbols(record.get("name") or record.get("pool_name"))
        sym_x = sym_x or name_x
        sym_y = sym_y or name_y
    if not sym_x or not sym_y:
        return "unknown", sym_x, sym_y
    in_x = sym_x in STABLECOINS or sym_x in HIGH_CAPS
    in_y = sym_y in STABLECOINS or sym_y in HIGH_CAPS
    if not in_x or not in_y:
        return "off_universe", sym_x, sym_y
    x_stable = sym_x in STABLECOINS
    y_stable = sym_y in STABLECOINS
    if x_stable and y_stable:
        return "stable_stable", sym_x, sym_y
    if x_stable or y_stable:
        return "stable_bluechip", sym_x, sym_y
    return "bluechip_bluechip", sym_x, sym_y


ComponentResult = Dict[str, Any]


def _component(score: float, missing: Optional[List[str]] = None, reason: str = "") -> ComponentResult:
    return {"score": round(score, 2), "missing": missing or [], "reason": reason}


# --------------------------------------------------------------------------
# Position input helpers
# --------------------------------------------------------------------------
def _is_fee_unreliable(pos: Dict[str, Any]) -> bool:
    """True if Orca/CLMM raw fee fields contain sentinel values."""
    raw = pos.get("fees_owed_raw") or []
    if any(v in FEE_SENTINELS for v in raw):
        return True
    # Also treat absurdly large decoded fee values as unreliable.
    fees_usd = pos.get("fees_usd")
    if isinstance(fees_usd, (int, float)) and fees_usd >= 1e12:
        return True
    return False


def _estimate_expected_fees(pos: Dict[str, Any], pool: Dict[str, Any]) -> float:
    """Infer expected fees from pool APR, position value, and age.

    Returns 0.0 when any required input is missing or non-positive.
    """
    try:
        apr = float(pool.get("realized_fee_apr") or 0.0) / 100.0
        value = float(pos.get("current_value_usd") or 0.0)
        days = float(pos.get("days_open") or 0.0)
        if apr <= 0 or value <= 0 or days <= 0:
            return 0.0
        return value * apr * (days / 365.0)
    except (TypeError, ValueError):
        return 0.0


def _estimate_position_value(pos: Dict[str, Any], pool: Dict[str, Any]) -> Optional[float]:
    """Fallback estimate of position USD value from token amounts and prices."""
    try:
        tx = pos.get("token_x_amount") or {}
        ty = pos.get("token_y_amount") or {}
        if not tx or not ty:
            return None
        ux = float(tx.get("ui") if isinstance(tx, dict) else 0.0)
        uy = float(ty.get("ui") if isinstance(ty, dict) else 0.0)
        px = float(pool.get("token_x_price_usd") or 0.0)
        py = float(pool.get("token_y_price_usd") or 0.0)
        if px <= 0 or py <= 0:
            return None
        return ux * px + uy * py
    except (TypeError, ValueError):
        return None


def _position_value(pos: Dict[str, Any], pool: Dict[str, Any]) -> Tuple[Optional[float], List[str]]:
    """Return best-effort current_value_usd and list of missing fields."""
    value = pos.get("current_value_usd")
    try:
        if isinstance(value, (int, float)) and value > 0:
            return float(value), []
    except (TypeError, ValueError):
        pass
    fallback = _estimate_position_value(pos, pool)
    if fallback is not None and fallback > 0:
        return fallback, ["current_value_usd"]
    return None, ["current_value_usd"]


# --------------------------------------------------------------------------
# Pool LP Opportunity Score
# --------------------------------------------------------------------------
def score_pool_fee_yield(pool: Dict[str, Any], max_pts: float, apr_cap_pct: float) -> float:
    """Realized fee APR only. fee/TVL is intentionally NOT added here: it is
    the same underlying signal as `turnover` (turnover x fee rate), and
    counting it twice double-rewards high-turnover pools."""
    apr_pct = float(pool.get("realized_fee_apr") or 0.0)
    return round(max_pts * clamp(apr_pct / apr_cap_pct, 0.0, 1.0), 2)


def score_pool_turnover(pool: Dict[str, Any], max_pts: float) -> float:
    """Window volume / TVL. Turnover of 5x+ over the window maxes out."""
    tvl = float(pool.get("tvl") or 0.0)
    volume = float(pool.get("volume_window") or 0.0)
    if tvl <= 0:
        return 0.0
    return round(max_pts * clamp((volume / tvl) / 5.0, 0.0, 1.0), 2)


def score_pool_depth(pool: Dict[str, Any], max_pts: float) -> float:
    """Log scale between DEPTH_MIN_USD and DEPTH_MAX_USD."""
    tvl = float(pool.get("tvl") or 0.0)
    if tvl < DEPTH_MIN_USD:
        return 0.0
    frac = math.log(tvl / DEPTH_MIN_USD) / math.log(DEPTH_MAX_USD / DEPTH_MIN_USD)
    return round(max_pts * clamp(frac, 0.0, 1.0), 2)


def score_pool_volatility_fit(pool: Dict[str, Any], max_pts: float, peak_pct: float) -> float:
    """Triangular curve peaking at the profile's peak volatility."""
    vol = float(pool.get("volatility") or 0.0)
    if vol <= 0:
        return 0.0
    if vol >= 3.0 * peak_pct:
        return 0.0
    if vol <= peak_pct:
        score = max_pts * (vol / peak_pct)
    else:
        score = max_pts * (3.0 * peak_pct - vol) / (2.0 * peak_pct)
    return round(clamp(score, 0.0, max_pts), 2)


def score_pool_depeg_safety(pool: Dict[str, Any], max_pts: float, sym_x: str, sym_y: str) -> float:
    """Distance of stable side(s) from $1. Worst side wins; a depegging
    stable drains the position into the bad token. Unknown price gets
    half credit (fail-suspicious: unseen pegs are not trusted pegs)."""
    sides = []
    for sym, key in ((sym_x, "token_x_price_usd"), (sym_y, "token_y_price_usd")):
        if sym not in STABLECOINS:
            continue
        price = pool.get(key)
        if price is None:
            sides.append(0.5)
            continue
        try:
            dist = abs(float(price) - 1.0)
            sides.append(clamp(1.0 - dist / DEPEG_ZERO_DIST, 0.0, 1.0))
        except (TypeError, ValueError):
            sides.append(0.5)
    if not sides:
        return round(max_pts, 2)
    return round(max_pts * min(sides), 2)


def score_pool(pool: Dict[str, Any]) -> PoolScore:
    pair_class, sym_x, sym_y = classify_pair(pool)

    if pair_class in ("off_universe", "unknown"):
        return PoolScore(
            pool=pool.get("name"),
            pool_address=pool.get("pool_address"),
            dex=pool.get("dex"),
            pair_class=pair_class,
            pair=[sym_x, sym_y],
            score=0.0,
            components={},
            verdict="IGNORE",
            reason=f"{pair_class} pair: universe policy is stables/high-caps only",
            data_quality={"missing": [], "unreliable": [], "notes": []},
            _pool=pool,
        )

    profile = POOL_PROFILES[pair_class]
    w = profile["weights"]
    components: Dict[str, float] = {
        "fee_yield": score_pool_fee_yield(pool, w["fee_yield"], profile["apr_cap_pct"]),
        "turnover": score_pool_turnover(pool, w["turnover"]),
        "depth": score_pool_depth(pool, w["depth"]),
        "volatility_fit": score_pool_volatility_fit(pool, w["volatility_fit"], profile["vol_peak_pct"]),
    }
    if "depeg_safety" in w:
        components["depeg_safety"] = score_pool_depeg_safety(pool, w["depeg_safety"], sym_x, sym_y)

    total = round(sum(components.values()), 2)
    if total >= POOL_THRESHOLDS["open"]:
        verdict = "OPEN_CANDIDATE"
    elif total >= POOL_THRESHOLDS["watch"]:
        verdict = "WATCH"
    else:
        verdict = "IGNORE"

    reason = (
        f"{verdict}: score={total}; "
        f"fee_yield={components.get('fee_yield', 0.0):.2f}, "
        f"turnover={components.get('turnover', 0.0):.2f}, "
        f"depth={components.get('depth', 0.0):.2f}, "
        f"volatility_fit={components.get('volatility_fit', 0.0):.2f}"
    )
    if "depeg_safety" in components:
        reason += f", depeg_safety={components['depeg_safety']:.2f}"

    return PoolScore(
        pool=pool.get("name"),
        pool_address=pool.get("pool_address"),
        dex=pool.get("dex"),
        pair_class=pair_class,
        pair=[sym_x, sym_y],
        score=total,
        components=components,
        verdict=verdict,
        reason=reason,
        data_quality={"missing": [], "unreliable": [], "notes": []},
        _pool=pool,
    )


# --------------------------------------------------------------------------
# Position Health Score
# --------------------------------------------------------------------------
def _pos_price_bounds(pos: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    lower = pos.get("lower_price")
    upper = pos.get("upper_price")
    current = pos.get("current_price")
    if lower is None or upper is None or current is None:
        return None, None, None
    try:
        return float(lower), float(upper), float(current)
    except (TypeError, ValueError):
        return None, None, None


def score_position_range_status(pos: Dict[str, Any], max_pts: float) -> ComponentResult:
    """In-range earns fees; distance-to-boundary discounts positions about
    to flip single-sided. Missing data is flagged, not treated as out-of-range."""
    lower, upper, current = _pos_price_bounds(pos)
    if lower is not None and upper is not None and current is not None and upper > lower:
        if not (lower <= current <= upper):
            return _component(0.0, reason=f"current_price {current:.6g} outside [{lower:.6g}, {upper:.6g}]")
        span = upper - lower
        edge_dist = min(current - lower, upper - current) / (span / 2.0)
        # Full credit when price sits in the middle 50% of the range;
        # fades toward 60% credit as the price hugs a boundary.
        edge_frac = clamp((edge_dist - 0.0) / 0.5, 0.0, 1.0)
        score = max_pts * (0.6 + 0.4 * edge_frac)
        return _component(score, reason=f"in range; edge distance={edge_dist:.2%}")

    in_range = pos.get("in_range")
    if isinstance(in_range, bool):
        score = max_pts if in_range else 0.0
        return _component(score, missing=["lower_price", "upper_price", "current_price"],
                          reason=f"price bounds missing; in_range={in_range}")

    return _component(
        max_pts * MISSING_DEFAULT_RANGE_STATUS,
        missing=["lower_price", "upper_price", "current_price", "in_range"],
        reason="price bounds and in_range missing; using fail-suspicious default",
    )


def score_position_fee_capture(pos: Dict[str, Any], max_pts: float, pool: Dict[str, Any]) -> ComponentResult:
    """Fees earned vs expected. When expected is missing, infer it from the
    pool APR and position value so the component is usable more often."""
    if _is_fee_unreliable(pos):
        return _component(
            max_pts * MISSING_DEFAULT_FEE_CAPTURE,
            unreliable=["fees_owed_raw"],
            reason="Orca raw fee sentinel detected; fee data unreliable",
        )

    earned = pos.get("fees_usd")
    if not isinstance(earned, (int, float)):
        return _component(
            max_pts * MISSING_DEFAULT_FEE_CAPTURE,
            missing=["fees_usd"],
            reason="fees_usd missing; using fail-suspicious default",
        )

    expected = pos.get("expected_fees_usd")
    if expected is None or (isinstance(expected, (int, float)) and float(expected) <= 0):
        expected = _estimate_expected_fees(pos, pool)

    if expected is None or (isinstance(expected, (int, float)) and float(expected) <= 0):
        return _component(
            max_pts * MISSING_DEFAULT_FEE_CAPTURE,
            missing=["expected_fees_usd"],
            reason="expected_fees_usd missing and cannot be estimated",
        )

    ratio = clamp(float(earned) / float(expected), 0.0, 1.0)
    return _component(max_pts * ratio, reason=f"earned ${earned:.4f} vs expected ${expected:.4f}")


def score_position_il_risk(pos: Dict[str, Any], max_pts: float) -> ComponentResult:
    """IL exposure for bluechip pairs. il_estimate_pct of 10%+ zeroes it.
    Missing estimate scores a fail-suspicious default so data issues are flagged."""
    il = pos.get("il_estimate_pct")
    if il is None:
        return _component(
            max_pts * MISSING_DEFAULT_IL_RISK,
            missing=["il_estimate_pct"],
            reason="IL estimate missing; using fail-suspicious default",
        )
    try:
        il_pct = float(il)
    except (TypeError, ValueError):
        return _component(
            max_pts * MISSING_DEFAULT_IL_RISK,
            missing=["il_estimate_pct"],
            reason="IL estimate unparseable; using fail-suspicious default",
        )
    return _component(max_pts * clamp(1.0 - il_pct / 10.0, 0.0, 1.0),
                      reason=f"IL estimate={il_pct:.2f}%")


def score_position_depeg_exposure(pos: Dict[str, Any], max_pts: float, sym_x: str, sym_y: str,
                                  pool: Dict[str, Any]) -> ComponentResult:
    """Stable/stable IL is depeg risk. Full credit when both pegs hold and
    the position is two-sided; penalized when single-sided in a depegged
    token (the exit already happened, against you)."""
    def peg_frac(sym: str, pool_key: str) -> float:
        if sym not in STABLECOINS:
            return 1.0
        price = pool.get(pool_key) if pool else None
        if price is None:
            return 0.5
        try:
            return clamp(1.0 - abs(float(price) - 1.0) / DEPEG_ZERO_DIST, 0.0, 1.0)
        except (TypeError, ValueError):
            return 0.5

    fx = peg_frac(sym_x, "token_x_price_usd")
    fy = peg_frac(sym_y, "token_y_price_usd")

    # Single-sided detection from token amounts.
    ui_x = ui_y = None
    tx, ty = pos.get("token_x_amount"), pos.get("token_y_amount")
    try:
        ui_x = float((tx or {}).get("ui")) if tx else None
        ui_y = float((ty or {}).get("ui")) if ty else None
    except (TypeError, ValueError):
        ui_x = ui_y = None

    if ui_x is not None and ui_y is not None and (ui_x > 0) != (ui_y > 0):
        held = fx if ui_x > 0 else fy
        return _component(max_pts * held, reason="single-sided stable position")

    return _component(max_pts * min(fx, fy), reason="two-sided stable exposure")


def score_position_staleness(pos: Dict[str, Any], max_pts: float) -> ComponentResult:
    """Confidence that entry assumptions still hold. Healthy long-lived
    positions are NOT punished: grace period of STALE_GRACE_DAYS, then a
    gentle erosion to zero at STALE_ZERO_DAYS."""
    days = pos.get("days_open")
    if days is None:
        return _component(
            max_pts * MISSING_DEFAULT_STALENESS,
            missing=["days_open"],
            reason="position age missing; using fail-suspicious default",
        )
    try:
        days_val = float(days)
    except (TypeError, ValueError):
        return _component(
            max_pts * MISSING_DEFAULT_STALENESS,
            missing=["days_open"],
            reason="position age unparseable; using fail-suspicious default",
        )
    if days_val <= STALE_GRACE_DAYS:
        return _component(max_pts, reason=f"age {days_val:.1f}d <= {STALE_GRACE_DAYS}d grace")
    frac = 1.0 - (days_val - STALE_GRACE_DAYS) / (STALE_ZERO_DAYS - STALE_GRACE_DAYS)
    return _component(max_pts * clamp(frac, 0.0, 1.0),
                      reason=f"age {days_val:.1f}d; linear decay toward {STALE_ZERO_DAYS}d")


def _has_critical_missing(data_quality: Dict[str, List[str]]) -> bool:
    """True if any component flagged a missing input that should prevent a CLOSE."""
    return bool(data_quality.get("missing") or data_quality.get("unreliable"))


def score_position(pos: Dict[str, Any], pools_by_addr: Optional[Dict[str, Dict[str, Any]]] = None) -> PositionScore:
    pair_class, sym_x, sym_y = classify_pair(pos)
    pool: Optional[Dict[str, Any]] = (pools_by_addr or {}).get(pos.get("pool_address"))
    if pool is not None and pair_class == "unknown":
        pair_class, _, _ = classify_pair(pool)
    pool_name = pos.get("pool_name") or pos.get("name") or (pool or {}).get("name")

    data_quality: Dict[str, List[str]] = {"missing": [], "unreliable": [], "notes": []}

    if pair_class in ("off_universe", "unknown"):
        return PositionScore(
            position_id=pos.get("position_id") or pos.get("position_address"),
            pool=pool_name,
            pool_address=pos.get("pool_address"),
            pair_class=pair_class,
            pair=[sym_x, sym_y],
            lower_bound=pos.get("lower_bound"),
            upper_bound=pos.get("upper_bound"),
            fees_usd=pos.get("fees_usd"),
            score=0.0,
            components={},
            verdict="CLOSE",
            collect_fees=False,
            reason=f"{pair_class} pair: policy is stables/high-caps only",
            data_quality=data_quality,
            note="off-universe pair: policy is stables/high-caps only",
        )

    profile = POSITION_PROFILES[pair_class]
    w = profile["weights"]

    # Ensure we have a usable current_value_usd; this is needed for fee estimation.
    value, value_missing = _position_value(pos, pool or {})
    if value is not None:
        pos = dict(pos)
        pos["current_value_usd"] = value
    else:
        data_quality["missing"].extend(value_missing)

    results: Dict[str, ComponentResult] = {
        "range_status": score_position_range_status(pos, w["range_status"]),
        "fee_capture": score_position_fee_capture(pos, w["fee_capture"], pool or {}),
        "staleness": score_position_staleness(pos, w["staleness"]),
    }
    if "depeg_exposure" in w:
        results["depeg_exposure"] = score_position_depeg_exposure(
            pos, w["depeg_exposure"], sym_x, sym_y, pool or {})
    if "il_risk" in w:
        results["il_risk"] = score_position_il_risk(pos, w["il_risk"])

    components: Dict[str, float] = {}
    for name, result in results.items():
        components[name] = result["score"]
        for key in ("missing", "unreliable"):
            data_quality[key].extend(result.get(key, []))
        if result.get("reason"):
            data_quality["notes"].append(f"{name}: {result['reason']}")

    # Deduplicate lists after aggregation.
    data_quality["missing"] = sorted(set(data_quality["missing"]))
    data_quality["unreliable"] = sorted(set(data_quality["unreliable"]))

    total = round(sum(components.values()), 2)

    # Verdict logic: never CLOSE a position whose score is driven by missing data.
    if total < POSITION_THRESHOLDS["close"]:
        if _has_critical_missing(data_quality):
            verdict = "REVIEW"
        else:
            verdict = "CLOSE"
    elif total < POSITION_THRESHOLDS["review"]:
        verdict = "REVIEW"
    else:
        verdict = "HOLD"

    # Fee harvesting: worth the tx cost when fees clear ~1% of position value
    # (or the floor when value is unknown). Disabled when fee data is flagged
    # as unreliable.
    fees = pos.get("fees_usd")
    value_usd = pos.get("current_value_usd")
    collect = False
    if not _is_fee_unreliable(pos) and isinstance(fees, (int, float)) and fees >= 0:
        floor = COLLECT_MIN_USD
        if isinstance(value_usd, (int, float)) and value_usd > 0:
            floor = max(COLLECT_MIN_USD, COLLECT_PCT_OF_VALUE * float(value_usd))
        collect = fees >= floor

    reason = (
        f"{verdict}: score={total}; "
        + ", ".join(f"{k}={v:.2f}" for k, v in components.items())
    )
    if data_quality["missing"]:
        reason += f"; missing={','.join(data_quality['missing'])}"
    if data_quality["unreliable"]:
        reason += f"; unreliable={','.join(data_quality['unreliable'])}"

    return PositionScore(
        position_id=pos.get("position_id") or pos.get("position_address"),
        pool=pool_name,
        pool_address=pos.get("pool_address"),
        pair_class=pair_class,
        pair=[sym_x, sym_y],
        lower_bound=pos.get("lower_bound"),
        upper_bound=pos.get("upper_bound"),
        fees_usd=pos.get("fees_usd"),
        score=total,
        components=components,
        verdict=verdict,
        collect_fees=collect,
        reason=reason,
        data_quality=data_quality,
    )


# --------------------------------------------------------------------------
# Main cycle
# --------------------------------------------------------------------------
def validate_inputs(pools_dir: str, positions_dir: str, max_age_seconds: float) -> Dict[str, Any]:
    """Validate input files and return a dict with pools, positions, and errors.

    Returns:
        {
            "pools": <list>,
            "positions": <list>,
            "pool_path": <str or None>,
            "position_path": <str or None>,
            "failures": <list of error strings>,
        }
    """
    result: Dict[str, Any] = {
        "pools": [],
        "positions": [],
        "pool_path": None,
        "position_path": None,
        "failures": [],
    }

    pool_path = newest_file(pools_dir, "pool_scan")
    pos_path = newest_file(positions_dir, "position_scan")
    result["pool_path"] = pool_path
    result["position_path"] = pos_path

    if not pool_path or not is_fresh(pool_path, max_age_seconds):
        result["failures"].append("pool data missing or stale")
    else:
        try:
            pools = load_json(pool_path)
            if not isinstance(pools, list):
                result["failures"].append("pool data malformed (expected list)")
                pools = []
            result["pools"] = pools
        except Exception as exc:
            result["failures"].append(f"pool data unreadable: {exc}")

    if not pos_path or not is_fresh(pos_path, max_age_seconds):
        result["failures"].append("position data missing or stale")
    else:
        try:
            pos_data = load_json(pos_path)
            positions = (pos_data.get("positions", []) if isinstance(pos_data, dict)
                         else pos_data if isinstance(pos_data, list) else [])
            result["positions"] = positions
        except Exception as exc:
            result["failures"].append(f"position data unreadable: {exc}")

    return result


def run_cycle(pools_dir: str, positions_dir: str, max_age_seconds: float = 3900.0) -> Dict[str, Any]:
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime()),
        "sources": {},
        "pool_scores": [],
        "position_scores": [],
        "verdicts": [],
        "failures": [],
    }

    validated = validate_inputs(pools_dir, positions_dir, max_age_seconds)
    report["sources"] = {
        "pools": validated["pool_path"],
        "positions": validated["position_path"],
    }

    if validated["failures"]:
        report["failures"] = validated["failures"]
        return report

    pools = validated["pools"]
    positions = validated["positions"]

    pools_by_addr: Dict[str, Dict[str, Any]] = {}
    open_pools: Dict[str, float] = {}
    for pool in pools:
        s = score_pool(pool)
        # Include _pool in the dict so run_cycle can build OPEN signals.
        report["pool_scores"].append(s.to_dict(include_internal=True))
        if pool.get("pool_address"):
            pools_by_addr[pool["pool_address"]] = pool
        if s.verdict == "OPEN_CANDIDATE":
            open_pools[s.pool_address] = s.score
            report["verdicts"].append({
                "action": "OPEN_CANDIDATE",
                "pool": s.pool,
                "pool_address": s.pool_address,
                "dex": s.dex,
                "pair_class": s.pair_class,
                "score": s.score,
                "evidence": s.components,
                "reason": s.reason,
                "data_quality": s.data_quality,
                "_pool": s._pool,
            })

    for pos in positions:
        s = score_position(pos, pools_by_addr)
        # Include internal pool dict only for OPEN_CANDIDATE handling.
        verdict = s.verdict
        if verdict in {"CLOSE", "REVIEW"} and s.pool_address in open_pools:
            verdict = "REBALANCE"  # pool is still good: close and re-range
        if verdict == "HOLD" and s.collect_fees:
            verdict = "COLLECT_FEES"
        report["position_scores"].append(s.to_dict())
        report["verdicts"].append({
            "action": verdict,
            "position": s.position_id,
            "pool": s.pool,
            "pool_address": s.pool_address,
            "dex": pos.get("dex"),
            "pair_class": s.pair_class,
            "lower_bound": s.lower_bound,
            "upper_bound": s.upper_bound,
            "fees_usd": s.fees_usd,
            "score": s.score,
            "evidence": s.components,
            "reason": s.reason,
            "data_quality": s.data_quality,
            "note": s.note,
        })

    return report


def render_summary(report: Dict[str, Any]) -> str:
    lines = [f"Sheldon LP evaluation — {report['generated_at']}"]
    if report["failures"]:
        lines += [f"FAILURES: {'; '.join(report['failures'])}"]
        return "\n".join(lines)
    top = sorted(report["pool_scores"], key=lambda s: s["score"], reverse=True)[:5]
    lines.append("Top pool LP opportunities:")
    for s in top:
        lines.append(f"  {s['pool']:<22} [{s.get('pair_class') or '-':<17}] "
                     f"score={s['score']:>6.1f}  {s['verdict']}")
    if report["position_scores"]:
        lines.append("Open LP positions:")
        for s in report["position_scores"]:
            extra = " (fees ready to collect)" if s["collect_fees"] else "" if "collect_fees" in s else ""
            lines.append(f"  {str(s['pool']):<22} [{s.get('pair_class') or '-':<17}] "
                         f"score={s['score']:>6.1f}  {s['verdict']}{extra}")
    else:
        lines.append("Open LP positions: none")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sheldon LP scoring engine")
    ap.add_argument("--pools-dir", default="/data/missy-data/pool_screens")
    ap.add_argument("--positions-dir", default="/data/missy-data/position_scans")
    ap.add_argument("--json", action="store_true", help="full JSON report")
    args = ap.parse_args()
    report = run_cycle(args.pools_dir, args.positions_dir)
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(render_summary(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
