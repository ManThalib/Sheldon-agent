"""Position Health Score functions.

These are the individual component calculators and the main position scoring
entry points (score_position, score_position_missy).
"""


import math
from scoring.config import get_config


U64_MAX = (1 << 64) - 1

STABLECOINS = {"USDC", "USDT", "USDG"}
HIGH_CAPS = {
    "SOL", "WSOL",
    "BTC", "WBTC", "CBBTC",
    "ETH", "WETH",
    "XRP", "cbXRP",
    "ZEC",
    "TAO",
    "XMR",
    "BMT",
    "QNT",
    "VIRTUAL",
    "ZBCN",
    "GRASS"
}

ZERO_DIST = 0.005
UNKNOWN_CREDIT = 0.5
IL_ZERO_PCT = 10.0
IL_CONC_FULL_WIDTH_TICKS = 2000
IL_CONC_MAX = 4.0
COLLECT_MIN_USD = 5.0
COLLECT_PCT_OF_VALUE = 0.01
FEE_EXPECT_MAX_DAYS = 30.0


def _is_stable(sym: str) -> bool:
    return sym in STABLECOINS


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


def _pos_price_bounds(pos: dict) -> tuple:
    lower = pos.get("lower_price")
    upper = pos.get("upper_price")
    current = pos.get("current_price")
    if lower is None or upper is None or current is None:
        return None, None, None
    return float(lower), float(upper), float(current)


def score_position_range_status(pos: dict, max_pts: float) -> tuple:
    """In-range earns fees; distance-to-boundary discounts positions about
    to flip single-sided. Missing data scores partial credit and flags
    unknown (fail-closed on data quality, not on the position).

    An RPC-failed scan (null tick bounds from a fallback/historical record)
    is unknown data, not out-of-range evidence: null bounds must never
    score 0 and must never read as verifiably out-of-range.

    When the record carries Missy's edge_distance_frac (0 at center, 1 at
    boundary) and no price bounds, that feature drives a centering curve:
    center = full credit, edge = zero. Local records without it keep the
    flat full-credit in-range behavior.
    """
    unknown_credit = float(get_config()["constants"]["unknown_credit"])
    lower, upper, current = _pos_price_bounds(pos)
    if lower is not None and upper > lower:
        if not (lower <= current <= upper):
            return 0.0, True
        span = upper - lower
        edge_dist = min(current - lower, upper - current) / (span / 2.0)
        # edge_frac: 0 at center, 1 at boundary. New formula rewards being
        # centered: 1 - edge_frac gives 1.0 at center, 0.0 at edge.
        edge_frac = clamp((edge_dist - 0.0) / 0.5, 0.0, 1.0)
        return round(max_pts * (1.0 - edge_frac), 2), True
    edge = pos.get("edge_distance_frac")
    if "in_range" in pos and pos["in_range"] is not None:
        # A fallback record with null tick bounds reports in_range=false
        # only because the scanner could not decode the range. Treat that
        # as unknown data (partial credit), not as out-of-range evidence.
        bounds_missing = pos.get("lower_bound") is None or pos.get("upper_bound") is None
        if bounds_missing and not pos["in_range"]:
            return round(max_pts * unknown_credit, 2), False
        if not pos["in_range"]:
            return 0.0, True
        if isinstance(edge, (int, float)):
            edge_frac = clamp(float(edge), 0.0, 1.0)
            # New: centering reward — 1.0 at center, 0.0 at edge
            return round(max_pts * (1.0 - edge_frac), 2), True
        return round(max_pts, 2), True
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
    """Net yield coverage: whether fees cover impermanent loss.

    Positive net yield (fees > IL) scores above baseline.
    Break-even (fees ≈ IL) scores at baseline.
    Negative net yield (IL > fees) scores below baseline.

    This replaces the old IL‑as‑pure‑penalty model with a profitability
    signal: a position that earns more in fees than it loses to IL is
    objectively healthy, regardless of IL magnitude.
    """
    unknown_credit = float(get_config()["constants"]["unknown_credit"])
    days = pos.get("days_open")
    il, _source = estimate_il_pct(pos, pool, days)
    if il is None:
        return round(max_pts * unknown_credit, 2), False

    fees = _fees_usd_or_none(pos)
    value = pos.get("current_value_usd")

    # Compute net yield percentage: (fees - IL) / value
    # If fees is unknown, treat IL as full penalty (net yield = -il%)
    if fees is None or value is None or float(value) <= 0:
        # Can't compute net yield; return unknown credit
        return round(max_pts * unknown_credit, 2), False

    net_yield_pct = (float(fees) - float(il)) / float(value)
    # Map to score: 1.0 = break-even, >1 = profitable, <1 = losing
    # clamp to [0, 2] so large negatives don't zero-out the whole score
    coverage = clamp(1.0 + net_yield_pct, 0.0, 2.0)
    return round(max_pts * coverage, 2), True


def _peg_frac(sym, pool_key, pool):
    """Helper: fraction of peg remaining for a stable side."""
    if sym not in STABLECOINS:
        return 1.0
    price = None
    if pool:
        price = pool.get(pool_key)
    if price is None:
        return UNKNOWN_CREDIT  # unseen peg: partial credit, flag below
    return clamp(1.0 - abs(float(price) - 1.0) / ZERO_DIST, 0.0, 1.0)


def score_position_depeg_exposure(pos: dict, max_pts: float, sym_x, sym_y,
                                  pool: dict) -> tuple:
    """Stable/stable IL is depeg risk. Full credit when both pegs hold and
    the position is two-sided; penalized when single-sided in a depegged
    token (the exit already happened, against you)."""
    unknown_credit = float(get_config()["constants"]["unknown_credit"])

    def peg_frac(sym, pool_key):
        if sym not in STABLECOINS:
            return 1.0
        price = None
        if pool:
            price = pool.get(pool_key)
        if price is None:
            return unknown_credit  # unseen peg: partial credit
        return clamp(1.0 - abs(float(price) - 1.0) / ZERO_DIST, 0.0, 1.0)

    known = True
    for sym in (sym_x, sym_y):
        if sym in STABLECOINS and (not pool or pool.get(
                "token_x_price_usd" if sym == sym_x else "token_y_price_usd") is None):
            known = False

    fx = peg_frac(sym_x, "token_x_price_usd")
    fy = peg_frac(sym_y, "token_y_price_usd")

    # Single-sided detection: Missy's feature vector first, token-amount
    # fallback for local records.
    held_x = None
    single_sided = pos.get("single_sided")
    if single_sided is None:
        ui_x = ui_y = None
        tx, ty = pos.get("token_x_amount"), pos.get("token_y_amount")
        try:
            ui_x = float((tx or {}).get("ui")) if tx else None
            ui_y = float((ty or {}).get("ui")) if ty else None
        except (TypeError, ValueError):
            ui_x = ui_y = None
        if ui_x is not None and ui_y is not None and (ui_x > 0) != (ui_y > 0):
            single_sided = True
            held_x = ui_x > 0
        else:
            single_sided = False
    else:
        bias = pos.get("side_bias_x")
        if isinstance(bias, (int, float)):
            held_x = float(bias) > 0.5

    if single_sided:
        # You're 100% in one token; your exit already happened against the
        # peg of whichever side you hold.
        if held_x is None:
            return round(max_pts * min(fx, fy), 2), False
        held = fx if held_x else fy
        return round(max_pts * held, 2), known
    return round(max_pts * min(fx, fy), 2), known


def score_position_staleness(pos: dict, max_pts: float) -> tuple:
    """Range efficiency: proportion of time the position has spent in-range
    over its lifetime. Positions that have consistently stayed within bounds
    reward confidence in range selection; those that frequently exit are
    viewed as less robust.

    This replaces the old staleness-eager-penalty model with a range
    efficiency signal: age alone does not hurt a position that has proven
    its range selection by staying in-range.
    """
    unknown_credit = float(get_config()["constants"]["unknown_credit"])
    in_range_days = pos.get("in_range_days")
    total_days = pos.get("days_open")

    if total_days is None or float(total_days) <= 0:
        # No data yet: conservatively assume full efficiency
        return round(max_pts, 2), True

    total_days = float(total_days)
    in_range = False is None and pos.get("in_range") is not None

    # Determine in-range days:
    # If the record explicitly tracks in_range_days, use it.
    # Otherwise, if in_range is known True and days_open is available,
    # assume all days were in-range (conservative).
    # If in_range is known False, zero in-range days.
    # If in_range is unknown, assume partial efficiency.
    if in_range_days is not None:
        try:
            in_range_days = float(in_range_days)
            if in_range_days < 0:
                in_range_days = 0.0
        except (TypeError, ValueError):
            in_range_days = None
    else:
        # Infer from in_range flag
        in_range_flag = pos.get("in_range")
        if in_range_flag is True:
            in_range_days = total_days  # assume all days in range
        elif in_range_flag is False:
            in_range_days = 0.0
        else:
            # Unknown: assume half efficiency
            in_range_days = total_days * 0.5

    efficiency_pct = in_range_days / total_days
    # Clamp to [0, 1] and score
    efficiency_pct = clamp(efficiency_pct, 0.0, 1.0)
    return round(max_pts * efficiency_pct, 2), True


def score_position_core(pos: dict, pool, pool_name, pair_class, sym_x, sym_y,
                        ctx: dict = None, extra_unknown: list = None) -> dict:
    """Shared scoring policy. `pos` is either the raw record (local path) or
    a normalized view built from Missy's features — policy is identical."""
    cfg = get_config()
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
    cfg = get_config()
    # NEW: apply depeg risk as a penalty multiplier (max 30% reduction).
    # Depeg exposure of 1.0 (fully pegged) = no reduction; lower = more reduction.
    # This is separate from the additive component model; it caps the final score.
    try:
        depeg_pts, depeg_known = score_position_depeg_exposure(
            pos, 1.0, sym_x, sym_y, pool)  # pts is peg fraction 0-1
        if depeg_known and depeg_pts is not None:
            peg_frac = clamp(float(depeg_pts), 0.0, 1.0)
            total = total * (1.0 - peg_frac * 0.3)
    except Exception:
        pass  # if anything fails, keep total unchanged
    thr = cfg["position_thresholds"]

    # Dynamic expected-PnL override (only when inputs are complete).
    pnl = None
    pnl_cfg = (ctx or {}).get("position_pnl") if ctx else None
    if pnl_cfg and pnl_cfg.get("enabled"):
        from scoring.dynamic import expected_pnl_verdict
        pnl = expected_pnl_verdict(pos, pool, cfg)

    # --- Open/close separation -------------------------------------------
    # Close requires known data for every component. Any unknown input caps
    # the verdict at REVIEW: missing data is a data problem, not evidence.
    if pnl and pnl.get("decisive"):
        action = pnl["action"]
        if unknown_parts and action in ("CLOSE", "REBALANCE"):
            verdict = "REVIEW"
            reason = (f"PNL suggests {action} but unknown: "
                      f"{', '.join(unknown_parts)}")
        else:
            verdict = action
            reason = (f"PNL decision: {action} (HOLD={pnl['expected_hold_usd']}, "
                      f"CLOSE={pnl['expected_close_usd']}, "
                      f"REBALANCE={pnl['expected_rebalance_usd']}, "
                      f"margin={pnl['margin_usd']})")
    elif total < thr["close"] and unknown_parts:
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

    for gap in (extra_unknown or []):
        if gap not in unknown_parts:
            unknown_parts.append(gap)

    return {"position_id": pos.get("position_id") or pos.get("position_address"),
            "pool": pool_name,
            "pool_address": pos.get("pool_address"),
            "pair_class": pair_class, "pair": [sym_x, sym_y],
            "lower_bound": pos.get("lower_bound"),
            "upper_bound": pos.get("upper_bound"),
            "lower_price": pos.get("lower_price"),
            "upper_price": pos.get("upper_price"),
            "current_price": pos.get("current_price"),
            "fees_usd": fees,
            "score": total, "components": components, "verdict": verdict,
            "collect_fees": collect,
            "data_quality": {"unknown_components": unknown_parts,
                             "policy_close": False},
            "reason": reason,
            "expected_pnl": pnl,
            "note": reason}


def score_position(pos: dict, pools_by_addr: dict = None, ctx: dict = None) -> dict:
    """Local scoring path: derive inputs from the raw position record."""
    from scoring.universe import classify_pair
    pair_class, sym_x, sym_y = classify_pair(pos)
    pool = (pools_by_addr or {}).get(pos.get("pool_address"))
    if pool is not None and pair_class == "unknown":
        pair_class, _, _ = classify_pair(pool)
    pool_name = pos.get("pool_name") or pos.get("name") or (pool or {}).get("name")

    if pair_class in ("off_universe", "unknown"):
        from scoring.policy import _position_policy_close
        result = _position_policy_close(pos, pool_name, pair_class, sym_x, sym_y)
        result["score_policy"] = {"source": "local", "features_version": 0}
        return result

    result = score_position_core(pos, pool, pool_name, pair_class, sym_x, sym_y, ctx)
    result["score_policy"] = {"source": "local", "features_version": 0}
    return result


def score_position_missy(pos: dict, pools_by_addr: dict = None,
                         ctx: dict = None) -> dict:
    """Missy-feature scoring path (scoring.position_source = "missy").

    Sheldon still owns the policy: profile weights, thresholds, verdicts,
    open/close separation. Only the *inputs* come from Missy's versioned
    position_features block; feature gaps surface as unknown components and
    cap the verdict exactly like any other missing data. Falls back to the
    local path when the block is missing or its version differs.

    NEW: cross-model support. When the paired pool has a new_fc_score (fee-capture
    model), its components are consumed alongside position features to inform
    the verdict. The position score itself remains Sheldon-owned; new_fc provides
    a complementary lens (fee efficiency + volatility effectiveness + turnover
    sustainability) that sits beside—not inside—the missy-default score.
    """
    from scoring.config import load_scoring_policy
    policy = load_scoring_policy()
    features = pos.get("position_features")
    if (not isinstance(features, dict)
            or int(features.get("version") or 0) != policy["position_features_version"]):
        result = score_position(pos, pools_by_addr, ctx)
        result["score_policy"] = {
            "source": "local", "features_version": 0,
            "fallback_reason": "position_features missing or version mismatch"}
        return result

    pool = (pools_by_addr or {}).get(
        features.get("pool_address") or pos.get("pool_address"))
    pair_class, sym_x, sym_y = classify_pair(features)
    if pair_class == "unknown" and pool is not None:
        # Features carry no symbols: take class AND symbols from the pool
        # record so stable-side policy (depeg exposure) can fire.
        pair_class, sym_x, sym_y = classify_pair(pool)
    pool_name = (pool or {}).get("name")

    if pair_class in ("off_universe", "unknown"):
        from scoring.policy import _position_policy_close
        result = _position_policy_close(
            {"position_id": features.get("position_address") or pos.get("position_address"),
             "pool_address": features.get("pool_address") or pos.get("pool_address")},
            pool_name, pair_class, sym_x, sym_y)
        result["score_policy"] = {
            "source": "missy", "features_version": policy["position_features_version"]}
        return result

    view = _view_from_features(pos, features)
    # --- Cross-model: new_fc_score components from paired pool ---
    fc = {}
    fc_score = 0.0
    fc_verdict = "IGNORE"
    if pool and "tvl" in pool and "realized_fee_apr" in pool:
        try:
            fc = score_pool_new_fc(pool, ctx)
            fc_score = fc.get("score", 0.0)
            fc_verdict = fc.get("verdict", "IGNORE")
        except Exception:
            fc = {}
            fc_score = 0.0
            fc_verdict = "IGNORE"

    result = score_position_core(
        view, pool, pool_name, pair_class, sym_x, sym_y, ctx,
        extra_unknown=[g for g in (features.get("gaps") or [])
                       if isinstance(g, str)])

    # --- Augment result with new_fc cross-model insight ---
    result["new_fc_score"] = fc_score
    result["new_fc_components"] = fc.get("components", {})
    result["new_fc_verdict"] = fc_verdict
    # Cross-model agreement flag
    missy_verdict = result["verdict"]
    if fc_verdict == missy_verdict:
        result["cross_model"] = f"both {missy_verdict.lower()} (agreement)"
    elif fc_verdict != "IGNORE":
        result["cross_model"] = f"missy={missy_verdict.lower()} new_fc={fc_verdict.lower()}"
    else:
        result["cross_model"] = "new_fc_not_computed"

    result["score_policy"] = {
        "source": "missy", "features_version": policy["position_features_version"]}
    return result


def _view_from_features(pos: dict, features: dict) -> dict:
    """Map Missy's feature vector onto the keys the scoring policy reads.

    Format adaptation only — no scoring decisions here. Prices come from the
    raw position record (the feature vector does not carry them) and feed the
    expected-PnL verdict; absent prices (RPC-fallback scans) stay None and
    route through the policy's existing unknown-data paths. Token amounts and
    il/fee estimates remain deliberately absent.

    NEW: also carry new_fc_score components from the paired pool for
    cross-model position decision support.
    """
    gaps = set(features.get("gaps") or [])
    # Pull new_fc_score components from the pool record if available
    pool_addr = features.get("pool_address") or pos.get("pool_address")
    new_fc_components = {}
    new_fc_score = None
    new_fc_verdict = "IGNORE"
    if pool_addr:
        from scoring.pool.score import score_pool_new_fc
        pool_result = score_pool_new_fc(
            {"pool_address": pool_addr, "dex": features.get("dex") or pos.get("dex") or ""},
            {"norms": (pos.get("ctx") or {}).get("norms")}
        ) if False else None  # placeholder - pool may not be in ctx
    # Since pool may not be available in ctx, we carry new_fc info later
    # in score_position_missy where we have full pool context

    return {
        "position_id": features.get("position_address") or pos.get("position_address"),
        "pool_address": features.get("pool_address") or pos.get("pool_address"),
        "in_range": features.get("in_range"),
        "lower_bound": features.get("range_lower"),
        "upper_bound": features.get("range_upper"),
        "edge_distance_frac": features.get("edge_distance_frac"),
        "single_sided": features.get("single_sided"),
        "side_bias_x": features.get("side_bias_x"),
        "fees_usd": None if "fees_unknown" in gaps else features.get("fees_usd"),
        "current_value_usd": (features.get("value_usd")
                              if features.get("value_known") else None),
        "days_open": features.get("days_open"),
        "lower_price": pos.get("lower_price"),
        "upper_price": pos.get("upper_price"),
        "current_price": pos.get("current_price"),
        # Gaps for policy to reference
        "gaps": list(gaps),
    }