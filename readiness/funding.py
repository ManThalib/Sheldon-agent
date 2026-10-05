#!/usr/bin/env python3
"""Funding plan builder for Sheldon readiness gates.

Scores-order funding walk across strategies, returns per-strategy funded/unfunded
state, prep swap specs for the highest-scored unfunded strategy, and surplus sell
specs when everything selected is funded. Fails safe: no prep swaps without
balance data.

Mirrors the `plan_funding` function from the original readiness.py.
"""

import math
from typing import Any, Dict, List, Optional, Set

from capital import RESERVED_MINTS, SOL_MINT, USDC_MINT


# ---------------------------------------------------------------------------
# Hard-coded constants (mirror readiness.py top-level)
# ---------------------------------------------------------------------------
SOL_RESERVE_LAMPORTS = 20_000_000  # 0.02 SOL
USDC_DECIMALS = 6
BUY_BUFFER_PCT = 2.0
MIN_PREP_SWAP_USD = 1.0
SELL_SURPLUS_MIN_USD = 2.0


def _created_epoch(created_at: str) -> float:
    """Parse ISO-8601 timestamp to Unix epoch."""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError):
        return 0.0


def spendable(mint: str, asset: Dict[str, Any]) -> int:
    """Return spendable raw amount for a mint, net of SOL reserve."""
    if mint == SOL_MINT:
        return max(0, asset.get("amount_raw", 0) - SOL_RESERVE_LAMPORTS)
    return asset.get("amount_raw", 0)


def plan_funding(
    strategies: List[Dict[str, Any]],
    raw_assets: List[Dict[str, Any]],
    wallet_path: Optional[str] = None,
    wallet_mtime: float = 0.0,
    dust_mints: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Score-order funding walk across strategies.

    Returns per-strategy funded/unfunded state, prep swap specs for the
    highest-scored unfunded strategy, and surplus sell specs when everything
    selected is funded. Fails safe: no prep swaps without balance data.
    """
    dust_mints = dust_mints or set()

    # Build asset index by mint.
    assets: Dict[str, Dict[str, Any]] = {}
    for a in raw_assets or []:
        mint = (a.get("mint") or "").strip()
        if not mint:
            continue
        try:
            raw = int(a.get("amount_raw") or 0)
        except (TypeError, ValueError):
            raw = 0
        assets[mint] = {
            "amount_raw": max(0, raw),
            "amount_ui": float(a.get("amount_ui") or 0.0),
            "decimals": int(a.get("decimals") or 0),
            "price_usd": float(a.get("price_usd") or 0.0),
            "symbol": a.get("symbol") or "",
        }

    # Spendable per-mint balances.
    remaining: Dict[str, int] = {
        mint: spendable(mint, asset)
        for mint, asset in assets.items()
    }
    remaining.setdefault(USDC_MINT, 0)
    remaining.setdefault(SOL_MINT, 0)

    # Sort strategies by score descending.
    ordered = sorted(strategies, key=lambda s: (s.get("score") or 0.0), reverse=True)
    funded: List[Dict[str, Any]] = []
    unfunded: List[Dict[str, Any]] = []

    for s in ordered:
        entry = {
            "pool_address": s.get("pool_address"),
            "score": s.get("score"),
            "dex": s.get("dex"),
        }
        req = _required_amounts(s)
        if req is None:
            entry.update({"funded": False,
                          "reason": "missing pool enrichment (price/decimals/mint)"})
            unfunded.append(entry)
            continue

        entry["required"] = {"x_mint": req["x_mint"], "x_raw": req["x_raw"],
                             "y_mint": req["y_mint"], "y_raw": req["y_raw"]}
        entry["req"] = req
        have_x = remaining.get(req["x_mint"], 0)
        have_y = remaining.get(req["y_mint"], 0)
        entry["have"] = {"x_raw": have_x, "y_raw": have_y}

        if have_x >= req["x_raw"] and have_y >= req["y_raw"]:
            entry["funded"] = True
            remaining[req["x_mint"]] = have_x - req["x_raw"]
            remaining[req["y_mint"]] = have_y - req["y_raw"]
            funded.append(entry)
        else:
            entry["funded"] = False
            entry["reason"] = "insufficient token balance for open"
            unfunded.append(entry)

    # Build prep swap specs.
    prep_specs: List[Dict[str, Any]] = []
    notes: List[str] = []

    if unfunded:
        # Prep only the highest-scored unfunded strategy per cycle.
        target = next((e for e in unfunded if e.get("req")), None)
        if target is None:
            notes.append("no fundable unfunded strategy (missing enrichment); no prep swap")
        else:
            target["prep_target"] = True
            req = target["req"]

            # USDC kept aside for this target's Y side if Y is USDC.
            budget = remaining.get(USDC_MINT, 0)
            if req["y_mint"] == USDC_MINT:
                budget -= req["y_raw"]

            for side in ("x", "y"):
                mint = req[f"{side}_mint"]
                need = req[f"{side}_raw"]
                have = remaining.get(mint, 0)
                if mint == USDC_MINT:
                    if have < need:
                        notes.append(
                            f"pool {target['pool_address']}: insufficient USDC "
                            f"({have / 10**USDC_DECIMALS:.2f} < {need / 10**USDC_DECIMALS:.2f}); no prep possible"
                        )
                    continue
                delta = need - have
                if delta <= 0:
                    continue
                dec = req[f"{side}_dec"]
                px = req[f"{side}_px"]
                if dec <= 0 or px <= 0:
                    notes.append(f"pool {target['pool_address']}: side {side} price/decimals unknown; no prep swap")
                    continue
                delta_ui = delta / (10 ** dec)
                cost_raw = int(math.ceil(delta_ui * px * (1.0 + BUY_BUFFER_PCT / 100.0) * (10 ** USDC_DECIMALS)))
                if cost_raw / (10 ** USDC_DECIMALS) < MIN_PREP_SWAP_USD:
                    continue
                if cost_raw > budget:
                    notes.append(
                        f"pool {target['pool_address']}: buy of {delta_ui:.6f} needs "
                        f"{cost_raw / 10**USDC_DECIMALS:.2f} USDC, only "
                        f"{budget / 10**USDC_DECIMALS:.2f} available; no prep swap"
                    )
                    continue
                a = assets.get(mint) or {}
                prep_specs.append({
                    "direction": "buy",
                    "for_pool": target.get("pool_address"),
                    "input_mint": USDC_MINT,
                    "output_mint": mint,
                    "amount_raw": str(cost_raw),
                    "usd": cost_raw / (10 ** USDC_DECIMALS),
                    "need_ui": need / (10 ** dec),
                    "have_ui": have / (10 ** dec),
                    "symbol": a.get("symbol") or mint[:6],
                })
                budget -= cost_raw
    elif not unfunded:
        # Full 50/50 rebalance: sell leftover surplus back to USDC.
        # Fires whenever no shortfall is pending, including when nothing is
        # open-eligible: idle surplus above the reserve is dead capital.
        # Mints already queued as dust are skipped: the dust path routes
        # them to owner review (swap_to_usdc); selling them here too would
        # double-handle the same balance.
        dust_mints = dust_mints or set()
        for mint, left in sorted(remaining.items()):
            # SOL is in RESERVED_MINTS (never dust), but its surplus above
            # the reserve is rebalanceable: spendable() already netted the
            # reserve out of `left`. Other reserved mints are owner holds.
            if mint == USDC_MINT or (mint in RESERVED_MINTS and mint != SOL_MINT) or mint in dust_mints:
                continue
            a = assets.get(mint)
            if not a or a["decimals"] <= 0 or a["price_usd"] <= 0 or left <= 0:
                continue
            ui = left / (10 ** a["decimals"])
            if ui * a["price_usd"] < SELL_SURPLUS_MIN_USD:
                continue
            prep_specs.append({
                "direction": "sell",
                "for_pool": None,
                "input_mint": mint,
                "output_mint": USDC_MINT,
                "amount_raw": str(left),
                "usd": ui * a["price_usd"],
                "need_ui": 0.0,
                "have_ui": ui,
                "symbol": a.get("symbol") or mint[:6],
            })

    return {
        "funded": funded,
        "unfunded": unfunded,
        "prep_swaps": prep_specs,
        "notes": notes,
        "wallet_path": wallet_path,
        "wallet_mtime": wallet_mtime,
        "sol_reserve_lamports": SOL_RESERVE_LAMPORTS,
    }

# ---------------------------------------------------------------------------
# Required amounts computation (mirrors readiness.py.required_amounts)
# ---------------------------------------------------------------------------
def _required_amounts(strategy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Raw token amounts an open strategy will request, 50/50 USD split."""
    pool = strategy.get("_pool") or {}
    px_x = float(pool.get("token_x_price_usd") or 0.0)
    px_y = float(pool.get("token_y_price_usd") or 0.0)
    dec_x = int(pool.get("token_x_decimals") or 0)
    dec_y = int(pool.get("token_y_decimals") or 0)
    x_mint = (pool.get("token_x_address") or "").strip()
    y_mint = (pool.get("token_y_address") or "").strip()
    position_usd = float(strategy.get("suggested_usdc") or 0.0)
    if position_usd <= 0 or px_x <= 0 or px_y <= 0 or dec_x <= 0 or dec_y <= 0:
        return None
    if not x_mint or not y_mint:
        return None

    half_usd = position_usd / 2.0
    x_raw = int((half_usd / px_x) * (10 ** dec_x))
    y_raw = int((half_usd / px_y) * (10 ** dec_y))
    if x_raw <= 0 or y_raw <= 0:
        return None
    return {
        "x_mint": x_mint,
        "y_mint": y_mint,
        "x_raw": x_raw,
        "y_raw": y_raw,
        "x_dec": dec_x,
        "y_dec": dec_y,
        "x_px": px_x,
        "y_px": px_y,
        "half_usd": half_usd,
    }