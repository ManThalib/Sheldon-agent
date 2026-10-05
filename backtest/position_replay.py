#!/usr/bin/env python3
"""Position replay logic: replay position scans; track verdict path and (where
enriched) value.

Scores every historical position scan. For each position address, records
the first verdict and the verdict path over the next `horizon` scans.
current_value_usd only exists in enriched scans (2026-09-25+); when a
position has positive values in later scans, a value trajectory and
change percentage are attached.
"""

import glob
import json
import os
from datetime import datetime

from lp_scoring import score_position


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


def _scan_dt(path: str) -> datetime:
    """Extract datetime from a Missy scan filename like position_scan-20260922-121433.json."""
    stem = os.path.basename(path)
    for prefix in ("position_scan-", "pool_scan-"):
        if stem.startswith(prefix):
            dt_part = stem[len(prefix):].rsplit(".", 1)[0]
            try:
                return datetime.strptime(dt_part, "%Y%m%d-%H%M%S")
            except ValueError:
                pass
    return datetime.fromtimestamp(0)


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