from typing import Tuple

def _y_side_room_ok(lower: int, upper: int, active_bin: int,
                    room_pct: float) -> Tuple[bool, str]:
    """True when >= room_pct of the range sits above the active bin."""
    if active_bin is None or int(active_bin) == 0:
        return False, f"active bin unknown ({active_bin})"
    active_bin = int(active_bin)
    if not (lower <= active_bin < upper):
        return False, f"active bin {active_bin} outside range [{lower},{upper})"
    total = upper - lower
    room = upper - active_bin
    share = 100.0 * room / total
    if share + 1e-9 < room_pct:
        return False, f"y-side room {share:.1f}% < policy {room_pct}%"
    return True, ""