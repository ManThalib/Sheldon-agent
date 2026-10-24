#!/usr/bin/env python3
"""Deployable capital must be stablecoin-only.

Regression target (2026-10-18): ``summarize_wallet`` computed
``deployable_usdc = idle_usdc + dust_total``, where "dust" was every
non-SOL, non-USDC asset above $1. A real wallet scan holding
USDC $43.375 + WBTC $40.115 reported ``deployable_usdc = 83.49`` and
``open_eligible = true`` — off $40 of WBTC that cannot fund an open until it
is swapped. That is a false-eligibility and oversizing bug: a strategy could
be sized on money that is not there.

Contract pinned here:
  - volatile assets are never part of ``deployable_usdc`` /
    ``deployable_stable_usd``;
  - they are reported as ``liquidation_required_usd``;
  - they cannot, alone, make ``open_eligible`` true;
  - they cannot inflate ``suggested_position_usd``.

Hermetic: pure dict inputs, no RPC, no files.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capital import (  # noqa: E402
    DUST_MIN_USD,
    SOL_MINT,
    STABLE_MINTS,
    USDC_MINT,
    summarize_wallet,
)
from strategy.capital_plan import capital_plan  # noqa: E402
from strategy.position_sizing import open_eligible, suggested_position_usd  # noqa: E402

WBTC_MINT = "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"
JUP_MINT = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"


def _asset(mint, symbol, value, price=1.0, native_sol=False, amount_raw="1000000"):
    return {
        "mint": mint,
        "symbol": symbol,
        "amount_raw": amount_raw,
        "amount_ui": 1.0,
        "decimals": 6,
        "price_usd": price,
        "total_value_usd": value,
        "is_native_sol": native_sol,
    }


def _wallet(assets, total=None):
    return {
        "wallet": "B8qv53pKyW2sgP4VYik1GkkZ6NHNZZn6K5VTyVm55tTa",
        "wallet_id": "main",
        "total_usd": sum(a["total_value_usd"] for a in assets) if total is None else total,
        "assets": assets,
    }


class LiveShapeRegression(unittest.TestCase):
    """The exact shape of wallet_screen-20261018-045202.json."""

    def setUp(self):
        self.data = _wallet([
            _asset(USDC_MINT, "USDC", 43.375, price=1.0),
            _asset(WBTC_MINT, "WBTC", 40.115, price=60000.0),
            _asset(SOL_MINT, "SOL", 68.407, price=170.0, native_sol=True,
                   amount_raw="402394117"),
        ])
        self.summary = summarize_wallet(self.data)

    def test_deployable_excludes_wbtc(self):
        self.assertAlmostEqual(self.summary["deployable_usdc"], 43.375, places=6)

    def test_deployable_stable_equals_idle_stable(self):
        self.assertAlmostEqual(
            self.summary["deployable_stable_usd"], 43.375, places=6
        )
        self.assertAlmostEqual(self.summary["idle_stable_usd"], 43.375, places=6)

    def test_wbtc_is_liquidation_required_not_deployable(self):
        self.assertAlmostEqual(
            self.summary["liquidation_required_usd"], 40.115, places=6
        )
        self.assertAlmostEqual(self.summary["dust_total_usdc"], 40.115, places=6)

    def test_deployable_is_not_idle_plus_dust(self):
        """The old formula must be provably gone."""
        old_formula = self.summary["idle_usdc"] + self.summary["dust_total_usdc"]
        self.assertAlmostEqual(old_formula, 83.49, places=6)
        self.assertNotAlmostEqual(self.summary["deployable_usdc"], old_formula, places=2)

    def test_sol_stays_reserved_and_not_deployable_usd(self):
        self.assertAlmostEqual(self.summary["idle_sol_usd"], 68.407, places=6)
        self.assertAlmostEqual(self.summary["reserved_total_usdc"], 68.407, places=6)
        self.assertNotIn(
            SOL_MINT, [a["mint"] for a in self.summary["stable_assets"]]
        )


class WbtcAloneCannotFundAnOpen(unittest.TestCase):
    def test_wbtc_only_wallet_is_not_open_eligible(self):
        summary = summarize_wallet(_wallet([
            _asset(WBTC_MINT, "WBTC", 500.0, price=60000.0),
        ]))
        self.assertAlmostEqual(summary["deployable_usdc"], 0.0, places=6)
        self.assertAlmostEqual(summary["liquidation_required_usd"], 500.0, places=6)
        self.assertFalse(open_eligible(summary))
        self.assertFalse(capital_plan(summary)["open_eligible"])

    def test_wbtc_does_not_inflate_position_size(self):
        only_wbtc = summarize_wallet(_wallet([
            _asset(WBTC_MINT, "WBTC", 500.0, price=60000.0),
        ]))
        self.assertEqual(suggested_position_usd(only_wbtc["deployable_usdc"]), 0.0)

    def test_small_usdc_plus_large_wbtc_sizes_on_usdc_only(self):
        """$30 USDC + $400 WBTC must size like $30, not $430."""
        summary = summarize_wallet(_wallet([
            _asset(USDC_MINT, "USDC", 30.0),
            _asset(WBTC_MINT, "WBTC", 400.0, price=60000.0),
        ]))
        self.assertAlmostEqual(summary["deployable_usdc"], 30.0, places=6)
        # 75% of 30 = 22.5, above MIN_POSITION_USD (20) => eligible.
        self.assertTrue(open_eligible(summary))
        self.assertAlmostEqual(
            capital_plan(summary)["suggested_position_usdc"], 22.5, places=6
        )
        # 75% of the buggy 430 would have been 322.5 -> capped at 100.
        self.assertLess(capital_plan(summary)["suggested_position_usdc"], 100.0)

    def test_usdc_just_below_min_with_big_wbtc_is_ineligible(self):
        summary = summarize_wallet(_wallet([
            _asset(USDC_MINT, "USDC", 19.0),
            _asset(WBTC_MINT, "WBTC", 400.0, price=60000.0),
        ]))
        self.assertFalse(open_eligible(summary))


class StableRegistryTests(unittest.TestCase):
    def test_usdc_is_the_only_default_stable(self):
        self.assertEqual(STABLE_MINTS, {USDC_MINT})

    def test_volatile_mints_are_not_in_registry(self):
        for mint in (WBTC_MINT, JUP_MINT, SOL_MINT):
            self.assertNotIn(mint, STABLE_MINTS)

    def test_sub_dust_volatile_is_neither_deployable_nor_liquidation(self):
        summary = summarize_wallet(_wallet([
            _asset(USDC_MINT, "USDC", 25.0),
            _asset(JUP_MINT, "JUP", DUST_MIN_USD - 0.01),
        ]))
        self.assertAlmostEqual(summary["deployable_usdc"], 25.0, places=6)
        self.assertAlmostEqual(summary["liquidation_required_usd"], 0.0, places=6)
        self.assertEqual(summary["dust_assets"], [])

    def test_missing_mint_never_becomes_stable(self):
        summary = summarize_wallet(_wallet([
            _asset("", "???", 50.0),
            _asset(USDC_MINT, "USDC", 25.0),
        ]))
        self.assertAlmostEqual(summary["deployable_usdc"], 25.0, places=6)
        self.assertAlmostEqual(summary["liquidation_required_usd"], 50.0, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
