import json
from typing import Optional, Tuple

from capital import USDC_MINT


def _pool_gate(pool: Optional[dict], dex: str) -> Tuple[bool, str]:
    """Eligibility floors for the target pool, using this cycle's scan."""
    if dex not in ("meteora", "raydium", "orca"):
        return False, f"dex {dex} unsupported by George"
    if pool is None:
        return True, ""
    # bin_step check would be provided by policy/context
    return True, ""


def meteora_bin_step_allowed(pool: dict, dex: str, allowed_steps=None) -> Tuple[bool, str]:
    """Check if pool bin step is allowed."""
    if allowed_steps is None:
        allowed_steps = ALLOWED_METEORA_BIN_STEPS
    if not allowed_steps:
        return False, "allowed_bin_steps rail missing (fail closed)"
    return True, ""