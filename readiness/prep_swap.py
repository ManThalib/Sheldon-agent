#!/usr/bin/env python3
"""Build George-schema 'swap' signals from prep specs.

Mirrors the `build_prep_swap_signal` function from the original readiness.py.
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict


def _utc_iso():
    """Return current time in Asia/Shanghai (UTC+8) ISO format."""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def build_prep_swap_signal(spec: Dict[str, Any], idx: int, base: int) -> Dict[str, Any]:
    """Build a George-schema 'swap' signal from a prep spec.

    ``amount`` is in the INPUT mint's base units (Jupiter ExactIn).
    """
    if spec["direction"] == "buy":
        reason = (
            f"CAPITAL PREP buy {spec['symbol']}: need {spec['need_ui']:.6f}, "
            f"have {spec['have_ui']:.6f} (SOL reserve "
            f"{20_000_000 / 1e9:.4f} kept) for {spec['for_pool']}"
        )
    else:
        reason = (
            f"CAPITAL PREP sell {spec['symbol']} surplus {spec['have_ui']:.6f} "
            f"(~${spec['usd']:.2f}) back to USDC after funding opens"
        )
    return {
        "signal_id": f"sheldon-prep-{base}-{idx}",
        "action": "swap",
        "input_mint": spec["input_mint"],
        "output_mint": spec["output_mint"],
        "amount": str(spec["amount_raw"]),
        "max_slippage_bps": 100,
        "direction": spec["direction"],
        "prep_for_pool": spec.get("for_pool"),
        "reason": reason,
        "created_at": _utc_iso(),
    }