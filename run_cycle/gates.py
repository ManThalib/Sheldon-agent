"""Gates and filtering logic extracted from run_cycle.py.

Contains:
  - _in_trading_window: check if current time is within configured window
  - _filter_open_candidates: drop candidates that must not become signals
  - _range_center: get pool's current tick/bin position
  - _meteora_bin_step_allowed: check bin_step rail (helper)
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Tuple

# Constants
MIN_POSITION_USD = 20.0


def _utc_iso():
    """Return current time in Asia/Shanghai (UTC+8) ISO format."""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _meteora_bin_step_allowed(pool: Dict[str, Any], dex: str = None) -> Tuple[bool, str]:
    """Return (allowed, reason) for a Meteora pool's bin_step rail.

    Fail-closed: an unknown bin_step counts as disallowed. Non-Meteora
    pools are always allowed (bins are a DLMM concept). Callers that know
    the dex should pass it — Missy _pool records may lack a dex field.
    """
    from strategy.sheldon_policy import ALLOWED_METEORA_BIN_STEPS

    dex = (dex or pool.get("dex") or "").lower()
    if dex != "meteora":
        return True, ""
    bin_step = pool.get("bin_step")
    if bin_step in (None, "", 0):
        return False, "bin_step unknown (fail-closed)"
    try:
        bin_step = int(bin_step)
    except (TypeError, ValueError):
        return False, "bin_step malformed"
    allowed = ALLOWED_METEORA_BIN_STEPS
    if bin_step not in allowed:
        return False, f"bin_step {bin_step} not in allowed set {sorted(allowed)} (George rail)"
    return True, ""


def _in_trading_window(windows: dict, action: str) -> bool:
    """Return True if the current Asia/Shanghai time is inside the configured
    trading window and not on a blackout date.

    Mirrors the original logic from run_cycle.py.
    """
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)

    blackout = windows.get("blackout_dates") or []
    today = now.strftime("%Y-%m-%d")
    if today in blackout:
        return False

    window_key = "open_window_utc" if action == "open" else "close_window_utc"
    window = windows.get(window_key, "00:00-23:59")
    try:
        start_str, end_str = window.split("-")
        start_hour, start_min = map(int, start_str.strip().split(":"))
        end_hour, end_min = map(int, end_str.strip().split(":"))
    except (ValueError, AttributeError):
        return True

    current_min = now.hour * 60 + now.minute
    start_min_total = start_hour * 60 + start_min
    end_min_total = end_hour * 60 + end_min
    if end_min_total < start_min_total:
        return current_min >= start_min_total or current_min <= end_min_total
    return start_min_total <= current_min <= end_min_total


def _range_center(pool: dict, dex: str):
    """Return the pool's current position index.

    Meteora uses DLMM bin IDs (``active_bin_id``); Raydium CLMM and Orca
    Whirlpool use ticks. Missy may supply ``current_tick``,
    ``current_tick_index`` or reuse ``active_bin_id`` for the tick value.
    Returns None when the tick/bin is unknown.
    """
    if dex == "meteora":
        val = pool.get("active_bin_id")
        if val is None:
            return None
        return int(val)
    for key in ("current_tick", "current_tick_index", "active_bin_id"):
        val = pool.get(key)
        if val is None or val in ("", "0"):
            continue
        return int(val)
    return None


def _filter_open_candidates(open_candidates: list, active_positions: dict,
                            policy_windows: dict, min_score: float,
                            min_liquidity: float, min_volume: float,
                            allowed_bin_steps: set) -> tuple:
    """Drop open candidates that must not become signals.

    Honors Missy's eligibility flag when present (the default path after
    Phase 2). For older scans without `eligible`, legacy TVL/volume gates
    are applied as a backstop. Sheldon-specific rails (bin steps, open
    score, windows, capital) are always enforced.

    Returns (kept, skipped) tuples.
    """
    kept = []
    skipped = []
    seen = set()

    if not _in_trading_window(policy_windows, "open"):
        skipped.append({
            "pool_address": None,
            "dex": "any",
            "score": None,
            "reason": "outside configured open window or blackout date",
        })
        return kept, skipped

    for v in open_candidates:
        addr = v.get("pool_address")
        dex = v.get("dex") or "unknown"
        base = {"pool_address": addr, "dex": dex, "score": v.get("score")}

        # Dedup: already holding or duplicate pool
        if addr in active_positions:
            skipped.append({**base,
                            "reason": "already holding a position in this pool (dedup)"})
            continue
        if addr in seen:
            skipped.append({**base,
                            "reason": "duplicate pool in scan; first candidate kept"})
            continue

        pool = v.get("_pool") or {}
        center = _range_center(pool, dex)
        if center is None:
            skipped.append({**base,
                            "reason": "current tick/bin unknown; refusing to open blind"})
            continue

        # Phase 2: trust Missy's eligibility gate when present.
        eligible = pool.get("eligible")
        if eligible is False:
            rejected_reason = pool.get("rejected_reason") or "Missy eligibility: false"
            skipped.append({**base, "reason": f"Missy: {rejected_reason}"})
            continue

        # Legacy backstop for older scans that lack Missy eligibility data.
        if eligible is None:
            tvl = float(pool.get("tvl") or 0.0)
            if tvl < min_liquidity:
                skipped.append({**base,
                                "reason": f"pool TVL ${tvl:.0f} < legacy policy ${min_liquidity:.0f}"})
                continue
            volume = float(pool.get("volume_window") or pool.get("volume") or 0.0)
            if volume < min_volume:
                skipped.append({**base,
                                "reason": f"pool volume ${volume:.0f} < legacy policy ${min_volume:.0f}"})
                continue

        # Bin step check: only enforced for Meteora DLMM pools.
        # Raydium CLMM and Orca Whirlpool use tick_spacing, not bin steps.
        if dex == "meteora":
            bin_step = pool.get("bin_step")
            if bin_step in (None, "", 0):
                # Fail-closed: unknown/missing bin_step drops the candidate
                skipped.append({**base,
                                "reason": f"bin_step {bin_step} not in allowed set "
                                f"{sorted(allowed_bin_steps)} (George rail)"})
                continue
            try:
                if int(bin_step) not in allowed_bin_steps:
                    skipped.append({**base,
                                    "reason": f"bin_step {bin_step} not in allowed set "
                                    f"{sorted(allowed_bin_steps)} (George rail)"})
                    continue
            except (ValueError, TypeError):
                # Malformed bin_step (e.g. 'junk') fails closed
                skipped.append({**base,
                                "reason": f"bin_step {bin_step} not in allowed set "
                                f"{sorted(allowed_bin_steps)} (George rail)"})
                continue
        score = v.get("score")
        if score is None or float(score) < min_score:
            skipped.append({**base,
                            "reason": f"score {score} < policy min_open_score {min_score}"})
            continue
        seen.add(addr)
        kept.append(v)
    return kept, skipped