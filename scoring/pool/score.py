"""Pool LP Opportunity Score functions.

These are the individual component calculators and the main pool scoring
entry points (score_pool, score_pool_missy).
"""


import math
from scoring.config import get_config
from scoring.helpers import clamp


def _percentile_rank(sorted_values, value):
    """Thin wrapper around dynamic.percentile_rank to avoid circular imports."""
    from scoring import dynamic
    return dynamic.percentile_rank(sorted_values, value)


U64_MAX = (1 << 64) - 1

STABLECOINS = {"USDC", "USDT", "USDG"}
HIGH_CAPS = {
    "SOL", "WSOL",
    "CBT", "WBTC", "CBBTC",
    "ETH", "WETH",
    "XRP", "cbRXP",
    "ZEC",
    "TAO",
    "XMR",
    "BMT",
    "QNT",
    "VIRTUAL",
    "ZBCN",
    "GRASS"
}


def _is_stable(sym: str) -> bool:
    return sym in STABLECOINS


def _is_high_cap(sym: str) -> bool:
    return sym in HIGH_CAPS


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


def _missy_components(pool: dict) -> dict:
    """Map Missy's score_breakdown keys to Sheldon-style component names."""
    breakdown = pool.get("score_breakdown") or {}
    mapping = {
        "yield_score": "fee_yield",
        "depth_score": "depth",
        "efficiency_score": "turnover",
        "risk_score": "volatility_fit",
    }
    out = {}
    for src, dst in mapping.items():
        val = breakdown.get(src)
        if val is not None:
            out[dst] = float(val)
    return out


def score_pool_missy(pool: dict, ctx: dict = None) -> dict:
    """Use Missy's default score directly, preserving local verdict thresholds.

    Falls back to Sheldon's local scoring when the pool record does not
    yet contain a Missy score (legacy scans or tests).

    Policy gates are NOT outsourced: the universe hard-gate applies here
    exactly as in score_pool (a high Missy score never opens an
    off-universe pool), and Missy's own eligible=false discovery gates
    block OPEN_CANDIDATE (score kept for audit, verdict capped at IGNORE).
    """
    if "score" not in pool:
        from scoring.pool.score import score_pool
        return score_pool(pool, ctx)
    # Missy owns classification; trust its tag when present, else local.
    from scoring.universe import classify_pair
    pair_class, sym_x, sym_y = classify_pair(pool)
    if pool.get("pair_class"):
        pair_class = pool["pair_class"]
    score = float(pool.get("score") or 0.0)
    from scoring.config import load_scoring_policy

    policy = load_scoring_policy()

    if pair_class in ("off_universe", "unknown"):
        return {"pool": pool.get("name"), "pool_address": pool.get("pool_address"),
                "dex": pool.get("dex"), "pair_class": pair_class,
                "pair": [sym_x, sym_y], "score": 0.0, "components": {},
                "reason": "off-universe pair: policy is stables/high-caps only",
                "verdict": "IGNORE", "_pool": pool,
                "score_policy": {"source": policy["source"],
                                 "version": policy["version"]},
                "dynamic": {"regime": None, "thresholds": "static"}}

    thresholds = (ctx or {}).get("thresholds") or get_config()["pool_thresholds"]
    verdict = pool_verdict(score, thresholds)
    reasons = []
    if verdict == "OPEN_CANDIDATE" and pool.get("eligible") is False:
        verdict = "IGNORE"
        reasons.append(
            f"score {score} >= open threshold but Missy gate rejected: "
            f"{pool.get('rejected_reason') or 'eligible=false'}")
    elif verdict == "OPEN_CANDIDATE":
        reasons.append(f"Missy score {score} >= open threshold {thresholds['open']}")
    return {
        "pool": pool.get("name"),
        "pool_address": pool.get("pool_address"),
        "dex": pool.get("dex"),
        "pair_class": pair_class,
        "pair": [sym_x, sym_y],
        "score": score,
        "components": _missy_components(pool),
        "score_policy": {"source": policy["source"], "version": policy["version"]},
        "reason": "; ".join(reasons) if reasons else "Missy default score consumed",
        "verdict": verdict,
        "_pool": pool,
        "dynamic": {"regime": None, "thresholds": "static"},
    }


def pool_verdict(score: float, thresholds: dict) -> str:
    """Map a pool score to a verdict given thresholds."""
    if score >= thresholds["open"]:
        return "OPEN_CANDIDATE"
    if score >= thresholds["watch"]:
        return "WATCH"
    return "IGNORE"


def _maybe_blend(name: str, abs_pts: float, max_pts: float, raw_value: float,
                 pair_class: str, ctx: dict) -> float:
    """Blend absolute component score with rolling percentile, if available."""
    if not ctx or not max_pts:
        return abs_pts
    from scoring.dynamic import percentile_rank
    norms = (ctx.get("norms") or {}).get(pair_class)
    if norms is None or name not in norms:
        return abs_pts
    rank = _percentile_rank(norms[name], raw_value)
    alpha = float(ctx.get("blend_alpha", 0.7))
    norm_abs = abs_pts / max_pts if max_pts else 0.0
    return round(max_pts * (alpha * rank + (1.0 - alpha) * norm_abs), 2)


def score_pool(pool: dict, ctx: dict = None) -> dict:
    """Main pool LP opportunity score.

    Combines component scores (fee_yield, turnover, depth, volatility_fit,
    depeg_safety) using profile weights and determines a verdict based on
    thresholds.
    """
    from scoring.config import get_config
    from scoring.universe import classify_pair
    cfg = get_config()
    pair_class, sym_x, sym_y = classify_pair(pool)
    static_thr = cfg["pool_thresholds"]
    adaptive_thr = (ctx or {}).get("thresholds")
    thresholds = adaptive_thr if adaptive_thr else static_thr
    regime_name = ((ctx or {}).get("regime") or {}).get("name")

    if pair_class in ("off_universe", "unknown"):
        return {"pool": pool.get("name"), "pool_address": pool.get("pool_address"),
                "dex": pool.get("dex"), "pair_class": pair_class,
                "pair": [sym_x, sym_y], "score": 0.0, "components": {},
                "reason": "off-universe pair: policy is stables/high-caps only",
                "verdict": "IGNORE", "_pool": pool,
                "dynamic": {"regime": regime_name, "thresholds": "static"}}

    profile = cfg["pool_profiles"][pair_class]
    w = (ctx or {}).get("weights", {}).get(pair_class) or profile["weights"]
    reasons = []

    fee_abs = score_pool_fee_yield(pool, w["fee_yield"], profile["apr_cap_pct"])
    turnover_abs = score_pool_turnover(pool, w["turnover"])
    turnover_raw = 0.0
    if float(pool.get("tvl") or 0.0) > 0:
        turnover_raw = float(pool.get("volume_window") or 0.0) / float(pool.get("tvl"))

    components = {
        "fee_yield": _maybe_blend("fee_yield", fee_abs, w["fee_yield"],
                                   float(pool.get("realized_fee_apr") or 0.0),
                                   pair_class, ctx),
        "turnover": _maybe_blend("turnover", turnover_abs, w["turnover"],
                                 turnover_raw, pair_class, ctx),
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
    verdict = pool_verdict(total, thresholds)
    if verdict == "OPEN_CANDIDATE":
        reasons.append(f"score {total} >= open threshold {thresholds['open']}")
    return {"pool": pool.get("name"), "pool_address": pool.get("pool_address"),
            "dex": pool.get("dex"), "pair_class": pair_class,
            "pair": [sym_x, sym_y], "score": total, "components": components,
            "reason": "; ".join(reasons) if reasons else "all inputs present",
            "verdict": verdict, "_pool": pool,
            "dynamic": {"regime": regime_name,
                        "thresholds": ("adaptive" if adaptive_thr else "static")}}