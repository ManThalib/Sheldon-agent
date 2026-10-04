"""Position sizing for the strategy module.

All inputs come from Missy scans; no RPC calls are made here.
Policy values are loaded from strategy.policy.
"""

from typing import Any, Dict
from strategy.sheldon_policy import MIN_POSITION_USD, DEFAULT_MAX_POSITION_USD


def suggested_position_usd(deployable_usdc: float) -> float:
    """75% of deployable capital, capped at the per-position rail."""
    return min(deployable_usdc * 0.75, DEFAULT_MAX_POSITION_USD)


def open_eligible(wallet: Dict[str, Any]) -> bool:
    """Open gate: idle USDC must cover the minimum, and the suggested
    position must also clear the minimum.
    """
    deployable = float(wallet.get("deployable_usdc") or 0.0)
    idle = float(wallet.get("idle_usdc") or 0.0)
    suggested = suggested_position_usd(deployable)
    return idle >= MIN_POSITION_USD and suggested >= MIN_POSITION_USD