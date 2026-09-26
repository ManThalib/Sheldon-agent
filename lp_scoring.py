#!/usr/bin/env python3
"""Sheldon's LP scoring engine (v2).

Deterministic scoring for LP positions on Meteora DLMM, Raydium CLMM, and
Orca Whirlpool. Universe policy: stablecoin and high-cap pairs only — no
memecoins. Off-universe pools are hard-gated out before scoring.

Reads the newest Missy outputs (pool scan + position scan) and produces:
  - Pool LP Opportunity Score (0-100): is this pool worth an LP position?
  - Position Health Score (0-100): should an open LP position stay open?
  - Verdicts: OPEN_CANDIDATE / WATCH / IGNORE / HOLD / REVIEW / CLOSE /
    REBALANCE / COLLECT_FEES

v2 changes:
  - Configuration externalized to profiles.json (validated; fail-closed).
  - Open/close logic separated: missing position data caps the verdict at
    REVIEW. CLOSE requires real evidence from known inputs. Policy-based
    CLOSE (off-universe/unknown pair) is unaffected.
  - Expected fees estimated from pool realized APR when Missy does not
    provide them; fee_capture becomes a real earned/expected ratio.
  - IL estimate approximated from pool volatility scaled by range
    concentration when Missy does not provide il_estimate_pct.
  - Orca fee sentinels (u64::MAX) treated as "fees unknown", never zero.
  - Per-verdict human-readable reason; data-quality tracking.

Stdlib only. Run:  python3 lp_scoring.py [--pools-dir D] [--positions-dir D] [--json]
"""

import argparse
import glob
import json
import math
import os
import re
import sys
import time
from copy import deepcopy
from functools import lru_cache

# --------------------------------------------------------------------------
# Configuration — defaults mirror profiles.json. A valid profiles.json next
# to this file overrides any subset. Invalid file => fatal (fail-closed).
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "pool_profiles": {
        "stable_stable": {
            "weights": {"fee_yield": 35.0, "turnover": 15.0, "depth": 15.0,
                        "depeg_safety": 25.0, "volatility_fit": 10.0},
            "vol_peak_pct": 0.5,
            "apr_cap_pct": 100.0,
        },
        "stable_bluechip": {
            "weights": {"fee_yield": 30.0, "turnover": 20.0, "depth": 15.0,
                        "volatility_fit": 20.0, "depeg_safety": 15.0},
            "vol_peak_pct": 8.0,
            "apr_cap_pct": 300.0,
        },
        "bluechip_bluechip": {
            "weights": {"fee_yield": 30.0, "turnover": 20.0, "depth": 15.0,
                        "volatility_fit": 35.0},
            "vol_peak_pct": 10.0,
            "apr_cap_pct": 300.0,
        },
    },
    "position_profiles": {
        "stable_stable": {
            "weights": {"range_status": 35.0, "fee_capture": 25.0,
                        "depeg_exposure": 25.0, "staleness": 15.0},
        },
        "stable_bluechip": {
            "weights": {"range_status": 30.0, "fee_capture": 20.0,
                        "il_risk": 30.0, "staleness": 20.0},
        },
        "bluechip_bluechip": {
            "weights": {"range_status": 30.0, "fee_capture": 20.0,
                        "il_risk": 30.0, "staleness": 20.0},
        },
    },
    "pool_thresholds": {"open": 70.0, "watch": 55.0},
    "position_thresholds": {"close": 40.0, "review": 60.0},
    "constants": {
        "depth_min_usd": 50000.0,
        "depth_max_usd": 5000000.0,
        "depeg_zero_dist": 0.005,
        "stale_grace_days": 30.0,
        "stale_zero_days": 120.0,
        "collect_min_usd": 5.0,
        "collect_pct_of_value": 0.01,
        "turnover_max": 5.0,
        "unknown_credit": 0.5,
        "il_zero_pct": 10.0,
        "il_conc_full_width_ticks": 2000,
        "il_conc_max": 4.0,
        "fee_expect_max_days": 30.0,
    },
}

U64_MAX = (1 << 64) - 1


class ConfigError(Exception):
    """Raised when profiles.json is present but invalid."""


def _validate_config(cfg: dict) -> None:
    """Fail-closed validation. Raises ConfigError on any violation."""
    for section in ("pool_profiles", "position_profiles"):
        profiles = cfg.get(section)
        if not isinstance(profiles, dict) or not profiles:
            raise ConfigError(f"{section}: missing or empty")
        for name, prof in profiles.items():
            w = prof.get("weights") if isinstance(prof, dict) else None
            if not isinstance(w, dict) or not w:
                raise ConfigError(f"{section}.{name}: weights missing")
            total = sum(float(v) for v in w.values())
            if abs(total - 100.0) > 0.01:
                raise ConfigError(
                    f"{section}.{name}: weights sum to {total}, expected 100")
            for key, val in w.items():
                if float(val) < 0:
                    raise ConfigError(f"{section}.{name}.{key}: negative weight")

    pt = cfg.get("pool_thresholds") or {}
    if not ("watch" in pt and "open" in pt and float(pt["watch"]) <= float(pt["open"])):
        raise ConfigError("pool_thresholds: need watch <= open")
    st = cfg.get("position_thresholds") or {}
    if not ("close" in st and "review" in st and float(st["close"]) <= float(st["review"])):
        raise ConfigError("position_thresholds: need close <= review")

    c = cfg.get("constants") or {}
    if float(c.get("depth_min_usd", 0)) <= 0 or \
       float(c["depth_min_usd"]) >= float(c.get("depth_max_usd", 0)):
        raise ConfigError("constants: need 0 < depth_min_usd < depth_max_usd")
    if float(c.get("stale_grace_days", 0)) >= float(c.get("stale_zero_days", 0)):
        raise ConfigError("constants: need stale_grace_days < stale_zero_days")
    for key in ("depeg_zero_dist", "il_zero_pct", "fee_expect_max_days"):
        if float(c.get(key, 0)) <= 0:
            raise ConfigError(f"constants.{key}: must be > 0")


def _deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for key, val in (override or {}).items():
        if key.startswith("_"):
            continue
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = deepcopy(val)
    return out


def load_config(path: str) -> dict:
    """Load profiles.json over defaults. Missing file => defaults.
    Invalid JSON or failed validation => ConfigError."""
    if not os.path.exists(path):
        return deepcopy(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{path}: unreadable ({exc})") from exc
    if not isinstance(user, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    cfg = _deep_merge(DEFAULT_CONFIG, user)
    _validate_config(cfg)
    return cfg


_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "profiles.json")
_CONFIG = load_config(_CONFIG_PATH)


def set_config(cfg: dict) -> None:
    """Replace the active configuration (used by tests). Validates first."""
    _validate_config(cfg)
    global _CONFIG
    _CONFIG = cfg


def get_config() -> dict:
    return _CONFIG


# --------------------------------------------------------------------------
# Universe policy — stablecoins and high-caps only, no memes.
# --------------------------------------------------------------------------
STABLECOINS = {
    "USDC", "USDT", "USDS", "PYUSD", "DAI", "FDUSD", "EURC", "USDH", "USX",
}
HIGH_CAPS = {
    "SOL", "WSOL", "WBTC", "CBBTC", "WETH", "ETH",
    "JITOSOL", "JSOL", "MSOL", "BSOL", "JUP", "ZEC",
}

SUPPORTED_DEXES = {"meteora", "raydium", "orca"}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def newest_file(directory: str, prefix: str):
    paths = sorted(
        p for p in glob.glob(os.path.join(directory, prefix + "-*.json"))
        if not p.endswith((".failed", ".invalid"))
    )
    return paths[-1] if paths else None


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def is_fresh(path: str, max_age_seconds: float) -> bool:
    try:
        return (time.time() - os.path.getmtime(path)) <= max_age_seconds
    except OSError:
        return False


_PAIR_SPLIT_RE = re.compile(r"[-/]")


def pair_symbols(name: str):
    """Extract (symbol_x, symbol_y) from a pool name like 'SOL-USDC (bin 4)'."""
    if not name:
        return None, None
    base = name.split("(", 1)[0].strip()
    parts = [p.strip().upper() for p in _PAIR_SPLIT_RE.split(base) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


@lru_cache(maxsize=4096)
def _classify_cached(sym_x, sym_y, name):
    if not sym_x or not sym_y:
        name_x, name_y = pair_symbols(name)
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


def classify_pair(record: dict):
    """Return (pair_class, sym_x, sym_y). Cached per symbol/name triple."""
    sym_x = (record.get("token_x_symbol") or "").upper() or None
    sym_y = (record.get("token_y_symbol") or "").upper() or None
    name = record.get("name") or record.get("pool_name") or ""
    return _classify_cached(sym_x, sym_y, name)


# --------------------------------------------------------------------------
# Pool LP Opportunity Score
# --------------------------------------------------------------------------
def score_pool_fee_yield(pool: dict, max_pts: float, apr_cap_pct: float) -> float:
    """Realized fee APR only. fee/TVL is intentionally NOT added here: it is
    the same underlying signal as `turnover` (turnover x fee rate), and
    counting it twice double-rewards high-turnover pools."""
    apr_pct = float(pool.get("realized_fee_apr") or 0.0)
    return round(max_pts * clamp(apr_pct / apr_cap_pct, 0.0, 1.0), 2)


def score_pool_turnover(pool: dict, max_pts: float) -> float:
    """Window volume / TVL. `turnover_max` over the window maxes out."""
    tvl = float(pool.get("tvl") or 0.0)
    volume = float(pool.get("volume_window") or 0.0)
    if tvl <= 0:
        return 0.0
    cap = float(get_config()["constants"]["turnover_max"])
    return round(max_pts * clamp((volume / tvl) / cap, 0.0, 1.0), 2)


def score_pool_depth(pool: dict, max_pts: float) -> float:
    """Log scale between depth_min_usd and depth_max_usd."""
    c = get_config()["constants"]
    tvl_min = float(c["depth_min_usd"])
    tvl_max = float(c["depth_max_usd"])
    tvl = float(pool.get("tvl") or 0.0)
    if tvl < tvl_min:
        return 0.0
    frac = (math.log(tvl / tvl_min) / math.log(tvl_max / tvl_min))
    return round(max_pts * clamp(frac, 0.0, 1.0), 2)


def score_pool_volatility_fit(pool: dict, max_pts: float, peak_pct: float) -> float:
    """Triangular curve peaking at the profile's peak volatility."""
    vol = float(pool.get("volatility") or 0.0)
    if vol <= 0:
        return 0.0
    if vol >= 3.0 * peak_pct:
        return 0.0
    score = max_pts * (vol / peak_pct if vol <= peak_pct
                       else (3.0 * peak_pct - vol) / (2.0 * peak_pct))
    return round(clamp(score), 2)


def score_pool_depeg_safety(pool: dict, max_pts: float, sym_x, sym_y) -> float:
    """Distance of stable side(s) from $1. Worst side wins; a depegging
    stable drains the position into the bad token. Unknown price gets
    half credit (fail-suspicious: unseen pegs are not trusted pegs)."""
    zero_dist = float(get_config()["constants"]["depeg_zero_dist"])
    unknown_credit = float(get_config()["constants"]["unknown_credit"])
    sides = []
    for sym, key in ((sym_x, "token_x_price_usd"), (sym_y, "token_y_price_usd")):
        if sym not in STABLECOINS:
            continue
        price = pool.get(key)
        if price is None:
            sides.append(unknown_credit)  # unknown peg: partial credit
            continue
        dist = abs(float(price) - 1.0)
        sides.append(clamp(1.0 - dist / zero_dist, 0.0, 1.0))
    if not sides:  # no stable side: component not applicable
        return round(max_pts, 2)
    return round(max_pts * min(sides), 2)


def score_pool(pool: dict) -> dict:
    cfg = get_config()
    pair_class, sym_x, sym_y = classify_pair(pool)

    if pair_class in ("off_universe", "unknown"):
        return {"pool": pool.get("name"), "pool_address": pool.get("pool_address"),
                "dex": pool.get("dex"), "pair_class": pair_class,
                "pair": [sym_x, sym_y], "score": 0.0, "components": {},
                "reason": "off-universe pair: policy is stables/high-caps only",
                "verdict": "IGNORE", "_pool": pool}

    profile = cfg["pool_profiles"][pair_class]
    w = profile["weights"]
    reasons = []
    components = {
        "fee_yield": score_pool_fee_yield(pool, w["fee_yield"], profile["apr_cap_pct"]),
        "turnover": score_pool_turnover(pool, w["turnover"]),
        "depth": score_pool_depth(pool, w["depth"]),
        "volatility_fit": score_pool_volatility_fit(
            pool, w["volatility_fit"], profile["vol_peak_pct"]),
    }
    if "depeg_safety" in w:
        components["depeg_safety"] = score_pool_depeg_safety(
            pool, w["depeg_safety"], sym_x, sym_y)

    if float(pool.get("realized_fee_apr") or 0.0) <= 0:
        reasons.append("no realized fee APR in scan")
    if float(pool.get("volatility") or 0.0) <= 0:
        reasons.append("volatility unknown")

    total = round(clamp(sum(components.values())), 2)
    thr = cfg["pool_thresholds"]
    if total >= thr["open"]:
        verdict = "OPEN_CANDIDATE"
    elif total >= thr["watch"]:
        verdict = "WATCH"
    else:
        verdict = "IGNORE"
    if verdict == "OPEN_CANDIDATE":
        reasons.append(f"score {total} >= open threshold {thr['open']}")
    return {"pool": pool.get("name"), "pool_address": pool.get("pool_address"),
            "dex": pool.get("dex"), "pair_class": pair_class,
            "pair": [sym_x, sym_y], "score": total, "components": components,
            "reason": "; ".join(reasons) if reasons else "all inputs present",
            "verdict": verdict, "_pool": pool}


# --------------------------------------------------------------------------
# Position Health Score
#
# Every component returns (points, known) where `known` is False when the
# component ran on missing/unusable inputs. Missing data still contributes
# neutral credit to the score but caps the verdict at REVIEW: CLOSE requires
# evidence from real data, never from absent data.
# --------------------------------------------------------------------------
def _pos_price_bounds(pos: dict) -> tuple:
    lower = pos.get("lower_price")
    upper = pos.get("upper_price")
    current = pos.get("current_price")
    if lower is None or upper is None or current is None:
        return None, None, None
    return float(lower), float(upper), float(current)


def _fees_usd_or_none(pos: dict):
    """Fees in USD, or None when unknown.

    A raw sentinel (u64::MAX or larger) in fees_owed_raw means the decoder
    failed: fees are unknown, not zero. Missing fees_usd is unknown too.
    """
    fees = pos.get("fees_usd")
    raw = pos.get("fees_owed_raw") or []
    if any(isinstance(v, (int, float)) and v >= U64_MAX for v in raw):
        return None
    if fees is None:
        return None
    return float(fees)


def _range_concentration(pos: dict) -> float:
    """Concentration multiplier from range width in ticks/bins.

    A range as wide as il_conc_full_width_ticks behaves like a full-range
    position (1.0x); narrower ranges amplify IL up to il_conc_max."""
    c = get_config()["constants"]
    full = float(c["il_conc_full_width_ticks"])
    conc_max = float(c["il_conc_max"])
    lower = pos.get("lower_bound")
    upper = pos.get("upper_bound")
    if lower is None or upper is None:
        return conc_max  # unknown width: assume concentrated (conservative)
    width = float(upper) - float(lower)
    if width <= 0:
        return conc_max
    return clamp(full / width, 1.0, conc_max)


def estimate_il_pct(pos: dict, pool: dict, days_open) -> tuple:
    """Return (il_estimate_pct, source).

    Uses Missy's il_estimate_pct when present. Otherwise approximates from
    pool daily volatility using the concentrated-LP quadratic loss
    approximation: IL% ≈ conc * (vol_daily/100)^2 / 8 * 100 * days.
    """
    reported = pos.get("il_estimate_pct")
    if reported is not None:
        return float(reported), "reported"
    vol = float((pool or {}).get("volatility") or 0.0)
    if vol <= 0 or days_open is None or days_open <= 0:
        return None, "unknown"
    conc = _range_concentration(pos)
    il = conc * (vol / 100.0) ** 2 / 8.0 * 100.0 * float(days_open)
    return il, "estimated"


def estimate_expected_fees(pos: dict, pool: dict, fees_usd_known: bool) -> tuple:
    """Return (expected_fees_usd, source).

    Uses Missy's expected_fees_usd when present. Otherwise estimates from
    position value and pool realized APR over min(days_open, cap):
        expected = value * apr/100 * min(days, fee_expect_max_days) / 365
    """
    expected = pos.get("expected_fees_usd")
    if expected is not None and float(expected) > 0:
        return float(expected), "reported"
    value = pos.get("current_value_usd")
    apr = float((pool or {}).get("realized_fee_apr") or 0.0)
    days = pos.get("days_open")
    cap = float(get_config()["constants"]["fee_expect_max_days"])
    if value is None or float(value) <= 0 or apr <= 0 or days is None:
        return None, "unknown"
    days = min(float(days), cap)
    return float(value) * (apr / 100.0) * days / 365.0, "estimated"


def score_position_range_status(pos: dict, max_pts: float) -> tuple:
    """In-range earns fees; distance-to-boundary discounts positions about
    to flip single-sided. Missing data scores partial credit and flags
    unknown (fail-closed on data quality, not on the position)."""
    unknown_credit = float(get_config()["constants"]["unknown_credit"])
    lower, upper, current = _pos_price_bounds(pos)
    if lower is not None and upper > lower:
        if not (lower <= current <= upper):
            return 0.0, True
        span = upper - lower
        edge_dist = min(current - lower, upper - current) / (span / 2.0)
        edge_frac = clamp((edge_dist - 0.0) / 0.5, 0.0, 1.0)
        return round(max_pts * (0.6 + 0.4 * edge_frac), 2), True
    if "in_range" in pos and pos["in_range"] is not None:
        return (max_pts, True) if pos["in_range"] else (0.0, True)
    return round(max_pts * unknown_credit, 2), False


def score_position_fee_capture(pos: dict, max_pts: float, pool: dict) -> tuple:
    """Fees earned vs expected. Expected fees are estimated from the pool's
    realized APR when Missy does not provide them."""
    unknown_credit = float(get_config()["constants"]["unknown_credit"])
    fees = _fees_usd_or_none(pos)
    if fees is None:
        return round(max_pts * unknown_credit, 2), False
    expected, source = estimate_expected_fees(pos, pool, fees_usd_known=True)
    if expected is None or expected <= 0:
        return round(max_pts * unknown_credit, 2), False
    ratio = fees / expected
    return round(max_pts * clamp(ratio, 0.0, 1.0), 2), True


def score_position_il_risk(pos: dict, max_pts: float, pool: dict) -> tuple:
    """IL exposure for bluechip pairs, estimated from volatility when
    Missy does not provide an estimate."""
    unknown_credit = float(get_config()["constants"]["unknown_credit"])
    zero_pct = float(get_config()["constants"]["il_zero_pct"])
    days = pos.get("days_open")
    il, _source = estimate_il_pct(pos, pool, days)
    if il is None:
        return round(max_pts * unknown_credit, 2), False
    return round(max_pts * clamp(1.0 - float(il) / zero_pct), 2), True


def score_position_depeg_exposure(pos: dict, max_pts: float, sym_x, sym_y,
                                  pool: dict) -> tuple:
    """Stable/stable IL is depeg risk. Full credit when both pegs hold and
    the position is two-sided; penalized when single-sided in a depegged
    token (the exit already happened, against you)."""
    zero_dist = float(get_config()["constants"]["depeg_zero_dist"])
    unknown_credit = float(get_config()["constants"]["unknown_credit"])

    def peg_frac(sym, pool_key):
        if sym not in STABLECOINS:
            return 1.0
        price = None
        if pool:
            price = pool.get(pool_key)
        if price is None:
            return unknown_credit  # unseen peg: partial credit, flag below
        return clamp(1.0 - abs(float(price) - 1.0) / zero_dist, 0.0, 1.0)

    known = True
    for sym in (sym_x, sym_y):
        if sym in STABLECOINS and (not pool or pool.get(
                "token_x_price_usd" if sym == sym_x else "token_y_price_usd") is None):
            known = False

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
        held = fx if ui_x > 0 else fy  # you're 100% in one token
        return round(max_pts * held, 2), known
    return round(max_pts * min(fx, fy), 2), known


def score_position_staleness(pos: dict, max_pts: float) -> tuple:
    """Confidence that entry assumptions still hold. Healthy long-lived
    positions are NOT punished: grace period of stale_grace_days, then a
    gentle erosion to zero at stale_zero_days."""
    c = get_config()["constants"]
    unknown_credit = float(c["unknown_credit"])
    grace = float(c["stale_grace_days"])
    zero = float(c["stale_zero_days"])
    days = pos.get("days_open")
    if days is None:
        return round(max_pts * unknown_credit, 2), False  # unverifiable age
    days = float(days)
    if days <= grace:
        return round(max_pts, 2), True
    frac = 1.0 - (days - grace) / (zero - grace)
    return round(max_pts * clamp(frac, 0.0, 1.0), 2), True


def score_position(pos: dict, pools_by_addr: dict = None) -> dict:
    cfg = get_config()
    pair_class, sym_x, sym_y = classify_pair(pos)
    pool = (pools_by_addr or {}).get(pos.get("pool_address"))
    if pool is not None and pair_class == "unknown":
        pair_class, _, _ = classify_pair(pool)
    pool_name = pos.get("pool_name") or pos.get("name") or (pool or {}).get("name")

    if pair_class in ("off_universe", "unknown"):
        return {"position_id": pos.get("position_id") or pos.get("position_address"),
                "pool": pool_name,
                "pool_address": pos.get("pool_address"),
                "pair_class": pair_class, "pair": [sym_x, sym_y],
                "score": 0.0, "components": {}, "verdict": "CLOSE",
                "collect_fees": False,
                "data_quality": {"unknown_components": [], "policy_close": True},
                "reason": "off-universe pair: policy is stables/high-caps only",
                "note": "off-universe pair: policy is stables/high-caps only"}

    profile = cfg["position_profiles"][pair_class]
    w = profile["weights"]
    components = {}
    unknown_parts = []

    def add(name, result):
        pts, known = result
        components[name] = pts
        if not known:
            unknown_parts.append(name)

    add("range_status", score_position_range_status(pos, w["range_status"]))
    add("fee_capture", score_position_fee_capture(pos, w["fee_capture"], pool))
    add("staleness", score_position_staleness(pos, w["staleness"]))
    if "depeg_exposure" in w:
        add("depeg_exposure", score_position_depeg_exposure(
            pos, w["depeg_exposure"], sym_x, sym_y, pool))
    if "il_risk" in w:
        add("il_risk", score_position_il_risk(pos, w["il_risk"], pool))

    total = round(clamp(sum(components.values())), 2)
    thr = cfg["position_thresholds"]

    # --- Open/close separation -------------------------------------------
    # CLOSE requires known data for every component. Any unknown input caps
    # the verdict at REVIEW: missing data is a data problem, not evidence.
    if total < thr["close"] and unknown_parts:
        verdict = "REVIEW"
        reason = (f"score {total} < close threshold {thr['close']} but "
                  f"unknown: {', '.join(unknown_parts)}")
    elif total < thr["close"]:
        verdict = "CLOSE"
        reason = f"score {total} < close threshold {thr['close']}"
    elif total < thr["review"]:
        verdict = "REVIEW"
        reason = f"score {total} in review band [{thr['close']}, {thr['review']})"
    else:
        verdict = "HOLD"
        reason = f"score {total} >= hold threshold {thr['review']}"
    if unknown_parts and verdict != "REVIEW":
        reason += f" (unknown: {', '.join(unknown_parts)})"

    # Fee harvesting: worth the tx cost when fees clear collect_pct_of_value
    # of position value (or the floor when value is unknown). Unknown fees
    # never trigger a collection signal.
    fees = _fees_usd_or_none(pos)
    value = pos.get("current_value_usd")
    floor = float(cfg["constants"]["collect_min_usd"])
    if isinstance(value, (int, float)) and value > 0:
        floor = max(floor, float(cfg["constants"]["collect_pct_of_value"]) * float(value))
    collect = fees is not None and fees >= floor

    return {"position_id": pos.get("position_id") or pos.get("position_address"),
            "pool": pool_name,
            "pool_address": pos.get("pool_address"),
            "pair_class": pair_class, "pair": [sym_x, sym_y],
            "lower_bound": pos.get("lower_bound"),
            "upper_bound": pos.get("upper_bound"),
            "fees_usd": fees,
            "score": total, "components": components, "verdict": verdict,
            "collect_fees": collect,
            "data_quality": {"unknown_components": unknown_parts,
                             "policy_close": False},
            "reason": reason,
            "note": reason}


# --------------------------------------------------------------------------
# Main cycle
# --------------------------------------------------------------------------
def run_cycle(pools_dir: str, positions_dir: str, max_age_seconds: float = 3900.0) -> dict:
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime()),
              "sources": {}, "pool_scores": [], "position_scores": [],
              "verdicts": [], "failures": []}

    pool_path = newest_file(pools_dir, "pool_scan")
    pos_path = newest_file(positions_dir, "position_scan")
    report["sources"] = {"pools": pool_path, "positions": pos_path}

    pools, positions = [], []
    if not pool_path or not is_fresh(pool_path, max_age_seconds):
        report["failures"].append("pool data missing or stale")
    else:
        pools = load_json(pool_path)
        if not isinstance(pools, list):
            report["failures"].append("pool data malformed (expected list)")
            pools = []
    if not pos_path or not is_fresh(pos_path, max_age_seconds):
        report["failures"].append("position data missing or stale")
    else:
        pos_data = load_json(pos_path)
        positions = (pos_data.get("positions", []) if isinstance(pos_data, dict)
                     else pos_data if isinstance(pos_data, list) else [])
    if report["failures"]:
        return report

    pools_by_addr = {}
    open_pools = {}
    for pool in pools:
        s = score_pool(pool)
        report["pool_scores"].append(s)
        if pool.get("pool_address"):
            pools_by_addr[pool["pool_address"]] = pool
        if s["verdict"] == "OPEN_CANDIDATE":
            open_pools[s["pool_address"]] = s["score"]
            report["verdicts"].append({
                "action": "OPEN_CANDIDATE", "pool": s["pool"],
                "pool_address": s["pool_address"], "dex": s.get("dex"),
                "pair_class": s.get("pair_class"), "score": s["score"],
                "evidence": s["components"], "reason": s.get("reason"),
                "_pool": s.get("_pool")})

    for pos in positions:
        s = score_position(pos, pools_by_addr)
        report["position_scores"].append(s)
        verdict = s["verdict"]
        if verdict in {"CLOSE", "REVIEW"} and s["pool_address"] in open_pools:
            verdict = "REBALANCE"  # pool is still good: close and re-range
        if verdict == "HOLD" and s["collect_fees"]:
            verdict = "COLLECT_FEES"
        report["verdicts"].append({
            "action": verdict, "position": s["position_id"], "pool": s["pool"],
            "pool_address": s.get("pool_address"), "dex": pos.get("dex"),
            "pair_class": s.get("pair_class"),
            "lower_bound": s.get("lower_bound"), "upper_bound": s.get("upper_bound"),
            "fees_usd": s.get("fees_usd"), "score": s["score"],
            "evidence": s["components"],
            "reason": s.get("reason"),
            "note": s.get("note")})

    return report


def render_summary(report: dict) -> str:
    lines = [f"Sheldon LP evaluation — {report['generated_at']}"]
    if report["failures"]:
        lines += [f"FAILURES: {'; '.join(report['failures'])}"]
        return "\n".join(lines)
    top = sorted(report["pool_scores"], key=lambda s: s["score"], reverse=True)[:5]
    lines.append("Top pool LP opportunities:")
    for s in top:
        lines.append(f"  {str(s['pool']):<22} [{s.get('pair_class') or '-':<17}] "
                     f"score={s['score']:>6.1f}  {s['verdict']}")
    if report["position_scores"]:
        lines.append("Open LP positions:")
        for s in report["position_scores"]:
            extra = " (fees ready to collect)" if s["collect_fees"] else ""
            lines.append(f"  {str(s['pool']):<22} [{s.get('pair_class') or '-':<17}] "
                         f"score={s['score']:>6.1f}  {s['verdict']}{extra}")
            if s.get("reason"):
                lines.append(f"    reason: {s['reason']}")
    else:
        lines.append("Open LP positions: none")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sheldon LP scoring engine")
    ap.add_argument("--pools-dir", default="/data/missy-data/pool_screens")
    ap.add_argument("--positions-dir", default="/data/missy-data/position_scans")
    ap.add_argument("--json", action="store_true", help="full JSON report")
    args = ap.parse_args()
    try:
        report = run_cycle(args.pools_dir, args.positions_dir)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 1
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(render_summary(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
