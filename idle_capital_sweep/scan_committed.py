import json
import os
import time
from typing import Optional, Tuple

from .timestamp_utils import _created_epoch
from .signal_usdc import _signal_usdc_committed


def scan_committed_usdc(signals_dir: str, now: Optional[float] = None) -> Tuple[float, int]:
    """Sum USDC committed by Sheldon signals still in George's queues."""
    now = now if now is not None else time.time()
    parent = os.path.dirname(signals_dir.rstrip("/"))
    roots = {signals_dir, os.path.join(parent, "pending"),
             os.path.join(parent, "processed"), os.path.join(parent, "failed_verify")}
    total = 0.0
    count = 0
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".json") or not name.startswith("sheldon-"):
                continue
            try:
                with open(os.path.join(root, name), "r", encoding="utf-8") as fh:
                    sig = json.load(fh)
            except (OSError, ValueError):
                continue
            created = _created_epoch(sig.get("created_at") or "")
            if created and now - created > 86400:
                continue
            amount = _signal_usdc_committed(sig)
            if amount > 0:
                total += amount
                count += 1
    return total, count