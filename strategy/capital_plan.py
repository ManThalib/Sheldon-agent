"""Human-readable capital plan for the report.

Loads wallet state from Missy scans and policy values from strategy.policy.
"""

from typing import Any, Dict
from strategy.sheldon_policy import MIN_POSITION_USD, DEFAULT_MAX_POSITION_USD, MAX_POSITION_OPEN_PER_CYCLE
from strategy.position_sizing import suggested_position_usd, open_eligible


def capital_plan(wallet: Dict[str, Any]) -> Dict[str, Any]:
    """Human-readable capital plan section for the report."""
    idle = float(wallet.get("idle_usdc") or 0.0)
    dust = float(wallet.get("dust_total_usdc") or 0.0)
    deployable = float(wallet.get("deployable_usdc") or 0.0)
    size = suggested_position_usd(deployable)
    return {
        "idle_usdc": idle,
        "dust_total_usdc": dust,
        "deployable_usdc": deployable,
        "suggested_position_usdc": size,
        "open_eligible": open_eligible(wallet),
        "min_position_usd": MIN_POSITION_USD,
        "max_position_usd": DEFAULT_MAX_POSITION_USD,
        "max_opens_per_cycle": MAX_POSITION_OPEN_PER_CYCLE,
    }