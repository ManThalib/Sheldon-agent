#!/usr/bin/env python3
"""Token-light wrapper for Sheldon's LP scoring engine.
Runs lp_scoring.py, writes George signals for CLOSE/claim_fees, returns 3 when
an OPEN candidate needs agent sizing, and exits non-zero for errors.
"""
import json
import os
import sys
import subprocess
from datetime import datetime, timezone

BASE = "/data/.openclaw/workspace-agents/sheldon"
ENGINE = f"{BASE}/scoring/lp_scoring.py"
PENDING = "/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/signals/pending"
LOG_DIR = f"{BASE}/memory"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_entry(line: str) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = f"{LOG_DIR}/{today}.md"
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"- {line}\n")


def write_signal(action: str, pool: str, pool_address: str, evidence: dict,
                 position_id: str = None) -> None:
    os.makedirs(PENDING, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    signal_id = f"sheldon-{ts}-{action}-{pool.replace(' ', '_')}"
    payload = {
        "signal_id": signal_id,
        "action": action,
        "pool_address": pool_address,
        "side": "bidirectional",
        "bin_range": {"lower": 0, "upper": 0},
        "liquidity": {"amount_x": "0", "amount_y": "0"},
        "max_slippage_bps": 100,
        "reason": f"{action} from script; evidence={json.dumps(evidence)}",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    if position_id:
        payload["position_id"] = position_id
    path = os.path.join(PENDING, f"{signal_id}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def run() -> int:
    result = subprocess.run(
        [sys.executable, ENGINE, "--json"],
        capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print(f"ENGINE_ERROR: lp_scoring.py failed: {result.stderr}", file=sys.stderr)
        return 2
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"PARSE_ERROR: {exc}", file=sys.stderr)
        return 2

    if report.get("failures"):
        print(f"DATA_ERROR: {report['failures']}", file=sys.stderr)
        return 2

    signals_written = 0
    open_candidates = 0

    for p in report.get("pool_scores", []):
        if p["verdict"] == "OPEN_CANDIDATE":
            open_candidates += 1

    for s in report.get("position_scores", []):
        pos_id = s.get("position_id") or s.get("pool")
        if s["verdict"] == "CLOSE":
            write_signal("close", s["pool"], s["pool"], s["components"], position_id=pos_id)
            signals_written += 1
        if s.get("collect_fees"):
            write_signal("claim_fees", s["pool"], s["pool"], s["components"], position_id=pos_id)
            signals_written += 1

    top = sorted(report.get("pool_scores", []), key=lambda x: x["score"], reverse=True)[:3]
    top_str = ", ".join(f"{t['pool']}={t['score']:.1f}/{t['verdict']}" for t in top)
    log_entry(
        f"script-cycle {now()}: signals={signals_written}, open_candidates={open_candidates}, "
        f"top=[{top_str}], positions={len(report.get('position_scores', []))}"
    )

    print(f"OK signals={signals_written} open_candidates={open_candidates}")
    if open_candidates:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(run())
