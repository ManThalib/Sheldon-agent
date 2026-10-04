"""General helpers used across the scoring engine."""


import argparse
import glob
import json
import math
import os
import re
import sys
import time
from copy import deepcopy
from functools import lru_cache

from scoring.universe import STABLECOINS, HIGH_CAPS


U64_MAX = (1 << 64) - 1


def newest_file(directory: str, prefix: str):
    """Return the newest file matching ``prefix-*.json`` in *directory*,
    excluding files ending with ``.failed`` or ``.invalid``."""
    paths = sorted(
        p for p in glob.glob(os.path.join(directory, prefix + "-*.json"))
        if not p.endswith((".failed", ".invalid"))
    )
    return paths[-1] if paths else None


def load_json(path: str):
    """Load and return JSON from *path*."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Clamp *value* to the inclusive range [lo, hi]."""
    return max(lo, min(hi, value))


def is_fresh(path: str, max_age_seconds: float) -> bool:
    """Return True if *path* modification time is within *max_age_seconds*."""
    try:
        return (time.time() - os.path.getmtime(path)) <= max_age_seconds
    except OSError:
        return False


def _empty_wallet(reason: str) -> dict:
    """Return a zeroed wallet summary with an error reason."""
    return {
        "wallet": None,
        "total_usd": 0.0,
        "idle_usdc": 0.0,
        "dust_total_usdc": 0.0,
        "dust_assets": [],
        "reserved_total_usdc": 0.0,
        "deployable_usdc": 0.0,
        "errors": [reason],
        "source": None,
    }


def load_wallet_scan(directory: str) -> dict:
    """Load and parse the newest Missy wallet scan.

    Returns a capital summary dict (see capital.summarize_wallet).
    On missing/malformed data, returns a zeroed summary with an error.
    """
    path = newest_file(directory, "wallet_screen")
    if not path:
        return _empty_wallet("no wallet scan found")
    try:
        data = load_json(path)
    except Exception as exc:
        return _empty_wallet(f"wallet scan unreadable: {exc}")
    try:
        # NOTE: capital.summarize_wallet is imported at call site if needed
        from capital import summarize_wallet
        summary = summarize_wallet(data)
        summary["source"] = path
        return summary
    except Exception as exc:
        return _empty_wallet(f"wallet scan malformed: {exc}")


def pair_symbols(name: str):
    """Extract (symbol_x, symbol_y) from a pool name like 'SOL-USDC (bin 4)'."""
    if not name:
        return None, None
    base = name.split("(", 1)[0].strip()
    parts = [p.strip().upper() for p in re.split(r"[-/]", base) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


# --- LRU-cached pair classification (moved from lp_scoring.py) ---

_PAIR_SPLIT_RE = re.compile(r"[-/]")


@lru_cache(maxsize=4096)
def _classify_cached(sym_x, sym_y, name):
    if not sym_x or not sym_y:
        name_x, name_y = pair_symbols(name)
        sym_x = sym_x or name_x
        sym_y = sym_y or name_y
    if not sym_x or not sym_y:
        return "unknown", sym_x, sym_y
    in_x = sym_x in STABLECOINS or sym_x in HIGH_CAPS
    in_y = sym_y in STABLECOINS or sym_y in HIGH_CAPS
    if not in_x or not in_y:
        return "off_universe", sym_x, sym_y
    x_stable = sym_x in STABLECOINS
    y_stable = sym_y in STABLECOINS
    if x_stable and y_stable:
        return "stable_stable", sym_x, sym_y
    if x_stable or y_stable:
        return "stable_bluechip", sym_x, sym_y
    return "bluechip_bluechip", sym_x, sym_y


def classify_pair(record: dict):
    """Public wrapper: classify a pool/position record dict."""
    sym_x = (record.get("token_x_symbol") or "").upper() or None
    sym_y = (record.get("token_y_symbol") or "").upper() or None
    name = record.get("name") or record.get("pool_name") or ""
    return _classify_cached(sym_x, sym_y, name)


def classify_pair_from_record(record: dict):
    """Public wrapper: classify a pool/position record dict."""
    return classify_pair(record)