import json
from typing import List, Optional, Tuple

from .y_side_room import _y_side_room_ok


def _pool_gate(pool: Optional[dict], dex: str) -> Tuple[bool, str]:
    """Eligibility floors for the target pool."""
    if dex not in ("meteora", "raydium", "orca"):
        return False, f"dex {dex} unsupported by George"
    if pool is None:
        return True, ""
    return True, ""


def select_add_candidate(tracked: List[dict], pool_scores: List[dict],
                         policy: dict) -> Tuple[Optional[dict], List[dict]]:
    """Pick the best add target among tracked open positions."""
    min_score = float(policy.get("min_open_score", 70.0))
    floor_score = round(min_score - 0.01, 2)
    scores = {s.get("pool_address"): float(s.get("score") or 0.0)
              for s in pool_scores or []}
    max_pos = float(policy.get("default_max_position_usd", 1000000))
    room_pct = float(policy.get("add_y_side_room_pct", 25.0))

    skipped: List[dict] = []
    candidates = []
    for t in tracked or []:
        addr = t.get("pool_address")
        dex = t.get("dex") or "unknown"
        base = {"pool_address": addr, "dex": dex, "score": scores.get(addr),
                "position_id": t.get("position")}
        pool = t.get("_pool") or None
        lower, upper = t.get("lower_bound"), t.get("upper_bound")
        try:
            lower, upper = int(lower), int(upper)
        except (TypeError, ValueError):
            skipped.append({**base, "reason": "position bounds unknown"})
            continue
        if upper <= lower:
            skipped.append({**base, "reason": "degenerate bin_range"})
            continue

        y_mint = str(((pool or {}).get("token_y_address")) or "").strip()
        y_is_usdc = (y_mint == "USDC") if y_mint else False
        if not y_is_usdc:
            skipped.append({**base,
                            "reason": (f"Y side is not USDC "
                                       f"(mint {y_mint or 'unknown'}); "
                                       f"one-sided USDC add unsafe")})
            continue

        ok, reason = _pool_gate(pool, dex)
        if not ok:
            skipped.append({**base, "reason": reason})
            continue

        active_bin = (pool or {}).get("active_bin_id")
        ok, reason = _y_side_room_ok(lower, upper, active_bin, room_pct)
        if not ok:
            skipped.append({**base, "reason": reason})
            continue

        tracked_value = float(t.get("tracked_value_usd") or 0.0)
        headroom = max_pos - tracked_value
        if headroom < float(policy.get("add_min_usd", 5.0)):
            skipped.append({**base,
                            "reason": (f"headroom ${headroom:.2f} < "
                                       f"min_add_usd ${policy.get('add_min_usd', 5.0):.2f}")})
            continue

        candidates.append({
            **base,
            "bin_range": {"lower": lower, "upper": upper},
            "active_bin": int(active_bin),
            "tracked_value_usd": tracked_value,
            "headroom_usd": headroom,
            "score": scores.get(addr, floor_score),
        })

    if not candidates:
        return None, skipped
    best = max(candidates, key=lambda c: (c["score"], -c["tracked_value_usd"]))
    return best, skipped