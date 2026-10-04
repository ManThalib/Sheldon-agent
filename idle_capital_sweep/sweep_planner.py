import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from .state_manager import load_add_state, save_add_state
from .scan_committed import scan_committed_usdc
from .reservation_manager import release_confirmed_reservation
from .candidate_selector import select_add_candidate


def plan_sweep(report: dict, signals_dir: str, wallet_mtime: float,
               state: dict, state_dir: str = None,
               now: Optional[float] = None) -> Tuple[Optional[dict], dict]:
    """Decide whether this cycle should emit an idle-capital sweep."""
    now = now if now is not None else time.time()
    policy = {"add_enabled": True, "add_idle_max_usd": 20.0,
              "add_min_usd": 5.0, "add_cooldown_hours": 6.0,
              "add_max_per_day": 6, "add_y_side_room_pct": 25.0,
              "default_max_position_usd": 1000000,
              "add_min_usd": 5.0, "default_max_slippage_bps": 100,
              "add_enabled": True}
    info: Dict[str, Any] = {"phase": "idle_sweep", "decision": "skip", "reason": ""}

    if not policy.get("add_enabled"):
        info["reason"] = "add_policy.enabled is false"
        return None, info

    released = release_confirmed_reservation(state, signals_dir)
    if released != state:
        if not save_add_state(released, state_dir):
            info["reason"] = "could not persist reservation release; sweep skipped"
            return None, info
    state = released
    res = state.get("pending_reservation")
    reserved_usd = float(res.get("amount_usd") or 0.0) if res else 0.0
    info["reserved_usd"] = reserved_usd

    max_age = float(policy.get("add_max_wallet_scan_age_seconds", 900.0))
    scan_age = (now - wallet_mtime) if wallet_mtime else None
    if scan_age is None or scan_age > max_age:
        info["reason"] = (f"wallet scan stale (age "
                          f"{'unknown' if scan_age is None else f'{scan_age:.0f}s'} "
 f">{max_age:.0f}s)")
        return None, info

    cooldown_h = float(policy.get("add_cooldown_hours", 6.0))
    max_per_day = int(policy.get("add_max_per_day", 6))
    last = state.get("last_add") or {}
    last_epoch = float(last.get("reserved_at") or 0)
    if last_epoch and now - last_epoch < cooldown_h * 3600:
        info["reason"] = (f"cooldown: last add {last.get('signal_id')} "
                          f"{(now - last_epoch) / 3600:.1f}h ago < {cooldown_h}h")
        return None, info
    day_key = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    day_counts = state.get("day_counts") or {}
    adds_today = int(day_counts.get(day_key) or 0)
    if adds_today >= max_per_day:
        info["reason"] = f"day cap: {adds_today} adds already on {day_key}"
        return None, info

    wallet = report.get("wallet") or {}
    idle_raw = float(wallet.get("idle_usdc") or 0.0)
    committed, committed_n = scan_committed_usdc(signals_dir, now=now)
    idle_usd = idle_raw - committed - reserved_usd
    info.update({
        "wallet_idle_usdc": round(idle_raw, 2),
        "queued_committed_usdc": round(committed, 2),
        "queued_committed_signals": committed_n,
        "reserved_usd": round(reserved_usd, 2),
        "idle_net_usd": round(idle_usd, 2),
    })

    idle_max = float(policy.get("add_idle_max_usd", 20.0))
    min_add = float(policy.get("add_min_usd", 5.0))
    if idle_usd >= idle_max:
        info["reason"] = (f"idle ${idle_usd:.2f} >= idle_max_usd ${idle_max:.2f}; "
                          f"normal open path owns the capital")
        return None, info
    if idle_usd < min_add:
        info["reason"] = (f"idle ${idle_usd:.2f} < min_add_usd ${min_add:.2f}; "
                          f"not worth a tx")
        return None, info

    tracked = [v for v in report.get("verdicts") or []
               if v.get("action") in ("HOLD", "REBALANCE")]
    best, skipped = select_add_candidate(tracked, report.get("pool_scores") or [],
                                         policy)
    info["skipped"] = skipped
    if best is None:
        info["reason"] = "no qualifying add target (see skipped)"
        return None, info

    add_amount = round(min(idle_usd, float(best["headroom_usd"])), 2)
    if add_amount < min_add:
        info["reason"] = (f"add ${add_amount:.2f} shrank below min_add_usd "
                          f"after headroom cap")
        return None, info

    amount_y_raw = str(int(round(add_amount * (10**6))))
    base = int(now)
    signal = {
        "signal_id": f"sheldon-sweep-{base}",
        "action": "add_liquidity",
        "dex": best["dex"],
        "pool_address": best["pool_address"],
        "position_id": best["position_id"],
        "side": "quote_only",
        "bin_range": best["bin_range"],
        "liquidity": {"amount_x": "0", "amount_y": amount_y_raw},
        "position_usd": add_amount,
        "score": best["score"],
        "max_slippage_bps": int(policy.get("default_max_slippage_bps", 100)),
        "reason": (
            f"IDLE SWEEP ${add_amount:.2f} into tracked position "
            f"(score={best['score']}, tracked=${best['tracked_value_usd']:.2f}, "
            f"headroom ${headroom:.2f})"
        )
    }
    return best, skipped