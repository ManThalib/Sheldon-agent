#!/usr/bin/env python3
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
  - stable_bluechip: e.g. SOL-USDC, cbBTC-SOL. Risk = IL / trend walking out of range.
  - bluechip_bluechip: e.g. SOL-cbBTC. Same risk model as stable_bluechip.
  - off_universe / unknown: pools are IGNOREd; existing positions are CLOSEd.

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

# --------------------------------------------------------------------------
# Universe policy — stablecoins and high-caps only, no memes.
# Anything not in these sets gates the pool out entirely.
# --------------------------------------------------------------------------
STABLECOINS = {
    "USDC", "USDT", "USDS", "PYUSD", "DAI", "FDUSD", "EURC", "USDH", "USX",
}
HIGH_CAPS = {
    "SOL", "WSOL", "WBTC", "CBBTC", "WETH", "ETH",
    "JITOSOL", "JSOL", "MSOL", "BSOL", "JUP", "ZEC",
}

# --------------------------------------------------------------------------
# Scoring profiles per pair class. Components sum to 100.
# Change here; never improvise per-cycle.
# --------------------------------------------------------------------------
POOL_PROFILES = {
    "stable_stable": {
        "weights": {"fee_yield": 35.0, "turnover": 15.0, "depth": 15.0,
                    "depeg_safety": 25.0, "volatility_fit": 10.0},
        "vol_peak_pct": 0.5,     # stable pairs should barely move
        "apr_cap_pct": 100.0,    # stable APRs are low; 100%+ is exceptional
    },
    "stable_bluechip": {
        "weights": {"fee_yield": 30.0, "turnover": 20.0, "depth": 15.0,
                    "volatility_fit": 20.0, "depeg_safety": 15.0},
        "vol_peak_pct": 8.0,
        "apr_cap_pct": 300.0,
    },
    # bluechip/bluechip behaves like stable/bluechip minus the depeg leg;
    # its weight is folded into volatility_fit.
    "bluechip_bluechip": {
        "weights": {"fee_yield": 30.0, "turnover": 20.0, "depth": 15.0,
                    "volatility_fit": 35.0},
        "vol_peak_pct": 10.0,
        "apr_cap_pct": 300.0,
    },
}

POSITION_PROFILES = {
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
}

POOL_THRESHOLDS = {"open": 70.0, "watch": 55.0}
POSITION_THRESHOLDS = {"close": 40.0, "review": 60.0}

# Depth mapping: TVL USD range for log-scale scoring.
DEPTH_MIN_USD = 50_000.0
DEPTH_MAX_USD = 5_000_000.0
# A stable is considered depegged (zero credit) at this distance from $1.
DEPEG_ZERO_DIST = 0.005  # 0.5%
# Position staleness: healthy long-lived positions are NOT decayed; the
# component only erodes confidence that entry assumptions still hold.
STALE_GRACE_DAYS = 30.0
STALE_ZERO_DAYS = 120.0
# Fee collection: worth claiming when fees exceed this share of position
# value (plus a small absolute floor for tiny/unknown positions).
COLLECT_MIN_USD = 5.0
COLLECT_PCT_OF_VALUE = 0.01  # 1%


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


def classify_pair(record: dict):
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
    """Window volume / TVL. Turnover of 5x+ over the window maxes out."""
    tvl = float(pool.get("tvl") or 0.0)
    volume = float(pool.get("volume_window") or 0.0)
    if tvl <= 0:
        return 0.0
    return round(max_pts * clamp((volume / tvl) / 5.0, 0.0, 1.0), 2)


def score_pool_depth(pool: dict, max_pts: float) -> float:
    """Log scale between DEPTH_MIN_USD and DEPTH_MAX_USD."""
    tvl = float(pool.get("tvl") or 0.0)
    if tvl < DEPTH_MIN_USD:
        return 0.0
    frac = (math.log(tvl / DEPTH_MIN_USD)
            / math.log(DEPTH_MAX_USD / DEPTH_MIN_USD))
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
    sides = []
    for sym, key in ((sym_x, "token_x_price_usd"), (sym_y, "token_y_price_usd")):
        if sym not in STABLECOINS:
            continue
        price = pool.get(key)
        if price is None:
            sides.append(0.5)  # unknown peg: half credit
            continue
        dist = abs(float(price) - 1.0)
        sides.append(clamp(1.0 - dist / DEPEG_ZERO_DIST, 0.0, 1.0))
    if not sides:  # no stable side: component not applicable
        return round(max_pts, 2)
    return round(max_pts * min(sides), 2)


def score_pool(pool: dict) -> dict:
    pair_class, sym_x, sym_y = classify_pair(pool)

    if pair_class in ("off_universe", "unknown"):
        return {"pool": pool.get("name"), "pool_address": pool.get("pool_address"),
                "dex": pool.get("dex"), "pair_class": pair_class,
                "pair": [sym_x, sym_y], "score": 0.0, "components": {},
                "verdict": "IGNORE", "_pool": pool}

    profile = POOL_PROFILES[pair_class]
    w = profile["weights"]
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

    total = round(sum(components.values()), 2)
    if total >= POOL_THRESHOLDS["open"]:
        verdict = "OPEN_CANDIDATE"
    elif total >= POOL_THRESHOLDS["watch"]:
        verdict = "WATCH"
    else:
        verdict = "IGNORE"
    return {"pool": pool.get("name"), "pool_address": pool.get("pool_address"),
            "dex": pool.get("dex"), "pair_class": pair_class,
            "pair": [sym_x, sym_y], "score": total, "components": components,
            "verdict": verdict, "_pool": pool}


# --------------------------------------------------------------------------
# Position Health Score
# --------------------------------------------------------------------------
def _pos_price_bounds(pos: dict) -> tuple:
    lower = pos.get("lower_price")
    upper = pos.get("upper_price")
    current = pos.get("current_price")
    if lower is None or upper is None or current is None:
        return None, None, None
    return float(lower), float(upper), float(current)


def score_position_range_status(pos: dict, max_pts: float) -> float:
    """In-range earns fees; distance-to-boundary discounts positions about
    to flip single-sided. Missing data scores low (fail-closed)."""
    lower, upper, current = _pos_price_bounds(pos)
    if lower is not None and upper > lower:
        if not (lower <= current <= upper):
            return 0.0
        span = upper - lower
        edge_dist = min(current - lower, upper - current) / (span / 2.0)
        # Full credit when price sits in the middle 50% of the range;
        # fades toward 60% credit as the price hugs a boundary.
        edge_frac = clamp((edge_dist - 0.0) / 0.5, 0.0, 1.0)
        return round(max_pts * (0.6 + 0.4 * edge_frac), 2)
    if "in_range" in pos:
        return max_pts if pos["in_range"] else 0.0
    return round(max_pts * 0.3, 2)  # unknown: fail-closed


def score_position_fee_capture(pos: dict, max_pts: float) -> float:
    """Fees earned vs expected. Missy positions often lack
    expected_fees_usd; unknown scores low (fail-closed), not neutral."""
    earned, expected = pos.get("fees_usd"), pos.get("expected_fees_usd")
    if earned is None or expected is None or float(expected) <= 0:
        return round(max_pts * 0.3, 2)
    return round(max_pts * clamp(float(earned) / float(expected), 0.0, 1.0), 2)


def score_position_il_risk(pos: dict, max_pts: float) -> float:
    """IL exposure for bluechip pairs. il_estimate_pct of 10%+ zeroes it.
    Missing estimate scores low (fail-closed)."""
    il = pos.get("il_estimate_pct")
    if il is None:
        return round(max_pts * 0.4, 2)
    return round(max_pts * clamp(1.0 - float(il) / 10.0), 2)


def score_position_depeg_exposure(pos: dict, max_pts: float, sym_x, sym_y,
                                  pool: dict) -> float:
    """Stable/stable IL is depeg risk. Full credit when both pegs hold and
    the position is two-sided; penalized when single-sided in a depegged
    token (the exit already happened, against you)."""
    def peg_frac(sym, pool_key):
        if sym not in STABLECOINS:
            return 1.0
        price = None
        if pool:
            price = pool.get(pool_key)
        if price is None:
            return 0.5  # unseen peg: half credit
        return clamp(1.0 - abs(float(price) - 1.0) / DEPEG_ZERO_DIST, 0.0, 1.0)

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
        return round(max_pts * held, 2)
    return round(max_pts * min(fx, fy), 2)


def score_position_staleness(pos: dict, max_pts: float) -> float:
    """Confidence that entry assumptions still hold. Healthy long-lived
    positions are NOT punished: grace period of STALE_GRACE_DAYS, then a
    gentle erosion to zero at STALE_ZERO_DAYS."""
    days = pos.get("days_open")
    if days is None:
        return round(max_pts * 0.5, 2)  # unverifiable age: half credit
    days = float(days)
    if days <= STALE_GRACE_DAYS:
        return round(max_pts, 2)
    frac = 1.0 - (days - STALE_GRACE_DAYS) / (STALE_ZERO_DAYS - STALE_GRACE_DAYS)
    return round(max_pts * clamp(frac, 0.0, 1.0), 2)


def score_position(pos: dict, pools_by_addr: dict = None) -> dict:
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
                "note": "off-universe pair: policy is stables/high-caps only"}

    profile = POSITION_PROFILES[pair_class]
    w = profile["weights"]
    components = {
        "range_status": score_position_range_status(pos, w["range_status"]),
        "fee_capture": score_position_fee_capture(pos, w["fee_capture"]),
        "staleness": score_position_staleness(pos, w["staleness"]),
    }
    if "depeg_exposure" in w:
        components["depeg_exposure"] = score_position_depeg_exposure(
            pos, w["depeg_exposure"], sym_x, sym_y, pool)
    if "il_risk" in w:
        components["il_risk"] = score_position_il_risk(pos, w["il_risk"])

    total = round(sum(components.values()), 2)
    if total < POSITION_THRESHOLDS["close"]:
        verdict = "CLOSE"
    elif total < POSITION_THRESHOLDS["review"]:
        verdict = "REVIEW"
    else:
        verdict = "HOLD"

    # Fee harvesting: worth the tx cost when fees clear ~1% of position
    # value (or the floor when value is unknown).
    fees = pos.get("fees_usd")
    value = pos.get("current_value_usd")
    floor = COLLECT_MIN_USD
    if isinstance(value, (int, float)) and value > 0:
        floor = max(COLLECT_MIN_USD, COLLECT_PCT_OF_VALUE * float(value))
    collect = isinstance(fees, (int, float)) and fees >= floor

    return {"position_id": pos.get("position_id") or pos.get("position_address"),
            "pool": pool_name,
            "pool_address": pos.get("pool_address"),
            "pair_class": pair_class, "pair": [sym_x, sym_y],
            "lower_bound": pos.get("lower_bound"),
            "upper_bound": pos.get("upper_bound"),
            "fees_usd": pos.get("fees_usd"),
            "score": total, "components": components, "verdict": verdict,
            "collect_fees": collect}


# --------------------------------------------------------------------------
# Main cycle
# --------------------------------------------------------------------------
def run_cycle(pools_dir: str, positions_dir: str, max_age_seconds: float = 3900.0) -> dict:
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
                "evidence": s["components"], "_pool": s.get("_pool")})

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
        lines.append(f"  {s['pool']:<22} [{s.get('pair_class') or '-':<17}] "
                     f"score={s['score']:>6.1f}  {s['verdict']}")
    if report["position_scores"]:
        lines.append("Open LP positions:")
        for s in report["position_scores"]:
            extra = " (fees ready to collect)" if s["collect_fees"] else ""
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
