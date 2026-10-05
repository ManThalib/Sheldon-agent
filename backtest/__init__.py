"""Sheldon backtest: replay historical Missy scans through the scoring engine.

Modular package split from the original backtest.py:
  - pool_replay: pool replay logic (forward APR, survival tracking)
  - position_replay: position replay logic (verdict paths, PnL tracking)
  - synthetic_pnl: Synthetic PnL simulation (fee/IL/swap cost simulation)
  - reports: Report generation (comparative analysis)

Answers three questions with data instead of intuition:
  1. Do OPEN_CANDIDATE picks earn more forward fee APR than the average pool?
  2. Do would-be open ranges survive (price stays inside) over the horizon?
  3. How do position verdicts play out in forward value terms?

New in this version:
  4. Synthetic position PnL: simulate a $100 50/50 position through the
     horizon, including fees, impermanent loss, rebalancing when price leaves
     the range, and realistic swap/gas costs.
  5. Robustness: walk-forward first-half vs second-half performance and
     bootstrap confidence intervals for mean PnL.
"""

import argparse
import json
import sys
import os

from .pool_replay import (
    resolve_scorer,
    run_gate_report,
    _scan_dt,
    list_scans,
    load_scan,
    scan_completeness,
    _range_center,
    _derive_tick,
    price_in_range,
    _simulate_position,
    backtest_pools,
    _walk_forward_stats,
    _bootstrap,
)
from .position_replay import backtest_positions
from .synthetic_pnl import _impermanent_loss, _simulate_position as _sns_simulate_position
from .reports import (
    print_pool_replay,
    print_synthetic_pnl,
    print_robustness,
    print_position_replay,
    print_detail,
)

# ---------------------------------------------------------------------------
# Default simulation constants (mirror original backtest.py defaults)
# ---------------------------------------------------------------------------
DEFAULT_DATA_DIR = "/data/missy-data"
DEFAULT_POSITION_VALUE_USD = 100.0
DEFAULT_ENTRY_COST_BPS = 50
DEFAULT_EXIT_COST_BPS = 50
DEFAULT_CLAIM_COST_USD = 0.02
DEFAULT_BOOTSTRAP_SAMPLES = 1000

# Backward-compatible aliases
_impermanent_loss = _impermanent_loss
_simulate_position = _sns_simulate_position

# Expose report-printing functions at top level for CLI usage
print_pool_replay = print_pool_replay
print_synthetic_pnl = print_synthetic_pnl
print_robustness = print_robustness
print_position_replay = print_position_replay
print_detail = print_detail


def main() -> int:
    """Original entry point; delegating to keep CLI behaviour identical."""
    import backtest as _mod
    return _mod.main()


# ---------------------------------------------------------------------------
# Ensure constants are also available as backtest.<name> for CLI/submodules.
# ---------------------------------------------------------------------------
# (Already defined above; no-op keep-alive block to confirm intent.)
_ = (DEFAULT_DATA_DIR, DEFAULT_POSITION_VALUE_USD, DEFAULT_ENTRY_COST_BPS,
     DEFAULT_EXIT_COST_BPS, DEFAULT_CLAIM_COST_USD, DEFAULT_BOOTSTRAP_SAMPLES)

__all__ = [
    "resolve_scorer",
    "run_gate_report",
    "_scan_dt",
    "list_scans",
    "load_scan",
    "scan_completeness",
    "_range_center",
    "_derive_tick",
    "price_in_range",
    "_impermanent_loss",
    "_simulate_position",
    "backtest_pools",
    "_walk_forward_stats",
    "_bootstrap",
    "DEFAULT_DATA_DIR",
    "DEFAULT_POSITION_VALUE_USD",
    "DEFAULT_ENTRY_COST_BPS",
    "DEFAULT_EXIT_COST_BPS",
    "DEFAULT_CLAIM_COST_USD",
    "DEFAULT_BOOTSTRAP_SAMPLES",
    "backtest_positions",
    "print_pool_replay",
    "print_synthetic_pnl",
    "print_robustness",
    "print_position_replay",
    "print_detail",
    "main",
]