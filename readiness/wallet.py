#!/usr/bin/env python3
"""Raw wallet scan loading for Sheldon readiness gates.

Reads Missy's newest wallet scan and returns structured asset data.
Mirrors the `load_raw_wallet` function from the original readiness.py.
"""

import glob
import json
import os
from typing import Any, Dict, List, Optional


def newest_wallet_scan(wallet_scans_dir: str, prefix: str = "wallet_screen") -> Optional[str]:
    """Find the newest wallet scan file in the directory."""
    paths = sorted(
        p for p in glob.glob(os.path.join(wallet_scans_dir, prefix + "-*.json"))
        if not p.endswith((".failed", ".invalid"))
    )
    return paths[-1] if paths else None


def load_raw_wallet(wallet_scans_dir: str, wallet_id: str = "main") -> Dict[str, Any]:
    """Load the newest raw Missy wallet scan for ``wallet_id``.

    MAIN (the policy wallet) reads ``wallet_screen-latest.json`` — the
    symlink the MAIN cron always refreshes. Mirror wallets (C.2+) read
    ``wallet_screen-<id>-latest.json`` when their own cron exists; until a
    mirror cron runs, mirrors have no scan and funding for mirror legs is
    the mirror's own business (George's registry gates on registered keys,
    not on scans).
    """
    import lp_scoring  # local import: lp_scoring owns file discovery

    if wallet_id != "main":
        mirror_path = os.path.join(
            wallet_scans_dir, f"wallet_screen-{wallet_id}-latest.json"
        )
        if not os.path.exists(mirror_path):
            return {
                "path": mirror_path, "mtime": 0.0, "assets": [],
                "error": f"no wallet scan for wallet_id '{wallet_id}'",
            }
        path = mirror_path
    else:
        path = newest_wallet_scan(wallet_scans_dir, "wallet_screen")
    if not path:
        return {"path": None, "mtime": 0.0, "assets": [], "error": "no wallet scan found"}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return {"path": path, "mtime": 0.0, "assets": [], "error": f"wallet scan unreadable: {exc}"}
    try:
        mtime = os.stat(path).st_mtime  # follows the latest.json symlink
    except OSError:
        mtime = 0.0
    assets = data.get("assets") if isinstance(data, dict) else None
    if not isinstance(assets, list):
        return {"path": path, "mtime": mtime, "assets": [], "error": "wallet scan malformed (no assets)"}
    return {"path": path, "mtime": mtime, "assets": assets, "error": None}