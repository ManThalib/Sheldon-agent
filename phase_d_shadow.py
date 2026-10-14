#!/usr/bin/env python3
"""Sheldon Phase D — Shadow Mode Evaluation.

Tracks rotation recommendations and subsequent outcomes without execution.
Minimum 7 days and 20 distinct evaluated opportunities; extend if samples
insufficient. Reports net return, drawdown, turnover, cost drag, failed
transitions, and exposure metrics.

Shadow mode never writes George-schema signals. All outcomes are synthetic
/ observational only.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import lp_scoring
from dynamic import expected_pnl_verdict
from planner import compare_4way, check_guardrails
from capital import summarize_wallet


# --------------------------------------------------------------------------
# Shadow-mode state (persists across runs in state/shadow_state.json)
# --------------------------------------------------------------------------
SHADOW_STATE_FILENAME = "shadow_state.json"


def _shadow_state_path(state_dir: str) -> str:
    return os.path.join(state_dir, SHADOW_STATE_FILENAME)


def _load_shadow_state(state_dir: str) -> dict:
    path = _shadow_state_path(state_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {
        "evaluated_opportunities": [],
        "day_0": _today_str(),
        "symbols_tracked": set(),
    }


def _save_shadow_state(state_dir: str, state: dict) -> None:
    path = _shadow_state_path(state_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def _today_str():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Opportunity capture: one per cycle when a ROTATE is recommended by planner
# --------------------------------------------------------------------------
def capture_opportunity(state_dir: str, recommendation: dict) -> dict:
    """Record a rotation recommendation for shadow-mode tracking.

    Called by the Phase B planner after it generates a rotation_plan.json.
    The recommendation dict should contain at minimum:
      - position_pool_address
      - best_action
      - best_expected_usd_24h
      - candidate_pool_address
      - guardrail_outcome
    """
    state = _load_shadow_state(state_dir)

    # Ensure we have a set for symbols_tracked
    if not isinstance(state.get("symbols_tracked"), list):
        state["symbols_tracked"] = set()

    # Add this opportunity with a timestamp
    opportunity = {
        "id": len(state.get("evaluated_opportunities", [])),
        "timestamp": _today_str(),
        "position_pool_address": recommendation.get("position_pool_address"),
        "best_action": recommendation.get("best_action"),
        "best_expected_usd_24h": recommendation.get("best_expected_usd_24h"),
        "candidate_pool_address": recommendation.get("candidate_pool_address"),
        "guardrail_outcome": recommendation.get("guardrail_outcome"),
    }

    state.setdefault("evaluated_opportunities", []).append(opportunity)
    _save_shadow_state(state_dir, state)
    return state


# --------------------------------------------------------------------------
# Outcome recording: after a rotation attempt (or non-attempt), record result
# --------------------------------------------------------------------------
def record_outcome(state_dir: str, opportunity_id: int, outcome: dict) -> dict:
    """Record the actual outcome of a rotation opportunity.

    outcome dict should contain:
      - final_pnl_usd: actual PnL from the transition (if executed)
      - executed: bool whether rotation was actually executed
      - final_value_usd: portfolio value after the cycle
      - reason: human-readable reason for outcome
    """
    state = _load_shadow_state(state_dir)
    opportunities = state.get("evaluated_opportunities", [])
    if opportunity_id < 0 or opportunity_id >= len(opportunities):
        raise IndexError("Opportunity ID out of range")

    # Attach outcome to the opportunity
    opportunities[opportunity_id]["outcome"] = outcome
    opportunities[opportunity_id]["outcome_timestamp"] = _today_str()
    _save_shadow_state(state_dir, state)
    return state


# --------------------------------------------------------------------------
# Phase D: Shadow-mode evaluation report
# --------------------------------------------------------------------------
def generate_shadow_report(state_dir: str, min_opportunities: int = 20,
                          min_days: int = 7) -> dict:
    """Generate the Phase D shadow-mode evaluation report.

    Checks that we have at least `min_opportunities` evaluated opportunities
    spanning at least `min_days` distinct days. If insufficient, reports
    how many more are needed.

    Returns a dict with:
      - n_evaluated: number of opportunities captured
      - n_days: number of distinct days spanned
      - met_threshold: bool whether min_opportunities & min_days satisfied
      - metrics: computed metrics (PnL, turnover, drawdown, etc.)
      - recommendations: go/no-go recommendation for Phase E
    """
    state = _load_shadow_state(state_dir)
    opportunities = state.get("evaluated_opportunities", [])

    n_evaluated = len(opportunities)

    # Count distinct days
    distinct_days = set()
    for opp in opportunities:
        ts = opp.get("timestamp", "")
        if ts:
            distinct_days.add(ts[:10])  # YYYY-MM-DD
    n_days = len(distinct_days)

    met_threshold = n_evaluated >= min_opportunities and n_days >= min_days

    # Compute metrics if we have enough data with outcomes
    metrics = {
        "n_evaluated": n_evaluated,
        "n_days": n_days,
        "met_threshold": met_threshold,
        "pnl_usd": None,
        "turnover_n": 0,
        "cost_drag_usd": 0.0,
        "failed_transitions": 0,
        "exposure_violations": 0,
        "go_no_go": "NO_GO",
    }

    if not met_threshold:
        metrics["note"] = (
            f"Insufficient shadow-mode samples: {n_evaluated} opportunities "
            f"over {n_days} days (need {min_opportunities} over {min_days})"
        )
        metrics["go_no_go"] = "NO_GO_INSUFFICIENT_SAMPLES"
        return {"shadow_report": metrics}

    # We have enough data — compute metrics from outcomes
    pnls = []
    turnovers = []
    failed_transitions = 0
    exposure_violations = 0

    for opp in opportunities:
        outcome = opp.get("outcome")
        if outcome is None:
            continue

        # PnL
        pnl = outcome.get("final_pnl_usd")
        if pnl is not None:
            pnls.append(pnl)

        # Turnover count (if executed)
        executed = outcome.get("executed", False)
        if executed:
            turnovers.append(1)
            # Cost drag: entry + exit + swap + claim
            cost = outcome.get("cost_drag_usd", 0.0)
            cost_drag = cost if cost else 0.0
            if cost_drag:
                # aggregate simply
                pass

        # Failed transitions
        if not executed or outcome.get("reason", "").startswith("FAIL"):
            failed_transitions += 1

        # Exposure violations (simple check: did we preserve position/token/DEX)
        # In shadow mode we don't have full exposure data, so we note it
        exposure_violations += int(outcome.get("exposure_violation", False))

    if pnls:
        metrics["pnl_usd"] = {
            "mean": round(sum(pnls) / len(pnls), 4),
            "median": round(sorted(pnls)[len(pnls) // 2], 4),
            "min": round(min(pnls), 4),
            "max": round(max(pnls), 4),
            "count": len(pnls),
        }

    if turnovers:
        metrics["turnover_n"] = len(turnovers)
        metrics["cost_drag_usd"] = round(
            sum([o.get("cost_drag_usd", 0.0) for o in outcomes if o.get("executed")]), 4,
        )

    metrics["failed_transitions"] = failed_transitions
    metrics["exposure_violations"] = exposure_violations

    # Go/no-go recommendation
    # Pass only if: met_threshold, positive mean PnL, failed_transitions < n_evaluated * 0.5,
    # and exposure_violations == 0
    pnl_mean = metrics.get("pnl_usd", {}).get("mean", 0) if metrics.get("pnl_usd") else 0
    pass_conditions = [
        met_threshold,
        pnl_mean > 0 if isinstance(pnl_mean, (int, float)) else False,
        failed_transitions < n_evaluated * 0.5 if n_evaluated > 0 else False,
        exposure_violations == 0,
    ]
    metrics["go_no_go"] = "GO" if all(pass_conditions) else "NO_GO"

    # Add summary of pass/fail reasons
    reasons = []
    if not met_threshold:
        reasons.append("insufficient_samples")
    if not isinstance(pnl_mean, (int, float)) or pnl_mean <= 0:
        reasons.append("non_positive_pnl")
    if failed_transitions >= n_evaluated * 0.5 if n_evaluated > 0 else True:
        reasons.append("high_failure_rate")
    if exposure_violations > 0:
        reasons.append("exposure_violations")
    metrics["pass_reasons"] = [r for r in ["threshold", "positive_pnl", "low_failure_rate", "no_exposure_violations"] if r not in reasons]
    metrics["fail_reasons"] = reasons

    return {"shadow_report": metrics}


# --------------------------------------------------------------------------
# CLI entry point
# --------------------------------------------------------------------------
def main() -> int:
    ap = __import__("argparse").ArgumentParser(description="Sheldon Phase D shadow-mode evaluation")
    ap.add_argument("--state-dir", default="state", help="Path to state directory for shadow state")
    ap.add_argument("--output", default="shadow_report.json", help="Output report file path")
    ap.add_argument("--min-opportunities", type=int, default=20, help="Minimum evaluated opportunities")
    ap.add_argument("--min-days", type=int, default=7, help="Minimum distinct days spanned")
    args = ap.parse_args()

    # Generate report
    report = generate_shadow_report(args.state_dir, args.min_opportunities, args.min_days)
    shadow_report = report["shadow_report"]

    # Write output
    out_path = args.output
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True) if os.path.dirname(out_path) else None
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)

    # Print summary to stdout
    print(f"Shadow-mode report written to {out_path}")
    print(f"Evaluated opportunities: {shadow_report['n_evaluated']}")
    print(f"Distinct days: {shadow_report['n_days']}")
    print(f"Threshold met: {shadow_report['met_threshold']}")
    print(f"Go/No-Go: {shadow_report['go_no_go']}")
    if shadow_report.get("pass_reasons"):
        print(f"Pass reasons: {', '.join(shadow_report['pass_reasons'])}")
    if shadow_report.get("fail_reasons"):
        print(f"Fail reasons: {', '.join(shadow_report['fail_reasons'])}")
    if shadow_report.get("pnl_usd"):
        p = shadow_report["pnl_usd"]
        print(f"PnL mean: {p['mean']} USD (n={p['count']})")
    if shadow_report.get("turnover_n"):
        print(f"Turnovers: {shadow_report['turnover_n']}")
    if shadow_report.get("failed_transitions") is not None:
        print(f"Failed transitions: {shadow_report['failed_transitions']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())