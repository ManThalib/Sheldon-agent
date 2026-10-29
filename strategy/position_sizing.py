"""Position sizing for the strategy module.

All inputs come from Missy scans; no RPC calls are made here.
Policy values are loaded from strategy.policy.
"""

from typing import Any, Dict
from strategy.sheldon_policy import (MIN_POSITION_USD, DEFAULT_MAX_POSITION_USD,
                                     SOL_RESERVE_LAMPORTS, SWAPPABLE_SOL_HAIRCUT)


def suggested_position_usd(deployable_usdc: float) -> float:
    """75% of deployable capital, capped at the per-position rail."""
    return min(deployable_usdc * 0.75, DEFAULT_MAX_POSITION_USD)


def swappable_sol_usd(wallet: Dict[str, Any],
                      sol_reserve_lamports: int = SOL_RESERVE_LAMPORTS,
                      haircut: float = SWAPPABLE_SOL_HAIRCUT) -> float:
    """Idle SOL usable as deployable capital, net of the reserve and haircut.

    Owner ruling 2026-10-25: a wallet that holds mostly SOL (after SOL-heavy
    closes or capital-prep buys) must not read as dead to the planner. SOL is
    not unswappable dust: it is the pipeline's X-side asset and plan_funding
    sells it when an open needs the stable side. Reserve and haircut keep it
    conservative; the result is 0.0 whenever the balances are unknown.
    """
    raw = float(wallet.get("idle_sol_raw_lamports") or 0.0)
    usd = float(wallet.get("idle_sol_usd") or 0.0)
    if raw <= 0.0 or usd <= 0.0:
        return 0.0
    spendable_raw = max(0.0, raw - float(sol_reserve_lamports))
    return (usd * spendable_raw / raw) * haircut


def deployable_capital_usd(wallet: Dict[str, Any]) -> float:
    """Deployable capital: stablecoin plus swappable idle SOL.

    Dust / liquidation_required assets remain excluded (pinned by
    tests/test_capital_stable_only.py)."""
    idle = float(wallet.get("idle_stable_usd")
                 if wallet.get("idle_stable_usd") is not None
                 else wallet.get("idle_usdc") or 0.0)
    return idle + swappable_sol_usd(wallet)


def open_eligible(wallet: Dict[str, Any]) -> bool:
    """Open gate: deployable capital must cover the minimum, and the
    suggested position must also clear the minimum.

    Stablecoin covers the Y side directly; capital sizing additionally counts
    swappable idle SOL above the reserve (owner ruling 2026-10-25) because
    ``plan_funding`` sells it when an open needs the stable side — so a
    SOL-heavy wallet is no longer read as dead by the planner. Volatile
    *dust* (``dust_assets`` / ``liquidation_required_usd``) stays excluded:
    ``deployable_capital_usd`` never counts it, so WBTC cannot fund an open.
    ``idle_stable_usd`` falls back to ``idle_usdc`` so hand-built wallet
    dicts keep working.
    """
    deployable = deployable_capital_usd(wallet)
    suggested = suggested_position_usd(deployable)
    return deployable >= MIN_POSITION_USD and suggested >= MIN_POSITION_USD