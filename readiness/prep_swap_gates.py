#!/usr/bin/env python3
"""Prep swap gating for Sheldon readiness module.

Filter prep swap specs through the rescan-wait and hourly loop guards.

Mirrors the `gate_prep_swaps` function from the original readiness.py.
"""

import glob
import json
import os
import time
from typing import Any, List, Dict, Optional, Tuple

# Constants mirror from readiness top-level
PREP_CONFIRM_GRACE_SECONDS = 60
MAX_PREP_SWAPS_PER_MINT_PER_HOUR = 3
MAX_WALLET_SCAN_AGE_SECONDS = 180


def _prep_swap_history(signals_dir: str) -> List[Dict[str, Any]]:
    """Prep swap history from the pending and processed queues.

    ``signals_dir`` may be either the signals root or its pending subdir
    (run_cycle passes George's pending dir); scan both layouts.
    """
    search_dirs = {signals_dir}
    parent = os.path.dirname(signals_dir.rstrip("/"))
    search_dirs.add(os.path.join(parent, "pending"))
    search_dirs.add(os.path.join(parent, "processed"))
    history = []
    for d in search_dirs:
        for path in glob.glob(os.path.join(d, "sheldon-prep-*.json")):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    sig = json.load(fh)
            except Exception:
                continue
            if sig.get("action") != "swap":
                continue
            history.append({
                "signal_id": sig.get("signal_id") or os.path.basename(path),
                "input_mint": sig.get("input_mint"),
                "output_mint": sig.get("output_mint"),
                "created_epoch": _created_epoch(sig.get("created_at") or ""),
                "path": path,
            })
    return history


def _created_epoch(created_at: str) -> float:
    """Parse ISO-8601 timestamp to Unix epoch."""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError):
        return 0.0


def gate_prep_swaps(
    prep_specs: List[Dict[str, Any]],
    signals_dir: str,
    wallet_mtime: float,
    now: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Filter prep swap specs through the rescan-wait and hourly loop guards.

    A spec is blocked when the newest prior prep swap for the same mint pair
    is newer than the wallet scan (rescan has not caught up yet), or when
    more than MAX_PREP_SWAPS_PER_MINT_PER_HOUR swaps for that pair were
    emitted in the last hour (loop not converging).
    """
    now = now if now is not None else time.time()
    history = _prep_swap_history(signals_dir)
    allowed: List[Dict[str, Any]] = []
    blocked: List[Dict[str, str]] = []

    scan_age = (now - wallet_mtime) if wallet_mtime else None
    if scan_age is None or scan_age > MAX_WALLET_SCAN_AGE_SECONDS:
        reason = (f"wallet scan stale or missing "
                  f"(age {scan_age:.0f}s > {MAX_WALLET_SCAN_AGE_SECONDS}s)"
                  if scan_age is not None else "no wallet scan mtime")
        for spec in prep_specs:
            blocked.append({"direction": spec["direction"],
                            "output_mint": spec["output_mint"],
                            "reason": reason})
        return allowed, blocked

    for spec in prep_specs:
        pair = (spec["input_mint"], spec["output_mint"])
        matches = [h for h in history
                   if (h["input_mint"], h["output_mint"]) == pair and h["created_epoch"] > 0]
        if matches:
            last = max(matches, key=lambda h: h["created_epoch"])
            if wallet_mtime < last["created_epoch"] + PREP_CONFIRM_GRACE_SECONDS:
                blocked.append({
                    "direction": spec["direction"],
                    "output_mint": spec["output_mint"],
                    "reason": (f"confirmation grace: wallet rescan must be taken "
                               f">={ PREP_CONFIRM_GRACE_SECONDS }s after last prep swap "
                               f"{last['signal_id']}"),
                })
                continue
            recent = [h for h in matches if h["created_epoch"] >= now - 3600]
            if len(recent) >= MAX_PREP_SWAPS_PER_MINT_PER_HOUR:
                blocked.append({
                    "direction": spec["direction"],
                    "output_mint": spec["output_mint"],
                    "reason": (f"loop guard: {len(recent)} prep swaps for this mint pair "
                               f"in the last hour"),
                })
                continue
        allowed.append(spec)
    return allowed, blocked