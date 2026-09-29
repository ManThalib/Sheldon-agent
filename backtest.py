#!/usr/bin/env python3
"""Sheldon backtest: replay historical Missy scans through the scoring engine.

Answers three questions with data instead of intuition:

  1. Do OPEN_CANDIDATE picks earn more forward fee APR than the average pool?
  2. Do would-be open ranges survive (price stays inside) over the horizon?
  3. How do position verdicts play out in forward value terms?

New in this version:
  4. Synthetic position PnL: simulate a $100 50/50 position through the
     horizon, including fees, impermanent loss, rebalancing when price leaves
     the range, and realistic swap/gas costs.
  5. Robustness: walk-forward first-half vs second-half performance and
     bootstrap confidence intervals for mean PnL.

Method
------
Pool replay:
  For every historical pool scan, score all pools. For each OPEN_CANDIDATE
  pool at scan i, look at scans i+1 .. i+horizon:
    - forward_apr: mean realized_fee_apr of that pool in later scans
    - survival: fraction of later scans where pool price stayed inside the
      would-be ±(width/2) bin/tick range around the entry price
  Baseline: the same forward stats across ALL scored (investable) pools,
  so selection skill is measured against the universe, not against zero.

Position replay:
  Score every historical position scan. For each position address, record
  the first verdict and the verdict path over the next `horizon` scans.
  current_value_usd only exists in enriched scans (2026-09-25+); when a
  position has positive values in later scans, a value trajectory and
  change percentage are attached.

Synthetic PnL replay (enriched scans only):
  For each OPEN_CANDIDATE with token prices, model opening a 50/50 position
  of size `position_value_usd`, walk it forward through the horizon, collect
  fees using realized_fee_apr scaled by actual time between scans, apply
  impermanent loss from price-ratio changes, and rebalance (close + reopen)
  whenever price exits the configured ±half_width range. Costs include
  entry/exit swap fees and a flat claim/gas fee per rebalance.

Robustness:
  - Walk-forward: split OPEN_CANDIDATE PnL rows chronologically into first
    half vs second half and compare mean/median PnL, hit rate and max
    drawdown.
  - Bootstrap: resample PnL rows with replacement many times and report
    5th/95th percentile of mean PnL and hit rate.

Caveats
-------
- Scans before 2026-09-25 15:16 UTC lack Jupiter price enrichment
  (token_*_price_usd), which lowers depeg/fee component scores. Per-scan
  input completeness is reported so eras can be segmented.
- realized_fee_apr is a trailing-window metric, not truly forward-looking.
- Synthetic PnL is a simulation: it ignores partial fills, MEV, exact
  bin/tick rounding, and protocol-specific DLMM/CLMM fee mechanics. Treat
  it as directional, not exact.

Stdlib only. Run:
  python3 backtest.py [--data-dir /data/missy-data] [--horizon 4]
                      [--width 200] [--position-value-usd 100]
                      [--json] [--detail]
"""

import argparse
import glob
import json
import math
import os
import random
import sys
from datetime import datetime

from lp_scoring import score_pool, score_position, pool_verdict, get_config as lp_get_config
import dynamic

DEFAULT_DATA_DIR = "/data/missy-data"

# Simulation defaults. These are intentionally conservative; adjust via CLI.
DEFAULT_POSITION_VALUE_USD = 100.0
DEFAULT_ENTRY_COST_BPS = 50
DEFAULT_EXIT_COST_BPS = 50
DEFAULT_CLAIM_COST_USD = 0.02
DEFAULT_BOOTSTRAP_SAMPLES = 1000


def _scan_dt(path: str) -> datetime:
    """Extract datetime from a Missy scan filename like pool_scan-20260922-121433.json."""
    stem = os.path.basename(path)
    # strip prefix and suffix
    for prefix in ("pool_scan-", "position_scan-"):
        if stem.startswith(prefix):
            dt_part = stem[len(prefix):].rsplit(".", 1)[0]
            try:
                return datetime.strptime(dt_part, "%Y%m%d-%H%M%S")
            except ValueError:
                pass
    return datetime.fromtimestamp(0)


def list_scans(directory: str, prefix: str):
    paths = sorted(
        p for p in glob.glob(os.path.join(directory, prefix + "-*.json"))
        if not p.endswith((".failed", ".invalid"))
    )
    return paths


def load_scan(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        data = data.get("pools" if "pools" in data else "positions", [])
    return data if isinstance(data, list) else None


def scan_completeness(pools: list) -> float:
    """Fraction of pools with enriched token prices (era segmentation)."""
    if not pools:
        return 0.0
    good = sum(1 for p in pools
               if float(p.get("token_x_price_usd") or 0) > 0
               and float(p.get("token_y_price_usd") or 0) > 0)
    return good / len(pools)


def _range_center(pool: dict) -> float:
    if pool.get("dex") == "meteora":
        return float(pool.get("active_bin_id") or 0)
    for key in ("current_tick", "current_tick_index", "active_bin_id"):
        val = pool.get(key)
        if val not in (None, "", 0, "0"):
            return float(val)
    return 0.0


def _derive_tick(pool: dict) -> float:
    price = float(pool.get("pool_price") or 0.0)
    dx = int(pool.get("token_x_decimals") or 0)
    dy = int(pool.get("token_y_decimals") or 0)
    if price <= 0 or dx <= 0 or dy <= 0:
        return 0.0
    return math.log(price * (10 ** (dy - dx))) / math.log(1.0001)


def price_in_range(entry_pool: dict, later_pool, half_width: int):
    """Did price stay within the would-be ±half_width range vs entry?"""
    if later_pool is None:
        return None
    dex = entry_pool.get("dex")
    step = float(entry_pool.get("bin_step") or entry_pool.get("tick_spacing") or 0)
    p0 = float(entry_pool.get("pool_price") or 0.0)
    p1 = float(later_pool.get("pool_price") or 0.0)
    if p0 <= 0 or p1 <= 0:
        return None
    ratio = p1 / p0
    if dex == "meteora" and step > 0:
        factor = (1.0 + step / 10000.0) ** half_width
    else:
        factor = 1.0001 ** half_width
    return (1.0 / factor) <= ratio <= factor


def _impermanent_loss(price_ratio: float) -> float:
    """Classic 50/50 LP impermanent loss as a fraction of value."""
    if price_ratio <= 0:
        return 0.0
    return 2.0 * math.sqrt(price_ratio) / (1.0 + price_ratio) - 1.0


def _simulate_position(
    entry_pool: dict,
    future: list,
    half_width: int,
    position_value_usd: float,
    entry_cost_bps: int,
    exit_cost_bps: int,
    claim_cost_usd: float,
) -> dict:
    """Simulate a 50/50 position through future scans.

    Returns a dict with PnL breakdown and a per-step path.
    """
    px_x0 = float(entry_pool.get("token_x_price_usd") or 0.0)
    px_y0 = float(entry_pool.get("token_y_price_usd") or 0.0)
    if px_x0 <= 0 or px_y0 <= 0:
        return {"pnl_usd": None, "error": "missing token prices"}

    # Entry: 50/50 USD split, then pay entry swap cost.
    value_x = position_value_usd / 2.0
    value_y = position_value_usd / 2.0
    entry_cost = position_value_usd * entry_cost_bps / 10000.0
    net_value = position_value_usd - entry_cost

    path = []
    rebalances = 0
    total_fees = 0.0
    total_exit_costs = 0.0

    # Current state tracks the *net portfolio value* invested in the pool.
    current_value = net_value
    entry_price_ratio = 1.0  # ratio of current price to entry price

    entry_dt = _scan_dt(entry_pool.get("_path", ""))
    prev_dt = entry_dt

    for i, f in enumerate(future):
        later_pool = f["by_addr"].get(entry_pool.get("pool_address"))
        if later_pool is None:
            continue

        later_dt = _scan_dt(later_pool.get("_path", f["path"]))

        px_x1 = float(later_pool.get("token_x_price_usd") or 0.0)
        px_y1 = float(later_pool.get("token_y_price_usd") or 0.0)
        if px_x1 <= 0 or px_y1 <= 0:
            continue

        # Price ratio vs entry, using the pool price (price of X in terms of Y).
        price0 = float(entry_pool.get("pool_price") or 0.0)
        price1 = float(later_pool.get("pool_price") or 0.0)
        if price0 > 0 and price1 > 0:
            price_ratio = price1 / price0
        else:
            price_ratio = (px_x1 / px_x0) / (px_y1 / px_y0) if px_y1 and px_x0 else 1.0

        # Impermanent loss since entry, applied to current value.
        il = _impermanent_loss(price_ratio)
        gross_value = current_value * (1.0 + il)

        # Fees earned over this step, scaled to this position's size.
        # Use incremental time since the previous observation.
        delta_years = max(0.0, (later_dt - prev_dt).total_seconds() / (365.25 * 24 * 3600))
        apr = float(later_pool.get("realized_fee_apr") or 0.0)
        fees = current_value * (apr / 100.0) * delta_years
        total_fees += fees
        prev_dt = later_dt

        pre_rebalance_value = gross_value + fees

        # Determine if price is still inside the configured range.
        in_range = price_in_range(entry_pool, later_pool, half_width)

        path.append({
            "scan": os.path.basename(f["path"]),
            "value": round(pre_rebalance_value, 2),
            "fees_step": round(fees, 4),
            "il": round(il, 4),
            "in_range": in_range,
        })

        # If out of range and not the last scan, rebalance: close + reopen.
        if in_range is False and i < len(future) - 1:
            exit_cost = pre_rebalance_value * exit_cost_bps / 10000.0 + claim_cost_usd
            total_exit_costs += exit_cost
            remaining = pre_rebalance_value - exit_cost
            # Re-enter at current price with a fresh 50/50 position.
            entry_cost2 = remaining * entry_cost_bps / 10000.0
            current_value = remaining - entry_cost2
            entry_price_ratio = 1.0
            entry_pool = later_pool
            entry_pool["_path"] = f["path"]
            entry_dt = later_dt
            rebalances += 1
        else:
            current_value = pre_rebalance_value
            entry_price_ratio = price_ratio

    # Final close cost.
    final_exit_cost = current_value * exit_cost_bps / 10000.0 + claim_cost_usd
    total_exit_costs += final_exit_cost
    final_value = current_value - final_exit_cost
    pnl = final_value - position_value_usd

    return {
        "pnl_usd": round(pnl, 4),
        "final_value_usd": round(final_value, 4),
        "total_fees_usd": round(total_fees, 4),
        "total_exit_costs_usd": round(total_exit_costs, 4),
        "rebalances": rebalances,
        "path": path,
    }


def backtest_pools(pool_paths: list, args) -> dict:
    """Replay pool scans; compare OPEN_CANDIDATE forward stats vs baseline."""
    horizon = args.horizon
    half_width = max(1, args.width // 2)
    position_value = args.position_value_usd
    entry_bps = args.entry_cost_bps
    exit_bps = args.exit_cost_bps
    claim_usd = args.claim_cost_usd

    cfg = lp_get_config()
    use_dynamic = getattr(args, "dynamic", False)
    dyn_window = int((cfg.get("dynamic") or {}).get("percentile", {}).get("history_scans", 60))

    scans = []
    prior = []
    for path in pool_paths:
        pools = load_scan(path)
        if pools is None:
            continue
        # Tag each pool with its source path for datetime extraction.
        for p in pools:
            p["_path"] = path
        by_addr = {p.get("pool_address"): p for p in pools
                   if isinstance(p, dict) and p.get("pool_address")}

        ctx = None
        if use_dynamic:
            ctx = dynamic.build_context_from_prior(pools, prior, cfg)

        scored = {addr: score_pool(p, ctx) for addr, p in by_addr.items()}

        # Adaptive pool thresholds require the scored universe.
        if ctx is not None:
            scores = [s["score"] for s in scored.values()
                      if s["pair_class"] not in ("off_universe", "unknown")]
            adaptive_thr = dynamic.adaptive_pool_thresholds(scores, cfg)
            if adaptive_thr:
                ctx["thresholds"] = adaptive_thr
                for s in scored.values():
                    if s["pair_class"] in ("off_universe", "unknown"):
                        continue
                    s["verdict"] = pool_verdict(s["score"], adaptive_thr)
                    s["dynamic"]["thresholds"] = "adaptive"

        scans.append({"path": path, "by_addr": by_addr, "scored": scored,
                      "completeness": scan_completeness(pools), "ctx": ctx})

        if use_dynamic:
            prior.append(pools)
            if len(prior) > dyn_window:
                prior.pop(0)

    if not scans:
        return {"error": "no readable pool scans"}

    opens = []       # one row per OPEN_CANDIDATE observation
    baseline = []    # one row per investable scored pool observation
    for i, scan in enumerate(scans):
        future = scans[i + 1: i + 1 + horizon]
        if not future:
            continue
        for addr, s in scan["scored"].items():
            if s["verdict"] == "IGNORE":
                continue  # off-universe/unparseable: not investable
            apr_later = []
            for f in future:
                p = f["by_addr"].get(addr)
                if p is None:
                    continue
                apr = p.get("realized_fee_apr")
                if apr is not None:
                    apr_later.append(float(apr))
            if not apr_later:
                continue
            still_open = sum(
                1 for f in future
                if addr in f["scored"]
                and f["scored"][addr]["verdict"] == "OPEN_CANDIDATE")
            row = {
                "scan": os.path.basename(scan["path"]),
                "scan_path": scan["path"],
                "pool": s["pool"],
                "pool_address": addr,
                "pair_class": s["pair_class"],
                "score": s["score"],
                "forward_apr": sum(apr_later) / len(apr_later),
                "observed": len(apr_later),
                "still_open_frac": still_open / len(future),
            }
            entry = scan["by_addr"][addr]
            center = _range_center(entry)
            if center == 0 and entry.get("dex") != "meteora":
                center = _derive_tick(entry)
            if center:
                survivals = [price_in_range(entry, f["by_addr"].get(addr), half_width)
                             for f in future]
                survivals = [x for x in survivals if x is not None]
                if survivals:
                    row["range_survival"] = sum(survivals) / len(survivals)

            # Synthetic PnL only when entry scan has prices for both tokens.
            enriched = (
                float(entry.get("token_x_price_usd") or 0) > 0
                and float(entry.get("token_y_price_usd") or 0) > 0
                and float(entry.get("pool_price") or 0) > 0
            )
            if enriched:
                sim = _simulate_position(
                    entry, future, half_width,
                    position_value, entry_bps, exit_bps, claim_usd,
                )
                row["synthetic_pnl"] = sim

            baseline.append(row)
            if s["verdict"] == "OPEN_CANDIDATE":
                opens.append(row)

    def summarize(rows, include_pnl=True):
        if not rows:
            return {"n": 0}
        aprs = sorted(r["forward_apr"] for r in rows)
        surv = [r["range_survival"] for r in rows if "range_survival" in r]
        result = {
            "n": len(rows),
            "mean_forward_apr": round(sum(aprs) / len(aprs), 2),
            "median_forward_apr": round(aprs[len(aprs) // 2], 2),
            "mean_range_survival": (round(sum(surv) / len(surv), 3)
                                    if surv else None),
            "mean_still_open_frac": round(
                sum(r["still_open_frac"] for r in rows) / len(rows), 3),
        }
        if include_pnl:
            pnls = [r["synthetic_pnl"]["pnl_usd"] for r in rows
                    if r.get("synthetic_pnl") and r["synthetic_pnl"]["pnl_usd"] is not None]
            if pnls:
                pnls_sorted = sorted(pnls)
                wins = [p for p in pnls if p > 0]
                result.update({
                    "pnl_mean_usd": round(sum(pnls) / len(pnls), 2),
                    "pnl_median_usd": round(pnls_sorted[len(pnls_sorted) // 2], 2),
                    "pnl_min_usd": round(pnls_sorted[0], 2),
                    "pnl_max_usd": round(pnls_sorted[-1], 2),
                    "hit_rate_pct": round(len(wins) / len(pnls) * 100.0, 1),
                    "max_drawdown_usd": round(min(pnls), 2),
                })
            else:
                result["pnl_note"] = "no enriched rows"
        return result

    by_class = {}
    for r in opens:
        by_class.setdefault(r["pair_class"], []).append(r)

    return {
        "scans": len(scans),
        "completeness_by_scan": [
            {"scan": os.path.basename(s["path"]),
             "pools": len(s["scored"]),
             "price_enriched": round(s["completeness"], 2)}
            for s in scans],
        "open_candidates": {
            "overall": summarize(opens),
            "baseline_all_scored": summarize(baseline),
            "by_pair_class": {k: summarize(v) for k, v in sorted(by_class.items())},
        },
        "open_rows": opens,
    }


def _walk_forward_stats(rows: list) -> dict:
    """Split rows chronologically and compare PnL in first vs second half."""
    if not rows:
        return {"note": "no rows"}
    sorted_rows = sorted(rows, key=lambda r: r["scan_path"])
    mid = len(sorted_rows) // 2
    if mid == 0:
        return {"note": "too few rows"}
    first, second = sorted_rows[:mid], sorted_rows[mid:]

    def stats(sub):
        pnls = [r["synthetic_pnl"]["pnl_usd"] for r in sub
                if r.get("synthetic_pnl") and r["synthetic_pnl"]["pnl_usd"] is not None]
        if not pnls:
            return None
        wins = [p for p in pnls if p > 0]
        return {
            "n": len(pnls),
            "mean_pnl_usd": round(sum(pnls) / len(pnls), 2),
            "median_pnl_usd": round(sorted(pnls)[len(pnls) // 2], 2),
            "hit_rate_pct": round(len(wins) / len(pnls) * 100.0, 1),
            "max_drawdown_usd": round(min(pnls), 2),
        }

    return {
        "first_half": stats(first),
        "second_half": stats(second),
    }


def _bootstrap(rows: list, samples: int = DEFAULT_BOOTSTRAP_SAMPLES, seed: int = 42) -> dict:
    """Bootstrap resample PnL rows and report confidence intervals."""
    pnls = [r["synthetic_pnl"]["pnl_usd"] for r in rows
            if r.get("synthetic_pnl") and r["synthetic_pnl"]["pnl_usd"] is not None]
    if not pnls or len(pnls) < 5:
        return {"note": "too few PnL observations for bootstrap"}

    rng = random.Random(seed)
    n = len(pnls)
    mean_estimates = []
    hit_estimates = []
    for _ in range(samples):
        sample = [rng.choice(pnls) for _ in range(n)]
        mean_estimates.append(sum(sample) / n)
        hit_estimates.append(sum(1 for x in sample if x > 0) / n * 100.0)

    mean_estimates.sort()
    hit_estimates.sort()
    return {
        "samples": samples,
        "n": n,
        "mean_pnl_usd": {
            "point": round(sum(pnls) / len(pnls), 2),
            "ci_5": round(mean_estimates[int(samples * 0.05)], 2),
            "ci_95": round(mean_estimates[int(samples * 0.95)], 2),
        },
        "hit_rate_pct": {
            "point": round(sum(1 for x in pnls if x > 0) / len(pnls) * 100.0, 1),
            "ci_5": round(hit_estimates[int(samples * 0.05)], 1),
            "ci_95": round(hit_estimates[int(samples * 0.95)], 1),
        },
    }


def backtest_positions(pos_paths: list, pools_dir: str, horizon: int) -> dict:
    """Replay position scans; track verdict path and (where enriched) value."""
    pool_scans = {os.path.basename(p): p for p in list_scans(pools_dir, "pool_scan")}

    def nearest_pool_scan(pos_path: str):
        stem = os.path.basename(pos_path)
        candidates = [n for n in pool_scans if n <= stem]
        return pool_scans[candidates[-1]] if candidates else None

    scans = []
    for path in pos_paths:
        positions = load_scan(path)
        if positions is None:
            continue
        pool_map = {}
        ps = nearest_pool_scan(path)
        if ps:
            pools = load_scan(ps) or []
            pool_map = {p.get("pool_address"): p for p in pools
                        if isinstance(p, dict) and p.get("pool_address")}
        scored = []
        for p in positions:
            if not isinstance(p, dict):
                continue
            s = score_position(p, pool_map)
            s["_value_usd"] = float(p.get("current_value_usd") or 0.0)
            scored.append(s)
        scans.append({"path": path, "scored": scored})

    tracks = {}
    for i, scan in enumerate(scans):
        for s in scan["scored"]:
            pid = s.get("position_id")
            if not pid:
                continue
            tracks.setdefault(pid, []).append({
                "scan_index": i,
                "scan": os.path.basename(scan["path"]),
                "verdict": s["verdict"],
                "score": s["score"],
                "value": s["_value_usd"],
            })

    position_paths = []
    with_value_history = 0
    for pid, history in tracks.items():
        first = history[0]
        later = history[1:][:horizon]
        vals = [h["value"] for h in later if h["value"] > 0]
        if vals:
            with_value_history += 1
        row = {
            "position_id": pid,
            "first_verdict": first["verdict"],
            "first_score": first["score"],
            "observations": len(history),
            "verdict_path": [h["verdict"] for h in later],
        }
        if vals:
            row["value_path"] = vals
            if vals[0] > 0:
                row["value_change_pct"] = round(
                    (vals[-1] - vals[0]) / vals[0] * 100.0, 2)
        position_paths.append(row)

    verdict_counts = {}
    for scan in scans:
        for s in scan["scored"]:
            verdict_counts[s["verdict"]] = verdict_counts.get(s["verdict"], 0) + 1

    return {
        "position_scans": len(scans),
        "unique_positions": len(tracks),
        "positions_with_value_history": with_value_history,
        "verdict_counts": verdict_counts,
        "position_paths": position_paths,
        "note": ("current_value_usd only exists in enriched scans (2026-09-25+); "
                 "forward value tracking activates as enriched history accumulates"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Sheldon backtest over Missy scan history")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--pools-dir", default=None)
    ap.add_argument("--positions-dir", default=None)
    ap.add_argument("--horizon", type=int, default=4,
                    help="scans to look ahead (default 4)")
    ap.add_argument("--width", type=int, default=200,
                    help="would-be open range width in bins/ticks (default 200)")
    ap.add_argument("--position-value-usd", type=float, default=DEFAULT_POSITION_VALUE_USD,
                    help="synthetic position size in USD (default 100)")
    ap.add_argument("--entry-cost-bps", type=int, default=DEFAULT_ENTRY_COST_BPS,
                    help="entry swap cost in bps (default 50)")
    ap.add_argument("--exit-cost-bps", type=int, default=DEFAULT_EXIT_COST_BPS,
                    help="exit swap cost in bps (default 50)")
    ap.add_argument("--claim-cost-usd", type=float, default=DEFAULT_CLAIM_COST_USD,
                    help="flat gas/claim cost per close in USD (default 0.02)")
    ap.add_argument("--bootstrap-samples", type=int, default=DEFAULT_BOOTSTRAP_SAMPLES,
                    help="number of bootstrap resamples (default 1000)")
    ap.add_argument("--seed", type=int, default=42,
                    help="random seed for bootstrap")
    ap.add_argument("--dynamic", action="store_true",
                    help="use rolling history + adaptive thresholds + regime weights")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--detail", action="store_true",
                    help="print per-candidate rows")
    args = ap.parse_args()

    pools_dir = args.pools_dir or os.path.join(args.data_dir, "pool_screens")
    positions_dir = args.positions_dir or os.path.join(args.data_dir, "position_scans")

    pool_paths = list_scans(pools_dir, "pool_scan")
    pos_paths = list_scans(positions_dir, "position_scan")
    if not pool_paths:
        print(f"no pool scans under {pools_dir}", file=sys.stderr)
        return 2

    pools_result = backtest_pools(pool_paths, args)
    pos_result = backtest_positions(pos_paths, pools_dir, args.horizon)

    # Robustness metrics need the raw open rows.
    open_rows = pools_result.get("open_rows", [])
    walk_forward = _walk_forward_stats(open_rows)
    bootstrap = _bootstrap(open_rows, samples=args.bootstrap_samples, seed=args.seed)

    if args.json:
        json.dump({
            "pools": pools_result,
            "positions": pos_result,
            "synthetic_pnl": {
                "position_value_usd": args.position_value_usd,
                "entry_cost_bps": args.entry_cost_bps,
                "exit_cost_bps": args.exit_cost_bps,
                "claim_cost_usd": args.claim_cost_usd,
                "summary": pools_result.get("open_candidates", {}).get("overall"),
            },
            "robustness": {
                "walk_forward": walk_forward,
                "bootstrap": bootstrap,
            },
        }, sys.stdout, indent=2)
        print()
        return 0

    half_width = max(1, args.width // 2)
    oc = pools_result.get("open_candidates", {})
    overall = oc.get("overall", {})
    baseline = oc.get("baseline_all_scored", {})
    print(f"Pool replay: {pools_result.get('scans', 0)} scans, "
          f"horizon={args.horizon}, width=±{half_width}")
    print(f"  OPEN_CANDIDATE picks : n={overall.get('n', 0)}  "
          f"mean_fwd_apr={overall.get('mean_forward_apr')}%  "
          f"median={overall.get('median_forward_apr')}%  "
          f"range_survival={overall.get('mean_range_survival')}")
    print(f"  Baseline (all pools) : n={baseline.get('n', 0)}  "
          f"mean_fwd_apr={baseline.get('mean_forward_apr')}%  "
          f"median={baseline.get('median_forward_apr')}%  "
          f"range_survival={baseline.get('mean_range_survival')}")
    for cls, stats in (oc.get("by_pair_class") or {}).items():
        print(f"    {cls:<20} n={stats.get('n', 0)}  "
              f"mean_fwd_apr={stats.get('mean_forward_apr')}%  "
              f"survival={stats.get('mean_range_survival')}")

    all_scans = pools_result.get("completeness_by_scan", [])
    enriched = [c for c in all_scans if c["price_enriched"] > 0.5]
    print(f"  Price-enriched scans (2026-09-25+ era): {len(enriched)} / {len(all_scans)}")

    # Synthetic PnL summary
    print("\nSynthetic position PnL (enriched scans only):")
    print(f"  assumptions: ${args.position_value_usd} position, "
          f"entry {args.entry_cost_bps} bps, exit {args.exit_cost_bps} bps, "
          f"claim ${args.claim_cost_usd}")
    pnl = overall
    if "pnl_mean_usd" in pnl:
        print(f"  OPEN_CANDIDATE: "
              f"mean_pnl=${pnl['pnl_mean_usd']}, "
              f"median=${pnl['pnl_median_usd']}, "
              f"hit_rate={pnl['hit_rate_pct']}%, "
              f"max_drawdown=${pnl['max_drawdown_usd']}, "
              f"range=[{pnl['pnl_min_usd']}, {pnl['pnl_max_usd']}]")
    else:
        print("  no enriched OPEN_CANDIDATE rows (need token prices)")

    # Robustness
    print("\nRobustness:")
    print("  walk-forward:")
    wf = walk_forward
    if "first_half" in wf:
        for half in ("first_half", "second_half"):
            s = wf[half]
            print(f"    {half}: n={s['n']}  mean_pnl=${s['mean_pnl_usd']}  "
                  f"median=${s['median_pnl_usd']}  hit={s['hit_rate_pct']}%  "
                  f"drawdown=${s['max_drawdown_usd']}")
    else:
        print(f"    {wf.get('note')}")

    print("  bootstrap:")
    bs = bootstrap
    if "mean_pnl_usd" in bs:
        m = bs["mean_pnl_usd"]
        h = bs["hit_rate_pct"]
        print(f"    mean_pnl ${m['point']}  CI_5-95: [{m['ci_5']}, {m['ci_95']}]")
        print(f"    hit_rate {h['point']}%  CI_5-95: [{h['ci_5']}, {h['ci_95']}%]")
    else:
        print(f"    {bs.get('note')}")

    print(f"\nPosition replay: {pos_result.get('position_scans', 0)} scans, "
          f"{pos_result.get('unique_positions', 0)} unique positions, "
          f"{pos_result.get('positions_with_value_history', 0)} with value history")
    counts = pos_result.get("verdict_counts", {})
    print("  verdict counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for row in pos_result.get("position_paths", []):
        change = row.get("value_change_pct")
        suffix = f"  value Δ={change}%" if change is not None else ""
        print(f"    {row['position_id'][:20]:<20} first={row['first_verdict']:<8} "
              f"score={row['first_score']}  path={row['verdict_path']}{suffix}")

    if args.detail:
        print("\nOPEN_CANDIDATE rows:")
        for r in pools_result.get("open_rows", []):
            surv = r.get("range_survival")
            pnl = r.get("synthetic_pnl", {})
            pnl_str = ""
            if pnl and pnl.get("pnl_usd") is not None:
                pnl_str = (f" pnl=${pnl['pnl_usd']}"
                           f" fees=${pnl['total_fees_usd']}"
                           f" rb={pnl['rebalances']}")
            print(f"  {r['scan']}  {str(r['pool'])[:16]:<16} "
                  f"score={r['score']:>6.2f} fwd_apr={r['forward_apr']:>7.2f}% "
                  f"survival={surv if surv is not None else '-'}{pnl_str}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
