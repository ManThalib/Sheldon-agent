"""Sheldon cycle runner — modular package.

Split from the original run_cycle.py into focused sub-modules:
  - gates: trading window and open-candidate filtering
  - signals: George-signal building and writing
  - report: human-readable logs and summaries
  - state: persistent state management

Note: The original run_cycle.py API is preserved via backward-compatible
wrappers in this package __init__.  Sub-modules have the new implementation.
"""

from run_cycle.gates import _range_center
from run_cycle.signals import (
    _build_open_signal as _build_open_signal_impl,
    _build_dust_swap_signal as _build_dust_swap_signal_impl,
    write_signals as write_signals_impl,
)
from run_cycle.rotation import (
    _release_unemitted_rotations,
    _settle_rotations,
)
from run_cycle.report import (
    append_log as append_log_impl,
    _short_summary as _short_summary_impl,
    _wake_george as _wake_george_impl,
)


# -----------------------------------------------------------
# Backward-compatible exports (match original run_cycle.py API)
# -----------------------------------------------------------

def _in_trading_window(windows: dict, action: str) -> bool:
    """Original _in_trading_window from run_cycle.py."""
    from run_cycle.gates import _in_trading_window as _f
    return _f(windows, action)


def _filter_open_candidates(
    open_candidates: list,
    active_positions: dict,
    policy_windows: dict = None,
    min_score: float = 70.0,
    min_liquidity: float = 25000.0,
    min_volume: float = 5000.0,
    allowed_bin_steps: set = None,
) -> tuple:
    """Original _filter_open_candidates from run_cycle.py.

    Defaults match the original sheldon_policy.json values.
    """
    if policy_windows is None:
        from strategy import get_policy
        p = get_policy()
        policy_windows = {
            "open_window_utc": p.get("open_window_utc"),
            "close_window_utc": p.get("close_window_utc"),
            "blackout_dates": p.get("blackout_dates"),
        }
    if allowed_bin_steps is None:
        allowed_bin_steps = {4, 10, 20, 25, 50, 100}
    from run_cycle.gates import _filter_open_candidates as _f
    return _f(
        open_candidates, active_positions,
        policy_windows, min_score, min_liquidity, min_volume,
        allowed_bin_steps,
    )


def _load_active_positions(positions_dir: str) -> dict:
    """Original _load_active_positions from run_cycle.py."""
    import lp_scoring
    pos_path = lp_scoring.newest_file(positions_dir, "position_scan")
    if not pos_path:
        return {}
    try:
        data = lp_scoring.load_json(pos_path)
    except Exception:
        return {}
    positions = data.get("positions", []) if isinstance(data, dict) else data
    return {p.get("pool_address"): p for p in positions
            if p.get("pool_address") and p.get("status") != "closed"}


def _build_open_signal(strategy: dict, v: dict, base: int, min_score: float = 70.0) -> dict:
    """Original _build_open_signal from run_cycle.py (backward compatible).

    Note: The new implementation in signals module has a different signature
    (adds pool and supported_dexes parameters). This wrapper maintains
    compatibility with existing callers.
    """
    from run_cycle.signals import _build_open_signal as _impl
    # Call with pool=None and supported_dexes=SUPPORTED_DEXES for backward compat
    # The new implementation has a different signature (adds pool and supported_dexes).
    # Use keyword args to maintain backward compatibility.
    pool = v.get("_pool") or {}
    supported_dexes = {"meteora", "raydium", "orca"}
    return _impl(strategy, v, pool, idx=1, base=base, min_score=70.0,
                 supported_dexes=supported_dexes)


def _build_dust_swap_signal(asset: dict, idx: int, base: int) -> dict:
    """Original _build_dust_swap_signal from run_cycle.py."""
    from run_cycle.signals import _build_dust_swap_signal as _f
    return _f(asset, idx, base)


def write_signals(report: dict, signals_dir: str, positions_dir: str) -> tuple:
    """Original write_signals from run_cycle.py (backward compatible)."""
    from run_cycle.signals import write_signals as _f
    return _f(report, signals_dir, positions_dir)


def append_log(report: dict, memory_dir: str, signals_created: list, review: list):
    """Original append_log from run_cycle.py."""
    from run_cycle.report import append_log as _f
    return _f(report, memory_dir, signals_created, review)


def _short_summary(report: dict, review: list = None) -> str:
    """Original _short_summary from run_cycle.py."""
    from run_cycle.report import _short_summary as _f
    return _f(report, review)


def _wake_george(signals_created: list, review: list):
    """Original _wake_george from run_cycle.py."""
    from run_cycle.report import _wake_george as _f
    return _f(signals_created, review)


# Backward-compatible names that tests expect
_build_open_signal = _build_open_signal
_build_dust_swap_signal = _build_dust_swap_signal

# Rotation state management (persists across cycles for rotation detection)
import json
import os

def _load_rotation_state(state_dir: str) -> dict:
    """Load rotation state from JSON file, persist across cycles."""
    path = os.path.join(state_dir, "rotation_state.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {"rotations": {}, "last_rotation_cycle": {}}


def _save_rotation_state(state_dir: str, state: dict) -> None:
    """Persist rotation state atomically."""
    path = os.path.join(state_dir, "rotation_state.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass

__all__ = [
    "_in_trading_window",
    "_filter_open_candidates",
    "_load_active_positions",
    "_build_open_signal",
    "_build_dust_swap_signal",
    "write_signals",
    "append_log",
    "_short_summary",
    "_wake_george",
    "_load_rotation_state",
    "_save_rotation_state",
]