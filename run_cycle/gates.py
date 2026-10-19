"""Gates and filtering logic extracted from run_cycle.py.

Contains:
  - _in_trading_window: check if current time is within configured window
  - _filter_open_candidates: drop candidates that must not become signals
  - _range_center: get pool's current tick/bin position
  - _meteora_bin_step_allowed: check bin_step rail (helper)
"""

from datetime import datetime, timedelta, timezone
import time
from typing import Any, Dict, Tuple

# Constants
MIN_POSITION_USD = 20.0


def _utc_iso():
    """Return current time in Asia/Shanghai (UTC+8) ISO format."""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _meteora_bin_step_allowed(pool: Dict[str, Any], dex: str = None,
                              allowed_bin_steps: set = None,
                              ) -> Tuple[bool, str]:
    """Return (allowed, reason) for a Meteora pool's bin_step rail.

    Fail-closed: an unknown bin_step counts as disallowed. Non-Meteora
    pools are always allowed (bins are a DLMM concept). Callers that know
    the dex should pass it — Missy _pool records may lack a dex field.
    """
    from strategy.bin_step_rail import meteora_bin_step_allowed

    dex = (dex or pool.get("dex") or "").lower()
    if dex != "meteora":
        return True, ""
    # One shared predicate (owner ruling 2026-10-19): >= 4 and <= 400.
    # The historical whitelist membership test is gone; `allowed_bin_steps`
    # is retained only as an optional extra restriction for legacy callers.
    ok, reason = meteora_bin_step_allowed(pool.get("bin_step"))
    if not ok:
        return False, f"{reason} (George rail)"
    if allowed_bin_steps:
        try:
            step = int(pool.get("bin_step"))
        except (TypeError, ValueError):
            return False, "bin_step malformed (fail-closed)"
        if step not in allowed_bin_steps:
            return False, (f"bin_step {step} not in caller-restricted set "
                           f"{sorted(allowed_bin_steps)} (George rail)")
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
    RPC-batched tick enrichment (``tick_source: "rpc_batched"``) may
    populate ``current_tick_equivalent``; this is given high precedence.
    Returns None when the tick/bin is unknown.
    """
    import math

    if dex == "meteora":
        # Priority: active_bin_id > current_tick_equivalent > current_tick_index
        for key in ("active_bin_id", "current_tick_equivalent", "current_tick_index"):
            val = pool.get(key)
            if val is None:
                continue
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
        return None

    # For non-Meteora (Raydium CLMM, Orca Whirlpool): prioritize RPC-enriched fields
    for key in ("current_tick_equivalent", "current_tick", "current_tick_index", "active_bin_id"):
        val = pool.get(key)
        if val is None or val in ("", "0"):
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return None


def _filter_open_candidates(open_candidates: list, active_positions: dict,
                            policy_windows: dict, min_score: float,
                            min_liquidity: float, min_volume: float,
                            allowed_bin_steps: set, reserved_pools: set = None,
                            ctx: dict = None,
                            cooldown_pools: dict = None) -> tuple:
    """Drop open candidates that must not become signals.

    Honors Missy's eligibility flag when present (the default path after
    Phase 2). For older scans without `eligible`, legacy TVL/volume gates
    are applied as a backstop. Sheldon-specific rails (bin steps, open
    score, windows, capital) are always enforced.

    A ``reserved_pools`` set may be provided; pool addresses in this set
    are rotation targets of pending rotations. Candidates for these pools
    are skipped (with a review reason) until the source close settles —
    they re-enter the normal open flow on a later cycle.

    A ``cooldown_pools`` map (pool_address -> earliest reopen timestamp)
    enforces the reopen cooldown after a settled close (2026-10-16
    anti-churn rail): a pool closed moments ago must not re-open in the
    same burst. Expired cooldowns are transparent; closes/claims are never
    affected (opens only pass through this filter).

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

        # Dedup: already holding or duplicate pool.
        # Rotation reservation: pending rotation targets must not open while
        # the source close is still unsettled.
        is_held = addr in active_positions
        is_reserved = reserved_pools is not None and addr in reserved_pools
        if is_reserved:
            skipped.append({**base,
                            "reason": "reserved as rotation target; source close pending"})
            continue
        if is_held:
            skipped.append({**base,
                            "reason": "already holding a position in this pool (dedup)"})
            continue
        cooldown_until = (cooldown_pools or {}).get(addr)
        if cooldown_until and time.time() < float(cooldown_until):
            skipped.append({**base,
                            "reason": f"reopen cooldown after close "
                                      f"({int(float(cooldown_until) - time.time())}s remaining)"})
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
        # Delegated to strategy/bin_step_rail.py — the single implementation
        # (owner ruling 2026-10-19: any step >= 4, <= 400).
        if dex == "meteora":
            _ok, _why = _meteora_bin_step_allowed(pool, dex,
                                                 allowed_bin_steps)
            if not _ok:
                skipped.append({**base, "reason": _why})
                continue
        # Dual-model scoring gate: require cross-model agreement or a weighted
        # combination when the fee-capture model (new_fc) produced a score.
        # In verdict dicts new_fc_score is a SCALAR and its verdict lives in
        # new_fc_verdict (see scoring/pool/score.py). Candidates without a
        # new_fc score fall back to the Missy-only rail (original behavior).
        missy_score = v.get("score")
        new_fc_score = v.get("new_fc_score")
        new_fc_verdict = v.get("new_fc_verdict", "IGNORE")

        missy_above = missy_score is not None and float(missy_score) >= min_score
        new_fc_above = new_fc_score is not None and float(new_fc_score) >= min_score

        use_weighted = bool((ctx or {}).get("use_weighted_combination", False))
        combined_score = None
        if use_weighted and missy_score is not None and new_fc_score is not None:
            missy_w = 0.6  # missy weight
            fc_w = 0.4     # new_fc weight
            combined_score = round(missy_w * float(missy_score)
                                   + fc_w * float(new_fc_score), 2)
            combined_above = combined_score >= min_score
        else:
            combined_above = missy_above and new_fc_above

        if new_fc_score is not None:
            # Second model was computed: enforce the dual-model gate.
            from scoring.config import get_config
            from scoring.pool.score import pool_verdict
            thresholds = (ctx or {}).get("thresholds") or get_config()["pool_thresholds"]
            missy_verdict = (pool_verdict(float(missy_score), thresholds)
                             if missy_score else "IGNORE")

            if not missy_above and not new_fc_above:
                # Neither model above threshold
                skipped.append({**base,
                                "reason": f"dual-model: missy={missy_score}<{min_score} "
                                          f"new_fc={new_fc_score}<{min_score}"})
                continue
            if not use_weighted and missy_verdict != new_fc_verdict:
                # Models disagree without weighted combo - conservative approach
                skipped.append({**base,
                                "reason": f"dual-model disagreement: "
                                          f"missy={missy_verdict} new_fc={new_fc_verdict}"})
                continue
            if not combined_above:
                skipped.append({**base,
                                "reason": f"dual-model combined score {combined_score} < {min_score}"})
                continue
        else:
            # Missy-only rail (original behavior) when the second model is absent.
            if missy_score is None or float(missy_score) < min_score:
                skipped.append({**base,
                                "reason": f"score {missy_score} < policy min_open_score {min_score}"})
                continue

        seen.add(addr)
        kept.append(v)
    return kept, skipped