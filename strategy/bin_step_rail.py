"""Meteora bin_step rail — the single implementation.

Owner ruling 2026-10-19: any Meteora bin_step >= 4 is allowed. The step is an
integer; 1, 2 and 3 are rejected (tight bins are fee-farm bait with hostile
ranges), and missing / 0 / malformed values stay fail-closed. Meteora's
protocol maximum is 400, so anything above it is protocol-invalid and is
rejected rather than silently accepted.

This module exists because the same decision was previously encoded in four
different places with three different answers: two hardcoded fallbacks
excluded 4, one included it, and the idle-capital sweep compared nothing at
all (a probe with bin_step=999 returned allowed). Every caller must ask this
module instead of carrying its own copy.
"""

from typing import Any, Dict, Optional, Tuple

# Owner floor, not configurable: 4 inclusive.
METEORA_MIN_BIN_STEP = 4
# Protocol maximum (Meteora DLMM bin_step upper bound).
METEORA_MAX_BIN_STEP = 400

DEFAULT_BIN_STEP_RULE: Dict[str, int] = {
    "min": METEORA_MIN_BIN_STEP,
    "max": METEORA_MAX_BIN_STEP,
}


def normalize_rule(raw: Optional[Any]) -> Dict[str, int]:
    """Coerce a configured rail into ``{"min", "max"}``, bounded by protocol.

    Accepts the current ``{"min": 4, "max": 400}`` shape and, for backwards
    compatibility only, a legacy whitelist list whose envelope is used. The
    owner floor of 4 and the protocol ceiling of 400 are never widened by
    configuration: a config cannot re-admit bin_step 1-3 or admit 401+.
    """
    lo, hi = METEORA_MIN_BIN_STEP, METEORA_MAX_BIN_STEP
    if isinstance(raw, dict):
        try:
            lo = int(raw.get("min", lo))
            hi = int(raw.get("max", hi))
        except (TypeError, ValueError):
            return dict(DEFAULT_BIN_STEP_RULE)
    elif isinstance(raw, (list, tuple)) and raw:
        try:
            values = [int(v) for v in raw]
        except (TypeError, ValueError):
            return dict(DEFAULT_BIN_STEP_RULE)
        lo, hi = min(values), max(values)
    else:
        return dict(DEFAULT_BIN_STEP_RULE)
    return {"min": max(lo, METEORA_MIN_BIN_STEP),
            "max": min(hi, METEORA_MAX_BIN_STEP)}


def allowed_bin_step_set(raw: Optional[Any] = None) -> frozenset:
    """The allowed steps as a set, derived from the same rule.

    Kept for callers that historically tested membership. It is derived, not
    configured, so it cannot disagree with :func:`meteora_bin_step_allowed`.
    """
    rule = normalize_rule(raw)
    return frozenset(range(rule["min"], rule["max"] + 1))


def meteora_bin_step_allowed(value: Any,
                             rule: Optional[Any] = None,
                             ) -> Tuple[bool, str]:
    """Return ``(allowed, reason)`` for one Meteora bin_step value.

    Fail-closed on anything that is not a plain integer in range.
    """
    if value is None or value == "" or value is False:
        return False, "bin_step unknown (fail-closed)"
    if isinstance(value, bool):
        return False, f"bin_step malformed ({value!r})"
    try:
        step = int(value)
    except (TypeError, ValueError):
        return False, f"bin_step malformed ({value!r})"
    if step == 0:
        return False, "bin_step unknown (fail-closed)"
    if step > METEORA_MAX_BIN_STEP:
        return False, (f"bin_step {step} exceeds Meteora protocol maximum "
                       f"{METEORA_MAX_BIN_STEP}")
    resolved = normalize_rule(rule)
    if step < resolved["min"]:
        return False, (f"bin_step {step} below owner minimum "
                       f"{resolved['min']}")
    if step > resolved["max"]:
        return False, (f"bin_step {step} above configured maximum "
                       f"{resolved['max']}")
    return True, ""
