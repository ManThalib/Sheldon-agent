import json
from typing import Optional, Tuple

from capital import USDC_MINT
from strategy.bin_step_rail import (
    DEFAULT_BIN_STEP_RULE,
    allowed_bin_step_set,
    meteora_bin_step_allowed as _rail_allowed,
)

_DEFAULT_RULE = dict(DEFAULT_BIN_STEP_RULE)


def _pool_gate(pool: Optional[dict], dex: str) -> Tuple[bool, str]:
    """Eligibility floors for the target pool, using this cycle's scan."""
    if dex not in ("meteora", "raydium", "orca"):
        return False, f"dex {dex} unsupported by George"
    if pool is None:
        return True, ""
    # bin_step is enforced below by meteora_bin_step_allowed(); this helper
    # only decides whether the pool record is present at all.
    return True, ""


def meteora_bin_step_allowed(pool: dict, dex: str,
                             rule=None) -> Tuple[bool, str]:
    """Check a Meteora pool's bin_step against the owner rule.

    Non-Meteora pools carry bin_step 0 as non-applicable metadata and are
    not judged here. Meteora pools are judged by the shared predicate: a
    missing/0/malformed step fails closed.

    This function previously returned ``(True, "")`` without comparing
    anything, so an out-of-range step was admitted by the idle-capital path
    even when every other gate rejected it.
    """
    if (dex or "").lower() != "meteora":
        return True, ""
    if rule is None:
        rule = _DEFAULT_RULE
    if not allowed_bin_step_set(rule):
        return False, "bin_step rail missing (fail closed)"
    return _rail_allowed((pool or {}).get("bin_step"), rule)