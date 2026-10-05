#!/usr/bin/env python3
"""Synthetic PnL simulation: simulate a 50/50 position through the horizon,
including fees, impermanent loss, rebalancing when price leaves the range,
and realistic swap/gas costs.

Caveats: ignores partial fills, MEV, exact bin/tick rounding, and
protocol-specific DLMM/CLMM fee mechanics. Treat as directional, not exact.
"""

import math
import os
from datetime import datetime


def _scan_dt(path: str) -> datetime:
    """Extract datetime from a Missy scan filename."""
    stem = os.path.basename(path)
    for prefix in ("pool_scan-", "position_scan-"):
        if stem.startswith(prefix):
            dt_part = stem[len(prefix):].rsplit(".", 1)[0]
            try:
                return datetime.strptime(dt_part, "%Y%m%d-%H%M%S")
            except ValueError:
                pass
    return datetime.fromtimestamp(0)


def _impermanent_loss(price_ratio: float) -> float:
    """Classic 50/50 LP impermanent loss as a fraction of value."""
    if price_ratio <= 0:
        return 0.0
    return 2.0 * math.sqrt(price_ratio) / (1.0 + price_ratio) - 1.0


def _simulate_position(
    entry_pool: dict,
    future: list,
    half_width: int,
    position_value_usd: float,
    entry_cost_bps: int,
    exit_cost_bps: int,
    claim_cost_usd: float,
) -> dict:
    """Simulate a 50/50 position through future scans.

    Returns a dict with PnL breakdown and a per-step path.

    Caveats:
      - Ignores partial fills, MEV, exact bin/tick rounding, and
        protocol-specific DLMM/CLMM fee mechanics.
      - Treat as directional, not exact.
    """
    px_x0 = float(entry_pool.get("token_x_price_usd") or 0.0)
    px_y0 = float(entry_pool.get("token_y_price_usd") or 0.0)
    if px_x0 <= 0 or px_y0 <= 0:
        return {"pnl_usd": None, "error": "missing token prices"}

    # Entry: 50/50 USD split, then pay entry swap cost.
    value_x = position_value_usd / 2.0
    value_y = position_value_usd / 2.0
    entry_cost = position_value_usd * entry_cost_bps / 10000.0
    net_value = position_value_usd - entry_cost

    path = []
    rebalances = 0
    total_fees = 0.0
    total_exit_costs = 0.0

    # Current state tracks the *net portfolio value* invested in the pool.
    current_value = net_value
    entry_price_ratio = 1.0  # ratio of current price to entry price

    entry_dt = _scan_dt(entry_pool.get("_path", ""))
    prev_dt = entry_dt

    for i, f in enumerate(future):
        later_pool = f["by_addr"].get(entry_pool.get("pool_address"))
        if later_pool is None:
            continue

        later_dt = _scan_dt(later_pool.get("_path", f["path"]))

        px_x1 = float(later_pool.get("token_x_price_usd") or 0.0)
        px_y1 = float(later_pool.get("token_y_price_usd") or 0.0)
        if px_x1 <= 0 or px_y1 <= 0:
            continue

        # Price ratio vs entry, using the pool price (price of X in terms of Y).
        price0 = float(entry_pool.get("pool_price") or 0.0)
        price1 = float(later_pool.get("pool_price") or 0.0)
        if price0 > 0 and price1 > 0:
            price_ratio = price1 / price0
        else:
            price_ratio = (px_x1 / px_x0) / (px_y1 / px_y0) if px_y1 and px_x0 else 1.0

        # Impermanent loss since entry, applied to current value.
        il = _impermanent_loss(price_ratio)
        gross_value = current_value * (1.0 + il)

        # Fees earned over this step, scaled to this position's size.
        # Use incremental time since the previous observation.
        delta_years = max(0.0, (later_dt - prev_dt).total_seconds() / (365.25 * 24 * 3600))
        apr = float(later_pool.get("realized_fee_apr") or 0.0)
        fees = current_value * (apr / 100.0) * delta_years
        total_fees += fees
        prev_dt = later_dt

        pre_rebalance_value = gross_value + fees

        # Determine if price is still inside the configured range.
        # Inline price_in_range logic (same as pool_replay.py).
        dex = entry_pool.get("dex")
        step = float(entry_pool.get("bin_step") or entry_pool.get("tick_spacing") or 0)
        p0 = float(entry_pool.get("pool_price") or 0.0)
        p1 = float(later_pool.get("pool_price") or 0.0)
        if p0 > 0 and p1 > 0:
            ratio = p1 / p0
        else:
            ratio = 1.0
        if dex == "meteora" and step > 0:
            factor = (1.0 + step / 10000.0) ** half_width
        else:
            factor = 1.0001 ** half_width
        in_range = (1.0 / factor) <= ratio <= factor

        path.append({
            "scan": os.path.basename(f["path"]),
            "value": round(pre_rebalance_value, 2),
            "fees_step": round(fees, 4),
            "il": round(il, 4),
            "in_range": in_range,
        })

        # If out of range and not the last scan, rebalance: close + reopen.
        if in_range is False and i < len(future) - 1:
            exit_cost = pre_rebalance_value * exit_cost_bps / 10000.0 + claim_cost_usd
            total_exit_costs += exit_cost
            remaining = pre_rebalance_value - exit_cost
            # Re-enter at current price with a fresh 50/50 position.
            entry_cost2 = remaining * entry_cost_bps / 10000.0
            current_value = remaining - entry_cost2
            entry_price_ratio = 1.0
            entry_pool = later_pool
            entry_pool["_path"] = f["path"]
            entry_dt = later_dt
            rebalances += 1
        else:
            current_value = pre_rebalance_value
            entry_price_ratio = price_ratio

    # Final close cost.
    final_exit_cost = current_value * exit_cost_bps / 10000.0 + claim_cost_usd
    total_exit_costs += final_exit_cost
    final_value = current_value - final_exit_cost
    pnl = final_value - position_value_usd

    return {
        "pnl_usd": round(pnl, 4),
        "final_value_usd": round(final_value, 4),
        "total_fees_usd": round(total_fees, 4),
        "total_exit_costs_usd": round(total_exit_costs, 4),
        "rebalances": rebalances,
        "path": path,
    }