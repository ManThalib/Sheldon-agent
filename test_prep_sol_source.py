#!/usr/bin/env python3
"""SOL-sourced capital prep tests (2026-10-16).

Sheldon's prep swaps were USDC-only: surplus native SOL counted as deployable
capital but could not fund a missing pool token. These tests pin the new
source-aware behavior:

  * USDC remains the first funding source (unchanged amounts when it can pay)
  * excess SOL above the calibrated reserve funds the missing token directly
  * SOL funds the USDC side when USDC itself is short
  * a SOL-paired target keeps its own SOL deposit (only true excess is spent)
  * SOL is never asked to fund its own side, and missing prices fail closed
  * the target's USDC side is carried on the signal as committed capital
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from capital import SOL_MINT, USDC_MINT
from idle_sweep import _signal_usdc_committed
from readiness import (
    build_prep_swap_signal,
    plan_funding,
    sol_overhead_lamports,
)

TOKEN_MINT = "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS"
NOW = 1_800_000_000.0
OVERHEAD = sol_overhead_lamports("meteora")
SOL_PX = 117.0
BUFFER = 1.02
LAMPORTS_PER_SOL = 1_000_000_000


def pool(x_mint=USDC_MINT, y_mint=TOKEN_MINT,
         x_px=1.0, y_px=2.0, x_dec=6, y_dec=9, dex="meteora"):
    return {
        "pool_address": "p1", "dex": dex,
        "bin_step": 10, "tick_spacing": 10, "active_bin_id": -100,
        "token_x_address": x_mint, "token_y_address": y_mint,
        "token_x_price_usd": x_px, "token_y_price_usd": y_px,
        "token_x_decimals": x_dec, "token_y_decimals": y_dec,
    }


def strategy(position_usd=90.0, **kw):
    return {
        "pool_address": "p1", "dex": kw.pop("dex", "meteora"),
        "suggested_usdc": position_usd, "score": 90.0,
        "center": -100, "bin_range": {"lower": -110, "upper": -90},
        "_pool": pool(**kw),
    }


def asset(mint, raw, decimals, price, symbol=""):
    return {"mint": mint, "amount_raw": str(raw), "decimals": decimals,
            "price_usd": price, "symbol": symbol}


def plan(strat, assets):
    return plan_funding([strat], assets, wallet_mtime=NOW - 30,
                        sol_reserve_lamports=OVERHEAD)


def buy(plan_dict, output_mint):
    return [s for s in plan_dict["prep_swaps"]
            if s["direction"] == "buy" and s["output_mint"] == output_mint]


class SolFundingSourceTests(unittest.TestCase):
    def test_sol_only_wallet_funds_both_sides(self):
        """No USDC at all: excess SOL sources USDC and the target token."""
        strat = strategy()  # USDC/TOKEN, $90
        assets = [asset(SOL_MINT, 2 * LAMPORTS_PER_SOL, 9, SOL_PX, "SOL")]
        out = plan(strat, assets)

        usdc_specs = buy(out, USDC_MINT)
        token_specs = buy(out, TOKEN_MINT)
        self.assertEqual(len(usdc_specs), 1)
        self.assertEqual(len(token_specs), 1)

        # USDC side: $45 + 2% buffer, paid in lamports at sol_price.
        usdc = usdc_specs[0]
        self.assertEqual(usdc["input_mint"], SOL_MINT)
        expected_usdc_lamports = int(
            __import__("math").ceil(45.0 * BUFFER / SOL_PX * LAMPORTS_PER_SOL))
        self.assertEqual(int(usdc["amount_raw"]), expected_usdc_lamports)
        self.assertEqual(usdc["for_pool"], "p1")

        # Token side: 22.5 tokens at $2 = $45 + buffer, also from SOL.
        tok = token_specs[0]
        self.assertEqual(tok["input_mint"], SOL_MINT)
        expected_tok_lamports = int(
            __import__("math").ceil(45.0 * BUFFER / SOL_PX * LAMPORTS_PER_SOL))
        self.assertEqual(int(tok["amount_raw"]), expected_tok_lamports)

        # Never spend into the reserve.
        spent = sum(int(s["amount_raw"]) for s in out["prep_swaps"])
        self.assertLessEqual(spent, 2 * LAMPORTS_PER_SOL - OVERHEAD)

    def test_usdc_still_first_when_it_can_pay(self):
        """USDC sufficient: amounts match the legacy USDC-only formula."""
        strat = strategy()
        assets = [
            asset(USDC_MINT, 200_000_000, 6, 1.0, "USDC"),
            asset(SOL_MINT, 5 * LAMPORTS_PER_SOL, 9, SOL_PX, "SOL"),
        ]
        out = plan(strat, assets)
        specs = out["prep_swaps"]
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec["input_mint"], USDC_MINT)
        self.assertEqual(spec["output_mint"], TOKEN_MINT)
        # 22.5 tokens * $2 * 1.02 = $45.90 -> 45_900_000 raw USDC (legacy math).
        self.assertEqual(int(spec["amount_raw"]), 45_900_000)
        self.assertNotIn("committed_usdc_raw", spec)

    def test_usdc_short_falls_back_to_sol(self):
        strat = strategy()
        assets = [
            asset(USDC_MINT, 10_000_000, 6, 1.0, "USDC"),
            asset(SOL_MINT, 3 * LAMPORTS_PER_SOL, 9, SOL_PX, "SOL"),
        ]
        out = plan(strat, assets)
        # x side is USDC but already short -> SOL must source the USDC side.
        usdc_specs = buy(out, USDC_MINT)
        self.assertEqual(len(usdc_specs), 1)
        self.assertEqual(usdc_specs[0]["input_mint"], SOL_MINT)
        # y side short (no TOKEN) and USDC cannot cover it -> SOL fallback.
        token_specs = buy(out, TOKEN_MINT)
        self.assertEqual(len(token_specs), 1)
        self.assertEqual(token_specs[0]["input_mint"], SOL_MINT)

    def test_sol_paired_target_keeps_its_own_deposit(self):
        """Only SOL above reserve + the target's SOL deposit is spendable."""
        strat = strategy(x_mint=SOL_MINT, y_mint=USDC_MINT,
                         x_px=SOL_PX, y_px=1.0, x_dec=9, y_dec=6)
        deposit = int((45.0 / SOL_PX) * LAMPORTS_PER_SOL)  # 384_615_384
        excess = 500_000_000
        assets = [
            asset(SOL_MINT, OVERHEAD + deposit + excess, 9, SOL_PX, "SOL"),
            asset(USDC_MINT, 0, 6, 1.0, "USDC"),
        ]
        out = plan(strat, assets)
        specs = buy(out, USDC_MINT)
        self.assertEqual(len(specs), 1)
        amount = int(specs[0]["amount_raw"])
        self.assertLessEqual(amount, excess)
        self.assertGreaterEqual(
            OVERHEAD + deposit + excess - amount, OVERHEAD + deposit)
        self.assertTrue(any("SOL deposit need" in n for n in out["notes"]))

    def test_sol_paired_target_blocks_when_excess_insufficient(self):
        strat = strategy(x_mint=SOL_MINT, y_mint=USDC_MINT,
                         x_px=SOL_PX, y_px=1.0, x_dec=9, y_dec=6)
        deposit = int((45.0 / SOL_PX) * LAMPORTS_PER_SOL)
        assets = [
            asset(SOL_MINT, OVERHEAD + deposit + 10_000_000, 9, SOL_PX, "SOL"),
            asset(USDC_MINT, 0, 6, 1.0, "USDC"),
        ]
        out = plan(strat, assets)
        self.assertEqual(buy(out, USDC_MINT), [])
        self.assertTrue(any("excess available" in n for n in out["notes"]))

    def test_sol_never_funds_its_own_side(self):
        strat = strategy(x_mint=SOL_MINT, y_mint=TOKEN_MINT,
                         x_px=SOL_PX, y_px=2.0, x_dec=9, y_dec=9)
        deposit = int((45.0 / SOL_PX) * LAMPORTS_PER_SOL)
        assets = [
            asset(SOL_MINT, OVERHEAD + deposit - 100_000_000, 9, SOL_PX, "SOL"),
            asset(USDC_MINT, 1_000_000, 6, 1.0, "USDC"),
        ]
        out = plan(strat, assets)
        for spec in out["prep_swaps"]:
            self.assertNotEqual(
                (spec["input_mint"], spec["output_mint"]), (SOL_MINT, SOL_MINT))
        self.assertTrue(any("cannot fund its own side" in n for n in out["notes"]))

    def test_missing_sol_price_fails_closed(self):
        strat = strategy()
        assets = [
            asset(USDC_MINT, 1_000_000, 6, 1.0, "USDC"),
            asset(SOL_MINT, 3 * LAMPORTS_PER_SOL, 9, 0.0, "SOL"),
        ]
        out = plan(strat, assets)
        self.assertEqual(buy(out, TOKEN_MINT), [])
        self.assertTrue(any("SOL price unknown" in n for n in out["notes"]))

    def test_insufficient_excess_sol_emits_nothing(self):
        strat = strategy()
        assets = [
            asset(USDC_MINT, 0, 6, 1.0, "USDC"),
            asset(SOL_MINT, OVERHEAD + 1_000_000, 9, SOL_PX, "SOL"),
        ]
        out = plan(strat, assets)
        self.assertEqual(out["prep_swaps"], [])
        self.assertTrue(any("excess available" in n for n in out["notes"]))


class SolPrepSignalTests(unittest.TestCase):
    def test_signal_carries_sol_source_and_committed_usdc(self):
        strat = strategy()  # USDC/TOKEN
        assets = [asset(SOL_MINT, 2 * LAMPORTS_PER_SOL, 9, SOL_PX, "SOL")]
        out = plan(strat, assets)
        token_spec = buy(out, TOKEN_MINT)[0]
        sig = build_prep_swap_signal(token_spec, 0, 123)
        self.assertEqual(sig["input_mint"], SOL_MINT)
        self.assertEqual(sig["output_mint"], TOKEN_MINT)
        self.assertIn("with SOL", sig["reason"])
        # The target's USDC side ($45) rides along for the idle sweep.
        self.assertEqual(sig["usdc_side_raw"], "45000000")

    def test_idle_sweep_counts_sol_prep_usdc_side(self):
        sig = {"action": "swap", "input_mint": SOL_MINT,
               "output_mint": TOKEN_MINT, "amount": "450000000",
               "usdc_side_raw": "45000000"}
        self.assertAlmostEqual(_signal_usdc_committed(sig), 45.0)
        usdc_sig = {"action": "swap", "input_mint": USDC_MINT,
                    "output_mint": TOKEN_MINT, "amount": "45000000"}
        self.assertAlmostEqual(_signal_usdc_committed(usdc_sig), 45.0)

    def test_usdc_prep_signal_has_no_side_field(self):
        strat = strategy()
        assets = [asset(USDC_MINT, 200_000_000, 6, 1.0, "USDC"),
                  asset(SOL_MINT, 5 * LAMPORTS_PER_SOL, 9, SOL_PX, "SOL")]
        spec = plan(strat, assets)["prep_swaps"][0]
        sig = build_prep_swap_signal(spec, 0, 123)
        self.assertNotIn("usdc_side_raw", sig)
        self.assertIn("with USDC", sig["reason"])
        self.assertAlmostEqual(_signal_usdc_committed(sig), 45.9)


if __name__ == "__main__":
    unittest.main()