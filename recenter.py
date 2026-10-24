#!/usr/bin/env python3
"""Active-bin drift recenter trigger.

Policy: an in-range position fires a recenter — close + reopen in the same
pool, the new range centered on the current active bin — when ALL hold:

  - drift: |active_bin - range_center| >= max(min_drift_bins,
    drift_frac_of_half_width * half_width);
  - age: the position is older than min_hold_hours (unknown age fails safe);
  - cooldown: the pool's last emitted recenter is older than cooldown_hours;
  - trend: the recent active-bin history moves in the drift direction
    (recenter into a trend, not into chop);
  - daily cap: fewer than max_recenters_per_pool_per_day recenters emitted
    for this pool today;
  - data: position value and token composition are known (the reopen leg
    needs position_usd and liquidity amounts — fail closed on unknowns).

Fail-closed throughout: unknown geometry, out-of-range positions (the OOR
close rail owns those), unknown value, and unknown amounts never fire.

Cooldowns and daily counters record only when a recenter signal is
ACTUALLY written (record_emitted from run_cycle after write_signals), so a
suppressed emission never burns the pool's cooldown.
"""

import json
import os
import time
from typing import Any, Dict, Optional

# Malformed-bound guard, mirroring range_state.py: a wider decoded range is
# invalid geometry, never a recenter candidate.
MAX_RANGE_BINS = 200

STATE_FILENAME = "recenter_state.json"
MAX_SAMPLES = 48

DEFAULTS = {
    "drift_frac_of_half_width": 0.6,
    "min_drift_bins": 8,
    "cooldown_hours": 6.0,
    "min_hold_hours": 12.0,
    "trend_confirm_scans": 3,
    "trend_min_span_seconds": 1800,
    "sample_gap_seconds": 600,
    "max_recenters_per_pool_per_day": 2,
    "amount_haircut_pct": 2.0,
    "min_position_usd": 20.0,
}


def state_path(state_dir: str = None) -> str:
    if state_dir is None:
        state_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    return os.path.join(state_dir, STATE_FILENAME)


def load_state(state_dir: str = None) -> dict:
    try:
        with open(state_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    pools = data.get("pools")
    return {"version": 1, "pools": pools} if isinstance(pools, dict) else {"version": 1, "pools": {}}


def save_state(state: dict, state_dir: str = None) -> None:
    try:
        path = state_path(state_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {"version": 1, "pools": state.get("pools") or {}}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def _pool_state(state: dict, addr: str) -> dict:
    """Ensure state["pools"][addr] exists and return it.
    Creates the nested dicts on first use; modifies *state* in place.
    """
    pools = state.setdefault("pools", {})
    return pools.setdefault(addr, {
        "samples": [],
        "last_recenter_ts": None,
        "last_recenter_position": None,
        "recenters_day": None,
        "recenters_today": 0,
    })


def update_history(positions: list, state: dict, now: float,
                   sample_gap_seconds: float = None) -> dict:
    """Append one active-bin/price sample per pool from verified raw records.

    Samples dedupe within sample_gap_seconds so burst scans (multiple scans
    minutes apart) do not fabricate a trend. Only records with known integer
    geometry (bounds + active bin) append; unknown rows never pollute the
    history. Keeps the newest MAX_SAMPLES entries per pool.
    """
    gap = float(sample_gap_seconds if sample_gap_seconds is not None
                else DEFAULTS["sample_gap_seconds"])
    for pos in positions or []:
        addr = pos.get("pool_address")
        active = _valid_int(pos.get("current_bin_id"))
        lower = _valid_int(pos.get("lower_bound"))
        upper = _valid_int(pos.get("upper_bound"))
        if not addr or active is None or lower is None or upper is None:
            continue
        if upper <= lower or (upper - lower) > MAX_RANGE_BINS:
            continue
        rec = _pool_state(state, str(addr))
        samples = rec["samples"]
        if samples and (now - float(samples[-1].get("t", 0))) < gap:
            continue
        samples.append({
            "t": float(now),
            "active_bin": int(active),
            "price": _valid_number(pos.get("current_price")),
        })
        del samples[:-MAX_SAMPLES]
    return state


def _valid_number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value != 0 else None


def _valid_int(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _trend_sign(samples: list, policy: dict) -> Optional[int]:
    """+1/-1 when the active bin moved over the confirmation window, else None.

    Requires trend_confirm_scans samples spanning at least
    trend_min_span_seconds; identical endpoints read as no trend (None).
    """
    n = int(policy["trend_confirm_scans"])
    if len(samples) < n:
        return None
    newest = samples[-1]
    oldest = samples[-n]
    span = float(newest.get("t", 0)) - float(oldest.get("t", 0))
    if span < float(policy["trend_min_span_seconds"]):
        return None
    delta = int(newest.get("active_bin", 0)) - int(oldest.get("active_bin", 0))
    if delta > 0:
        return 1
    if delta < 0:
        return -1
    return None


def _new_range(active_bin: int, pool: dict) -> Optional[dict]:
    """Volatility-adaptive range centered on the active bin.

    Reuses the exact open-flow helpers (_7d_volatility_based) so a recenter
    re-ranges with the same width policy a fresh open would get. Returns
    None when the pool record cannot support a width (missing bin_step on
    Meteora fails closed inside adaptive_half_width to MIN_HALF_WIDTH —
    still a valid range, so None is only for absurd center inputs).
    """
    from strategy.range._7d_volatility_based import adaptive_half_width
    half = int(adaptive_half_width(pool or {}))
    if half <= 0:
        return None
    return {"lower": int(active_bin) - half, "upper": int(active_bin) + half}


def _composition_amounts(pos: dict, policy: dict) -> Optional[Dict[str, str]]:
    """Reopen liquidity from the position's current composition, haircut.

    The close leg returns the position's tokens; re-depositing the same
    composition (minus a small haircut for fees/rounding/scan lag) is the
    best known estimate. Raw amounts are strings; a missing or all-zero
    composition cannot fund a reopen — None (fail closed).
    """
    haircut = 1.0 - float(policy["amount_haircut_pct"]) / 100.0

    def _raw(side):
        amounts = pos.get(f"amounts_{side}_raw")
        if isinstance(amounts, (list, tuple)) and len(amounts) == 1:
            amounts = amounts[0]
        if isinstance(amounts, str) and amounts:
            return amounts
        tok = pos.get(f"token_{side}_amount") or {}
        raw = tok.get("raw") if isinstance(tok, dict) else None
        if isinstance(raw, str) and raw:
            return raw
        return None

    ax, ay = _raw("x"), _raw("y")
    if ax is None or ay is None:
        return None
    try:
        ix, iy = int(ax), int(ay)
    except (TypeError, ValueError):
        return None
    if ix < 0 or iy < 0 or (ix <= 0 and iy <= 0):
        return None
    return {"amount_x": str(int(ix * haircut)), "amount_y": str(int(iy * haircut))}


def _position_usd(pos: dict, policy: dict) -> Optional[float]:
    """Known position value in USD, or None (fail closed)."""
    features = pos.get("position_features") or {}
    if features.get("value_known") and _valid_number(features.get("value_usd")):
        value = float(features["value_usd"])
    elif _valid_number(pos.get("current_value_usd")):
        value = float(pos["current_value_usd"])
    else:
        return None
    if value <= float(policy["min_position_usd"]):
        return None
    return value


def evaluate_recenter(pos: dict, pool: dict, pool_state: dict, policy: dict,
                      now: float) -> dict:
    """Evaluate the trigger for one raw position record.

    Returns a dict with fire=True plus the reopen payload (new bin_range,
    liquidity amounts, position_usd, recenter_reason) on fire, or
    fire=False with the blocking reason. Never raises on bad data; every
    unknown reads as no-fire.
    """
    out: Dict[str, Any] = {"fire": False, "reason": None}

    active = _valid_int(pos.get("current_bin_id"))
    lower = _valid_int(pos.get("lower_bound"))
    upper = _valid_int(pos.get("upper_bound"))
    if active is None or lower is None or upper is None:
        out["reason"] = "geometry unknown (bounds/active bin)"
        return out
    if upper <= lower or (upper - lower) > MAX_RANGE_BINS:
        out["reason"] = "range geometry invalid"
        return out

    half_width = (upper - lower) / 2.0
    center = (lower + upper) / 2.0
    drift = active - center
    drift_needed = max(float(policy["min_drift_bins"]),
                       float(policy["drift_frac_of_half_width"]) * half_width)
    out["drift_bins"] = drift
    out["drift_needed"] = drift_needed
    if abs(drift) < drift_needed:
        out["reason"] = (f"drift {drift:+.1f} bins < threshold "
                         f"{drift_needed:.1f}")
        return out
    drift_sign = 1 if drift > 0 else -1

    # In-range only: a fully out-of-range position belongs to the OOR close
    # rail, not to a recenter.
    if active < lower or active > upper:
        out["reason"] = "position out of range; close rail owns it"
        return out

    # Validate age before drift (any failure short-circuits)
    days_open = _valid_number(pos.get("days_open"))
    if days_open is None or days_open * 24.0 < float(policy["min_hold_hours"]):
        out["reason"] = ("position younger than min_hold_hours"
                           if days_open is not None
                           else "days_open unknown (fail closed)")
        return out

    # Cooldown check BEFORE drift (reason reported first takes priority)
    last_ts = pool_state.get("last_recenter_ts")
    if last_ts and (now - float(last_ts)) < float(policy["cooldown_hours"]) * 3600.0:
        out["reason"] = (f"cooldown: last recenter {int(now - float(last_ts))}s ago "
                           f"< {float(policy['cooldown_hours']) * 3600.0:.0f}s")
        return out

    # Drift check (only reached if position passes age validation)
    drift_needed = max(float(policy["min_drift_bins"]),
                       float(policy["drift_frac_of_half_width"]) * half_width)
    if days_open is None or days_open * 24.0 < float(policy["min_hold_hours"]):
        out["reason"] = ("position younger than min_hold_hours"
                         if days_open is not None
                         else "days_open unknown (fail closed)")
        return out

    samples = pool_state.get("samples") or []
    trend = _trend_sign(samples, policy)
    out["trend_sign"] = trend
    if trend is None:
        out["reason"] = "trend unknown (insufficient active-bin history)"
        return out
    if trend != drift_sign:
        out["reason"] = (f"trend {'+' if trend > 0 else '-'} opposes drift "
                         f"{'+' if drift_sign > 0 else '-'}; not recentring into chop")
        return out

    last_ts = pool_state.get("last_recenter_ts")
    if last_ts and (now - float(last_ts)) < float(policy["cooldown_hours"]) * 3600.0:
        out["reason"] = (f"cooldown: last recenter {int(now - float(last_ts))}s ago "
                         f"< {float(policy['cooldown_hours']) * 3600.0:.0f}s")
        return out

    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if (pool_state.get("recenters_day") == today
            and int(pool_state.get("recenters_today") or 0)
            >= int(policy["max_recenters_per_pool_per_day"])):
        out["reason"] = "daily recenter cap reached for pool"
        return out

    position_usd = _position_usd(pos, policy)
    if position_usd is None:
        out["reason"] = "position value unknown or below min_position_usd"
        return out

    liquidity = _composition_amounts(pos, policy)
    if liquidity is None:
        out["reason"] = "token composition unknown (cannot fund reopen leg)"
        return out

    new_range = _new_range(active, pool)
    if new_range is None:
        out["reason"] = "cannot build adaptive range for pool"
        return out
    if (new_range["lower"] == lower and new_range["upper"] == upper):
        out["reason"] = "adaptive range equals current range"
        return out

    out.update({
        "fire": True,
        "drift_bins": drift,
        "trend_sign": trend,
        "bin_range": new_range,
        "liquidity": liquidity,
        "position_usd": position_usd,
        "recenter_reason": (
            f"active bin {active} drifted {drift:+.0f} bins from range center "
            f"({center:.0f}); trend {'up' if trend > 0 else 'down'}; recentring "
            f"to [{new_range['lower']}, {new_range['upper']}]"
        ),
    })
    return out


def apply_recenter_verdicts(verdicts: list, active_positions: dict,
                            pools_by_addr: dict, state: dict, policy: dict,
                            now: float) -> list:
    """Evaluate every keepable position verdict and tag RECENTER on fire.

    Only HOLD / COLLECT_FEES verdicts are eligible: CLOSE/REBALANCE/ROTATE
    already dispose of the position and OPEN_CANDIDATE rows are not
    positions. Mutates verdicts in place, attaching the reopen payload for
    write_signals, and returns review notes for evaluated-but-not-fired
    positions (the report should explain why a drifted position stayed put).
    """
    notes: list = []
    for v in verdicts or []:
        if v.get("action") not in ("HOLD", "COLLECT_FEES"):
            continue
        addr = v.get("pool_address")
        pos = active_positions.get(addr)
        if not pos or (v.get("dex") or "").lower() != "meteora":
            continue
        pool_state = _pool_state(state, str(addr))
        result = evaluate_recenter(pos, pools_by_addr.get(addr) or {},
                                   pool_state, policy, now)
        if result["fire"]:
            v["action"] = "RECENTER"
            v["reason"] = f"RECENTER: {result['recenter_reason']}"
            v["note"] = v["reason"]
            v["recenter_bin_range"] = result["bin_range"]
            v["recenter_liquidity"] = result["liquidity"]
            v["recenter_position_usd"] = result["position_usd"]
            v["recenter_reason"] = result["recenter_reason"]
        else:
            notes.append({
                "pool_address": addr,
                "position": v.get("position"),
                "reason": f"recenter not fired: {result['reason']}",
                "drift_bins": result.get("drift_bins"),
            })
    return notes


def record_emitted(state: dict, emitted_signals: list, now: float) -> dict:
    """Record cooldown/daily counters for recenter signals actually written."""
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    for sig in emitted_signals or []:
        if sig.get("action") != "recenter":
            continue
        addr = str(sig.get("pool_address") or "")
        if not addr:
            continue
        rec = _pool_state(state, addr)
        rec["last_recenter_ts"] = float(now)
        rec["recenters_today"] = int(rec.get("recenters_today") or 0) + 1
        if rec.get("recenters_day") != today:
            rec["recenters_day"] = today
            rec["recenters_today"] = 0
    return state


def merged_policy(policy: dict, min_hold_hours_default: float = None) -> dict:
    """Normalize a policy block, filling defaults (never widening)."""
    merged = dict(DEFAULTS)
    for key in DEFAULTS:
        if isinstance(policy, dict) and policy.get(key) is not None:
            merged[key] = policy[key]
    if min_hold_hours_default is not None and (
            not isinstance(policy, dict) or policy.get("min_hold_hours") is None):
        merged["min_hold_hours"] = float(min_hold_hours_default)
    return merged
