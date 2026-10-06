"""Sheldon capital readiness module — modular package.

Split from the original readiness.py into focused sub-modules:
  - wallet: raw wallet scan loading
  - funding: funding plan builder
  - prep_swap_gates: swap gating (rescan wait + hourly loop guards)
  - prep_swap: build George-schema swap signals from prep specs

All original ``readiness.X`` names are re-exported at the top level so that
``import readiness`` (or ``from readiness import ...``) continues to work
identically to the original monolith.
"""

from .wallet import load_raw_wallet, newest_wallet_scan
from .funding import plan_funding, _required_amounts
from .prep_swap_gates import gate_prep_swaps, _prep_swap_history, _created_epoch
from .prep_swap import build_prep_swap_signal
from . import prep_ledger

# Backward-compatible re-exports (mirror original readiness.py API)
load_raw_wallet_func = load_raw_wallet
newest_wallet_scan_func = newest_wallet_scan
plan_funding_func = plan_funding
gate_prep_swaps_func = gate_prep_swaps
_build_open_signal_func_alias = None  # not applicable

# Expose constants for CLI / test access
PREP_CONFIRM_GRACE_SECONDS = 60
MAX_PREP_SWAPS_PER_MINT_PER_HOUR = 3
MAX_WALLET_SCAN_AGE_SECONDS = 180
SOL_RESERVE_LAMPORTS = 20_000_000
USDC_DECIMALS = 6
BUY_BUFFER_PCT = 2.0
MIN_PREP_SWAP_USD = 1.0
SELL_SURPLUS_MIN_USD = 2.0

# Prep ledger (state-based dedupe) constants
PREP_LEDGER_TTL_SECONDS = prep_ledger.PREP_LEDGER_TTL_SECONDS
PREP_REJECT_COOLDOWN_SECONDS = prep_ledger.PREP_REJECT_COOLDOWN_SECONDS
PREP_OSCILLATION_WINDOW_SECONDS = prep_ledger.PREP_OSCILLATION_WINDOW_SECONDS
DEFAULT_JOURNAL_DIR = prep_ledger.DEFAULT_JOURNAL_DIR

__all__ = [
    "load_raw_wallet",
    "newest_wallet_scan",
    "plan_funding",
    "gate_prep_swaps",
    "build_prep_swap_signal",
    "prep_ledger",
    "_prep_swap_history",
    "_created_epoch",
    "PREP_CONFIRM_GRACE_SECONDS",
    "MAX_PREP_SWAPS_PER_MINT_PER_HOUR",
    "MAX_WALLET_SCAN_AGE_SECONDS",
    "SOL_RESERVE_LAMPORTS",
    "USDC_DECIMALS",
    "BUY_BUFFER_PCT",
    "MIN_PREP_SWAP_USD",
    "SELL_SURPLUS_MIN_USD",
    "PREP_LEDGER_TTL_SECONDS",
    "PREP_REJECT_COOLDOWN_SECONDS",
    "PREP_OSCILLATION_WINDOW_SECONDS",
    "DEFAULT_JOURNAL_DIR",
]


def main() -> int:
    """No-op entry point for the modular package."""
    return 0