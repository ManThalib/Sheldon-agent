#!/usr/bin/env python3
"""Sheldon cycle runner.

Runs the deterministic LP scoring engine and, if configured, emits George-schema
signal files. Zero model reasoning. Outputs: JSON report, human summary log line,
and optional signal JSON files for George.

Usage:
    python3 run_cycle.py [--write-signals] [--signals-dir /path] [--json]
Returns exit 0 on success, 1 on fatal error, 2 on stale/missing data.
"""

import argparse
import json
import os
import sys
import time

from lp_scoring import newest_file, load_json
import lp_scoring

# Where George's executor looks for pending signals.
DEFAULT_SIGNALS_DIR = "/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/signals/pending"
DEFAULT_MEMORY_DIR = "/data/.openclaw/workspace-agents/sheldon/memory"


def _utc_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_active_positions(positions_dir: str) -> dict:
    """Return dict of pool_address -> position dict for currently active positions."""
    pos_path = newest_file(positions_dir, "position_scan")
    if not pos_path:
        return {}
    try:
        data = load_json(pos_path)
    except Exception:
        return {}
    positions = data.get("positions", []) if isinstance(data, dict) else data
    return {p.get("pool_address"): p for p in positions if p.get("pool_address")}


def write_signals(report: dict, signals_dir: str, positions_dir: str) -> tuple:
    """Convert verdicts into George-schema signal files.

    Returns:
        (created_paths, review_items)
        created_paths: list of files written to George's pending queue.
        review_items:  list of OPEN candidates that need bin/liquidity review.
    """
    os.makedirs(signals_dir, exist_ok=True)
    active = _load_active_positions(positions_dir)
    created = []
    review = []
    base = int(time.time())
    idx = 1

    for v in report.get("verdicts", []):
        action = v.get("action")
        pool_addr = v.get("pool_address")

        # CLOSE / COLLECT_FEES: only emit for protocols George can execute.
        dex = v.get("dex") or "unknown"
        if dex != "meteora":
            review.append({
                "pool_address": pool_addr,
                "dex": dex,
                "score": v.get("score"),
                "evidence": v.get("evidence"),
                "reason": f"{action} candidate ({dex}); only Meteora signals are queued",
            })
            continue
        if action in {"CLOSE", "COLLECT_FEES"}:
            lower = v.get("lower_bound")
            upper = v.get("upper_bound")
            try:
                lower = int(lower) if lower is not None else 0
                upper = int(upper) if upper is not None else 0
            except (TypeError, ValueError):
                lower, upper = 0, 0
            signal = {
                "signal_id": f"sheldon-{base}-{idx}",
                "action": "close" if action == "CLOSE" else "claim_fees",
                "pool_address": pool_addr,
                "position_id": v.get("position"),
                "side": "bidirectional",
                "bin_range": {"lower": lower, "upper": upper},
                "liquidity": {"amount_x": "0", "amount_y": "0"},
                "max_slippage_bps": 100,
                "reason": f"{action} score={v.get('score')}; evidence={json.dumps(v.get('evidence'))}",
                "created_at": _utc_iso(),
            }
        elif action == "OPEN_CANDIDATE":
            # OPEN signals stay in review until allocation logic is finalized.
            # George currently only executes Meteora DLMM, so non-Meteora
            # candidates are noted but not queued.
            dex = v.get("dex") or "unknown"
            if dex == "meteora":
                reason = "OPEN candidate; bin_range/liquidity need allocation data"
            else:
                reason = f"OPEN candidate ({dex}); only Meteora signals are queued"
            review.append({
                "pool_address": pool_addr,
                "dex": dex,
                "score": v.get("score"),
                "evidence": v.get("evidence"),
                "reason": reason,
            })
            continue
        else:
            # HOLD, REVIEW, WATCH, IGNORE => no signal.
            continue

        path = os.path.join(signals_dir, f"{signal['signal_id']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(signal, fh, indent=2)
            fh.write("\n")
        created.append(path)
        idx += 1

    return created, review


def append_log(report: dict, memory_dir: str, signals_created: list, review: list):
    os.makedirs(memory_dir, exist_ok=True)
    log_path = os.path.join(memory_dir, time.strftime("%Y-%m-%d") + ".md")
    lines = [
        f"## Cycle — {_utc_iso()}",
        f"- sources: pools={report.get('sources', {}).get('pools')} positions={report.get('sources', {}).get('positions')}",
        f"- verdicts: {len(report.get('verdicts', []))}",
    ]
    if report.get("failures"):
        lines.append(f"- failures: {report['failures']}")
    if review:
        lines.append(f"- review needed (OPEN candidates): {len(review)}")
        for r in review:
            lines.append(f"  - `{r['pool_address']}` score={r['score']} — {r['reason']}")
    if signals_created:
        lines.append(f"- signals written: {len(signals_created)}")
        for p in signals_created:
            lines.append(f"  - `{p}`")
    else:
        lines.append("- signals written: 0 (no actionable verdicts)")
    lines.append("- summary: " + _short_summary(report, review))
    lines.append("")
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _short_summary(report: dict, review: list = None) -> str:
    parts = []
    opens = [v for v in report.get("verdicts", []) if v.get("action") == "OPEN_CANDIDATE"]
    closes = [v for v in report.get("verdicts", []) if v.get("action") == "CLOSE"]
    collects = [v for v in report.get("verdicts", []) if v.get("action") == "COLLECT_FEES"]
    if opens or (review and len(review) > 0):
        parts.append(f"OPEN={len(opens)}")
    if closes:
        parts.append(f"CLOSE={len(closes)}")
    if collects:
        parts.append(f"COLLECT={len(collects)}")
    if not parts:
        return "no actionable verdicts"
    return " | ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sheldon deterministic cycle runner")
    ap.add_argument("--pools-dir", default="/data/missy-data/pool_screens")
    ap.add_argument("--positions-dir", default="/data/missy-data/position_scans")
    ap.add_argument("--write-signals", action="store_true")
    ap.add_argument("--signals-dir", default=DEFAULT_SIGNALS_DIR)
    ap.add_argument("--memory-dir", default=DEFAULT_MEMORY_DIR)
    ap.add_argument("--json", action="store_true", help="print full JSON report to stdout")
    ap.add_argument("--max-age-seconds", type=float, default=3900.0)
    args = ap.parse_args()

    report = lp_scoring.run_cycle(args.pools_dir, args.positions_dir, args.max_age_seconds)

    if report.get("failures"):
        err = "; ".join(report["failures"])
        print(f"ERROR: {err}", file=sys.stderr)
        if args.json:
            json.dump(report, sys.stdout, indent=2)
            print()
        return 2

    signals_created = []
    review = []
    if args.write_signals:
        signals_created, review = write_signals(report, args.signals_dir, args.positions_dir)

    append_log(report, args.memory_dir, signals_created, review)

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(_short_summary(report, review))

    return 0


if __name__ == "__main__":
    sys.exit(main())
