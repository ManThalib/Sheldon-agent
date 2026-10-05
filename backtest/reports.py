#!/usr/bin/env python3
"""Report generation: comparative analysis and formatted output from backtest results."""

import math


def print_pool_replay(pools_result, args):
    """Print the pool replay section of the backtest report."""
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


def print_synthetic_pnl(pools_result, args):
    """Print the synthetic position PnL summary."""
    oc = pools_result.get("open_candidates", {})
    overall = oc.get("overall", {})
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


def print_robustness(pools_result, args):
    """Print the robustness section (walk-forward + bootstrap)."""
    open_rows = pools_result.get("open_rows", [])
    walk_forward = _walk_forward_stats(open_rows)
    bootstrap = _bootstrap(open_rows, samples=args.bootstrap_samples, seed=args.seed)

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
        print(f"    hit_rate {h['point']}%  CI_5-95: [{h['ci_5']}, {h['ci_5']}%]")
    else:
        print(f"    {bs.get('note')}")


def print_position_replay(pos_result):
    """Print the position replay section of the backtest report."""
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


def print_detail(pools_result):
    """Print detailed per-candidate rows."""
    open_rows = pools_result.get("open_rows", [])
    print("\nOPEN_CANDIDATE rows:")
    for r in open_rows:
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


def _bootstrap(rows: list, samples: int = 1000, seed: int = 42) -> dict:
    """Bootstrap resample PnL rows and report confidence intervals."""
    pnls = [r["synthetic_pnl"]["pnl_usd"] for r in rows
            if r.get("synthetic_pnl") and r["synthetic_pnl"]["pnl_usd"] is not None]
    if not pnls or len(pnls) < 5:
        return {"note": "too few PnL observations for bootstrap"}

    import random
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