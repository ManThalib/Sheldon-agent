#!/usr/bin/env python3
"""Sheldon's LP scoring engine.

Elaborate scoring model for DeFi LP (Meteora DLMM) position decisions.
Reads the newest Missy outputs (pool scan + position scan) and produces:
  - Pool LP Opportunity Score (0-100): is this pool worth an LP position?
  - Position Health Score (0-100): should an open LP position stay open?
  - Verdicts: OPEN / HOLD / CLOSE / COLLECT_FEES  (no buy/sell market timing)

Stdlib only. Run:  python3 lp_scoring.py [--pools-dir D] [--positions-dir D] [--json]
"""

import argparse
import glob
import json
import math
import os
import sys
import time

# --------------------------------------------------------------------------
# Tunable weights — all components sum to their component max.
# Change here; never improvise per-cycle.
# --------------------------------------------------------------------------
POOL_WEIGHTS = {
    "fee_yield": 35,      # realized fee APR + fee/TVL efficiency
    "turnover": 20,       # volume relative to TVL (fee capture engine)
    "depth": 15,          # TVL log-scale: deeper = safer, more stable ranges
    "volatility_fit": 20, # moderate volatility earns fees; extreme = breach/IL risk
    "bin_step_fit": 10,   # bin spacing appropriate for the pool's volatility
}

POOL_THRESHOLDS = {"open": 70.0, "watch": 55.0}   # >=open: OPEN candidate; watch..open: HOLD/watch; <watch: ignore

POSITION_WEIGHTS = {
    "range_status": 35,     # in-range = fees accrue; out-of-range = single-sided
    "fee_capture": 25,      # fees earned vs expected for time in position
    "il_risk": 25,          # estimated impermanent loss / divergence exposure
    "time_decay": 15,       # positions past intended horizon decay
}

POSITION_THRESHOLDS = {"close": 40.0, "review": 60.0}  # <close: CLOSE; close..review: review; >=review: HOLD

# Volatility fit curve: peak at this value (%), falls off both sides.
VOLATILITY_PEAK_PCT = 8.0
# Depth mapping: TVL USD range for log-scale scoring.
DEPTH_MIN_USD = 50_000.0
DEPTH_MAX_USD = 5_000_000.0
# Fee APR mapping: APR at/above which the fee_yield component maxes out.
FEE_APR_CAP_PCT = 300.0
# Position horizon: days after which time decay starts biting.
HORIZON_DAYS = 14.0


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


# --------------------------------------------------------------------------
# Pool LP Opportunity Score
# --------------------------------------------------------------------------
def score_pool_fee_yield(pool: dict) -> float:
    """0-35: realized fee APR (0-25) + fee/TVL ratio (0-10)."""
    apr_pct = float(pool.get("realized_fee_apr") or 0.0)
    apr_pts = 25.0 * clamp(apr_pct / FEE_APR_CAP_PCT, 0.0, 1.0)
    ftvl_pct = float(pool.get("fee_tvl_ratio") or 0.0) * 100.0  # ratio -> %
    # fee/TVL of 1% daily is exceptional; scale to that.
    ftvl_pts = 10.0 * clamp(ftvl_pct / 1.0, 0.0, 1.0)
    return round(apr_pts + ftvl_pts, 2)


def score_pool_turnover(pool: dict) -> float:
    """0-20: window volume / TVL. Turnover of 5x+ over the window maxes out."""
    tvl = float(pool.get("tvl") or 0.0)
    volume = float(pool.get("volume_window") or 0.0)
    if tvl <= 0:
        return 0.0
    turnover = volume / tvl
    return round(20.0 * clamp(turnover / 5.0, 0.0, 1.0), 2)


def score_pool_depth(pool: dict) -> float:
    """0-15: log scale between DEPTH_MIN_USD and DEPTH_MAX_USD."""
    tvl = float(pool.get("tvl") or 0.0)
    if tvl < DEPTH_MIN_USD:
        return 0.0
    frac = (
        math.log(tvl / DEPTH_MIN_USD)
        / math.log(DEPTH_MAX_USD / DEPTH_MIN_USD)
    )
    return round(15.0 * clamp(frac, 0.0, 1.0), 2)


def score_pool_volatility_fit(pool: dict) -> float:
    """0-20: triangular curve peaking at VOLATILITY_PEAK_PCT.

    Too little volatility = little fee churn; too much = range breach + IL.
    """
    vol = float(pool.get("volatility") or 0.0)
    if vol <= 0:
        return 0.0
    peak = VOLATILITY_PEAK_PCT
    # Zero score at 0% and at 3x peak; linear to peak.
    if vol >= 3.0 * peak:
        return 0.0
    score = 20.0 * (vol / peak if vol <= peak else (3.0 * peak - vol) / (2.0 * peak))
    return round(clamp(score), 2)


def score_pool_bin_step_fit(pool: dict) -> float:
    """0-10: bin step vs volatility. Volatile pools need coarser spacing.

    Rule of thumb: bin_step * ~0.02% should cover a reasonable share of the
    pool's volatility excursion. Under-spaced (too fine for its volatility)
    scores low; well-matched scores high.
    """
    bin_step = float(pool.get("bin_step") or 0)
    vol = float(pool.get("volatility") or 0.0)
    if bin_step <= 0:
        return 5.0  # unknown/standard pool: neutral-middle
    spacing_pct = bin_step * 0.02  # DLMM bin step in % price distance
    if vol <= 0:
        return 5.0
    ratio = spacing_pct / vol  # 1.0 = spacing matches excursion
    if ratio < 0.2:
        return round(10.0 * (ratio / 0.2) * 0.5, 2)      # far too fine: 0-5
    if ratio <= 1.5:
        return round(5.0 + 5.0 * min(1.0, (ratio - 0.2) / 1.3), 2)  # 5-10
    return round(max(0.0, 10.0 - (ratio - 1.5) * 4.0), 2)  # too coarse: decays


def score_pool(pool: dict) -> dict:
    components = {
        "fee_yield": score_pool_fee_yield(pool),
        "turnover": score_pool_turnover(pool),
        "depth": score_pool_depth(pool),
        "volatility_fit": score_pool_volatility_fit(pool),
        "bin_step_fit": score_pool_bin_step_fit(pool),
    }
    total = round(sum(components.values()), 2)
    if total >= POOL_THRESHOLDS["open"]:
        verdict = "OPEN_CANDIDATE"
    elif total >= POOL_THRESHOLDS["watch"]:
        verdict = "WATCH"
    else:
        verdict = "IGNORE"
    return {"pool": pool.get("name"), "pool_address": pool.get("pool_address"),
            "dex": pool.get("dex"), "score": total, "components": components,
            "verdict": verdict}


# --------------------------------------------------------------------------
# Position Health Score
# --------------------------------------------------------------------------
def score_position_range_status(pos: dict) -> float:
    """0-35: is the current price inside the position's range?

    Expects optional position fields: in_range (bool), or lower/upper price
    bounds with current price. Missing data scores neutral-middle (17.5).
    """
    if "in_range" in pos:
        return 35.0 if pos["in_range"] else 0.0
    lower, upper, current = pos.get("lower_price"), pos.get("upper_price"), pos.get("current_price")
    if lower is not None and upper is not None and current is not None:
        return 35.0 if float(lower) <= float(current) <= float(upper) else 0.0
    return 17.5


def score_position_fee_capture(pos: dict) -> float:
    """0-25: fees earned vs expected.

    Expects optional: fees_usd (earned), expected_fees_usd. Missing data
    scores neutral-middle (12.5). Over-earning (rewards) caps at max.
    """
    earned, expected = pos.get("fees_usd"), pos.get("expected_fees_usd")
    if earned is None or expected is None or float(expected) <= 0:
        return 12.5
    ratio = float(earned) / float(expected)
    return round(25.0 * clamp(ratio, 0.0, 1.0), 2)


def score_position_il_risk(pos: dict) -> float:
    """0-25: estimated impermanent loss exposure.

    Expects optional: il_estimate_pct (positive number = loss). Missing data
    scores neutral-middle (12.5). il_estimate_pct of 10%+ zeroes the score.
    """
    il = pos.get("il_estimate_pct")
    if il is None:
        return 12.5
    return round(25.0 * clamp(1.0 - float(il) / 10.0), 2)


def score_position_time_decay(pos: dict) -> float:
    """0-15: decay after HORIZON_DAYS.

    Expects optional: days_open. Missing data scores full (fresh assumption).
    """
    days = pos.get("days_open")
    if days is None:
        return 15.0
    days = float(days)
    if days <= HORIZON_DAYS:
        return 15.0
    # Linear decay to zero at 3x horizon.
    return round(15.0 * clamp(1.0 - (days - HORIZON_DAYS) / (2.0 * HORIZON_DAYS)), 2)


def score_position(pos: dict) -> dict:
    components = {
        "range_status": score_position_range_status(pos),
        "fee_capture": score_position_fee_capture(pos),
        "il_risk": score_position_il_risk(pos),
        "time_decay": score_position_time_decay(pos),
    }
    total = round(sum(components.values()), 2)
    if total < POSITION_THRESHOLDS["close"]:
        verdict = "CLOSE"
    elif total < POSITION_THRESHOLDS["review"]:
        verdict = "REVIEW"
    else:
        verdict = "HOLD"
    # Fee harvesting: explicit threshold independent of health.
    fees = pos.get("fees_usd")
    collect = isinstance(fees, (int, float)) and fees >= 10.0
    return {"position_id": pos.get("position_id") or pos.get("position_address"),
            "pool": pos.get("pool_name") or pos.get("name"),
            "pool_address": pos.get("pool_address"),
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

    if not pool_path or not is_fresh(pool_path, max_age_seconds):
        report["failures"].append("pool data missing or stale")
    if not pos_path or not is_fresh(pos_path, max_age_seconds):
        report["failures"].append("position data missing or stale")
    if report["failures"]:
        return report

    pools = load_json(pool_path)
    if not isinstance(pools, list):
        report["failures"].append("pool data malformed (expected list)")
        return report

    for pool in pools:
        s = score_pool(pool)
        report["pool_scores"].append(s)
        if s["verdict"] == "OPEN_CANDIDATE":
            report["verdicts"].append({
                "action": "OPEN_CANDIDATE", "pool": s["pool"],
                "pool_address": s["pool_address"], "dex": s.get("dex"),
                "score": s["score"], "evidence": s["components"]})

    pos_data = load_json(pos_path)
    positions = pos_data.get("positions", []) if isinstance(pos_data, dict) else []
    for pos in positions:
        s = score_position(pos)
        report["position_scores"].append(s)
        verdict = "COLLECT_FEES" if (s["verdict"] == "HOLD" and s["collect_fees"]) else s["verdict"]
        report["verdicts"].append({
            "action": verdict, "position": s["position_id"], "pool": s["pool"],
            "pool_address": s.get("pool_address"), "dex": pos.get("dex"),
            "lower_bound": s.get("lower_bound"), "upper_bound": s.get("upper_bound"),
            "fees_usd": s.get("fees_usd"),
            "score": s["score"], "evidence": s["components"]})

    return report


def render_summary(report: dict) -> str:
    lines = [f"Sheldon LP evaluation — {report['generated_at']}"]
    if report["failures"]:
        lines += [f"FAILURES: {'; '.join(report['failures'])}"]
        return "\n".join(lines)
    top = sorted(report["pool_scores"], key=lambda s: s["score"], reverse=True)[:5]
    lines.append("Top pool LP opportunities:")
    for s in top:
        lines.append(f"  {s['pool']:<16} score={s['score']:>6.1f}  {s['verdict']}")
    if report["position_scores"]:
        lines.append("Open LP positions:")
        for s in report["position_scores"]:
            extra = " (fees ready to collect)" if s["collect_fees"] else ""
            lines.append(f"  {str(s['pool']):<16} score={s['score']:>6.1f}  {s['verdict']}{extra}")
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
