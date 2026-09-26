#!/usr/bin/env python3
"""Sheldon backtest: replay historical Missy scans through the scoring engine.

Answers three questions with data instead of intuition:

  1. Do OPEN_CANDIDATE picks earn more forward fee APR than the average pool?
  2. Do would-be open ranges survive (price stays inside) over the horizon?
  3. How do position verdicts play out in forward value terms?

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

Caveats
-------
- Scans before 2026-09-25 15:16 UTC lack Jupiter price enrichment
  (token_*_price_usd), which lowers depeg/fee component scores. Per-scan
  input completeness is reported so eras can be segmented.
- realized_fee_apr is a trailing-window metric, not truly forward-looking.

Stdlib only. Run:
  python3 backtest.py [--data-dir /data/missy-data] [--horizon 4]
                      [--width 200] [--json] [--detail]
"""

import argparse
import glob
import json
import math
import os
import sys

from lp_scoring import score_pool, score_position

DEFAULT_DATA_DIR = "/data/missy-data"


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


def backtest_pools(pool_paths: list, horizon: int, half_width: int) -> dict:
    """Replay pool scans; compare OPEN_CANDIDATE forward stats vs baseline."""
    scans = []
    for path in pool_paths:
        pools = load_scan(path)
        if pools is None:
            continue
        by_addr = {p.get("pool_address"): p for p in pools
                   if isinstance(p, dict) and p.get("pool_address")}
        scored = {addr: score_pool(p) for addr, p in by_addr.items()}
        scans.append({"path": path, "by_addr": by_addr, "scored": scored,
                      "completeness": scan_completeness(pools)})

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
            baseline.append(row)
            if s["verdict"] == "OPEN_CANDIDATE":
                opens.append(row)

    def summarize(rows):
        if not rows:
            return {"n": 0}
        aprs = sorted(r["forward_apr"] for r in rows)
        surv = [r["range_survival"] for r in rows if "range_survival" in r]
        return {
            "n": len(rows),
            "mean_forward_apr": round(sum(aprs) / len(aprs), 2),
            "median_forward_apr": round(aprs[len(aprs) // 2], 2),
            "mean_range_survival": (round(sum(surv) / len(surv), 3)
                                    if surv else None),
            "mean_still_open_frac": round(
                sum(r["still_open_frac"] for r in rows) / len(rows), 3),
        }

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

    half_width = max(1, args.width // 2)
    pools_result = backtest_pools(pool_paths, args.horizon, half_width)
    pos_result = backtest_positions(pos_paths, pools_dir, args.horizon)

    if args.json:
        json.dump({"pools": pools_result, "positions": pos_result},
                  sys.stdout, indent=2)
        print()
        return 0

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
            print(f"  {r['scan']}  {str(r['pool'])[:16]:<16} "
                  f"score={r['score']:>6.2f} fwd_apr={r['forward_apr']:>7.2f}% "
                  f"survival={surv if surv is not None else '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
