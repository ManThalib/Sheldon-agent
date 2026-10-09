#!/usr/bin/env python3
"""Phase 1 (2026-10-09) tests: structured pre-open readiness + SOL policy.

Covers the plan's Phase 1 acceptance criteria:
  * no open without a passing readiness object
  * a stale/missing scan fails closed
  * low SOL produces a targeted USDC->SOL top-up for a specific open
  * a SOL-paired pool requires SOL for rent/gas *and* half the position
  * the SOL reserved for rent/gas is not sold back as surplus
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from capital import SOL_MINT, USDC_MINT
from readiness import (
    OPEN_BLOCKED_CLOSE_UNSETTLED,
    OPEN_BLOCKED_POSITION_SCAN_STALE,
    OPEN_BLOCKED_SOL,
    OPEN_BLOCKED_TOKEN_X,
    OPEN_BLOCKED_TOKEN_Y,
    OPEN_BLOCKED_WALLET_SCAN_STALE,
    OPEN_READY,
    build_readiness,
    plan_funding,
    plan_sol_top_up,
    required_sol_lamports,
    sol_overhead_lamports,
)
from run_cycle import write_signals

TOKEN_MINT = "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS"
NOW = 1_800_000_000.0
OVERHEAD = sol_overhead_lamports("meteora")  # 82_323_560 lamports


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


def usdc_token_assets(sol_raw, usdc_raw=100_000_000, token_raw=30_000_000_000):
    return [
        asset(USDC_MINT, usdc_raw, 6, 1.0, "USDC"),
        asset(TOKEN_MINT, token_raw, 9, 2.0, "TKN"),
        asset(SOL_MINT, sol_raw, 9, 117.0, "SOL"),
    ]


class ReadinessModelTests(unittest.TestCase):
    def _build(self, strat, assets, **over):
        kwargs = dict(
            assets=assets,
            wallet_mtime=NOW - 30,
            position_mtime=NOW - 30,
            now=NOW,
            close_settled=True,
        )
        kwargs.update(over)
        return build_readiness(strat, **kwargs)

    def test_usdc_token_pool_ready(self):
        r = self._build(strategy(), usdc_token_assets(sol_raw=OVERHEAD + 5_000_000))
        self.assertTrue(r["ready"], r["blocking_reasons"])
        self.assertEqual(r["status"], OPEN_READY)
        self.assertTrue(all(r["checks"].values()))
        self.assertFalse(r["sol_is_pool_token"])

    def test_insufficient_sol_blocks_with_status(self):
        r = self._build(strategy(), usdc_token_assets(sol_raw=30_000_000))
        self.assertFalse(r["ready"])
        self.assertEqual(r["status"], OPEN_BLOCKED_SOL)
        self.assertFalse(r["checks"]["sol_ready"])
        self.assertTrue(r["checks"]["token_x_ready"])
        self.assertTrue(r["checks"]["token_y_ready"])
        self.assertTrue(any("SOL short" in x for x in r["blocking_reasons"]))

    def test_sol_paired_pool_needs_half_position_in_sol(self):
        # SOL/USDC pool: x=SOL, y=USDC. Position $90 -> half $45 -> 0.3846 SOL.
        strat = strategy(x_mint=SOL_MINT, y_mint=USDC_MINT,
                         x_px=117.0, y_px=1.0, x_dec=9, y_dec=6)
        need = required_sol_lamports("meteora", strat["_pool"], 90.0, 117.0)
        self.assertEqual(need, OVERHEAD + 384_615_385)
        assets = [
            asset(SOL_MINT, need - 1, 9, 117.0, "SOL"),
            asset(USDC_MINT, 100_000_000, 6, 1.0, "USDC"),
        ]
        self.assertFalse(self._build(strat, assets)["ready"])
        assets[0] = asset(SOL_MINT, need, 9, 117.0, "SOL")
        self.assertTrue(self._build(strat, assets)["ready"])

    def test_token_x_short_blocks(self):
        assets = usdc_token_assets(sol_raw=OVERHEAD + 5_000_000, usdc_raw=10_000_000)
        r = self._build(strategy(), assets)
        self.assertEqual(r["status"], OPEN_BLOCKED_TOKEN_X)
        self.assertFalse(r["checks"]["token_x_ready"])

    def test_token_y_short_blocks(self):
        assets = usdc_token_assets(sol_raw=OVERHEAD + 5_000_000, token_raw=1_000)
        r = self._build(strategy(), assets)
        self.assertEqual(r["status"], OPEN_BLOCKED_TOKEN_Y)

    def test_stale_wallet_scan_fails_closed(self):
        r = self._build(strategy(), usdc_token_assets(OVERHEAD + 5_000_000),
                        wallet_mtime=NOW - 5000)
        self.assertEqual(r["status"], OPEN_BLOCKED_WALLET_SCAN_STALE)
        self.assertFalse(r["ready"])

    def test_missing_position_mtime_fails_closed(self):
        r = self._build(strategy(), usdc_token_assets(OVERHEAD + 5_000_000),
                        position_mtime=0.0)
        self.assertEqual(r["status"], OPEN_BLOCKED_POSITION_SCAN_STALE)
        self.assertFalse(r["ready"])

    def test_close_unsettled_blocks(self):
        r = self._build(strategy(), usdc_token_assets(OVERHEAD + 5_000_000),
                        close_settled=False, close_reason="close still EMITTED")
        self.assertEqual(r["status"], OPEN_BLOCKED_CLOSE_UNSETTLED)

    def test_missing_assets_fails_closed(self):
        r = self._build(strategy(), [])
        self.assertFalse(r["ready"])


class SolTopUpTests(unittest.TestCase):
    def _readiness(self, assets, sol_policy=None, usdc_raw=100_000_000):
        strat = strategy()
        return build_readiness(
            strat, assets=assets, wallet_mtime=NOW - 30,
            position_mtime=NOW - 30, now=NOW, close_settled=True,
            sol_policy=sol_policy,
        )

    def test_top_up_allowed_when_sol_is_sole_failure(self):
        assets = usdc_token_assets(sol_raw=30_000_000)
        r = self._readiness(assets)
        out = plan_sol_top_up(r, assets=assets, required_usdc_raw=45_000_000)
        self.assertTrue(out["allowed"], out["reason"])
        spec = out["spec"]
        self.assertEqual(spec["input_mint"], USDC_MINT)
        self.assertEqual(spec["output_mint"], SOL_MINT)
        self.assertEqual(spec["direction"], "buy")
        self.assertEqual(spec["readiness_dependency"], "sol")
        self.assertEqual(spec["for_pool"], "p1")

    def test_top_up_blocked_when_a_token_is_short(self):
        assets = usdc_token_assets(sol_raw=30_000_000, token_raw=1_000)
        r = self._readiness(assets)
        out = plan_sol_top_up(r, assets=assets, required_usdc_raw=45_000_000)
        self.assertFalse(out["allowed"])
        self.assertIsNone(out["spec"])

    def test_top_up_preserves_position_usdc(self):
        # USDC available equals the position's USDC need -> nothing left.
        assets = usdc_token_assets(sol_raw=30_000_000, usdc_raw=45_000_000)
        r = self._readiness(assets)
        self.assertTrue(r["checks"]["token_y_ready"])
        out = plan_sol_top_up(r, assets=assets, required_usdc_raw=45_000_000)
        self.assertFalse(out["allowed"])
        self.assertIn("after position funding", out["reason"])

    def test_top_up_respects_max_top_up_usd(self):
        assets = usdc_token_assets(sol_raw=30_000_000)
        r = self._readiness(assets, sol_policy={"max_top_up_usd": 1.0})
        out = plan_sol_top_up(
            r, assets=assets, required_usdc_raw=45_000_000,
            sol_policy={"max_top_up_usd": 1.0},
        )
        self.assertFalse(out["allowed"])
        self.assertIn("max_top_up_usd", out["reason"])

    def test_top_up_disabled_by_policy(self):
        assets = usdc_token_assets(sol_raw=30_000_000)
        pol = {"auto_top_up_enabled": False}
        r = self._readiness(assets, sol_policy=pol)
        out = plan_sol_top_up(r, assets=assets, required_usdc_raw=45_000_000,
                              sol_policy=pol)
        self.assertFalse(out["allowed"])
        self.assertIn("disabled", out["reason"])

    def test_no_top_up_when_already_ready(self):
        assets = usdc_token_assets(sol_raw=OVERHEAD + 5_000_000)
        r = self._readiness(assets)
        self.assertTrue(r["ready"])
        out = plan_sol_top_up(r, assets=assets, required_usdc_raw=45_000_000)
        self.assertFalse(out["allowed"])


class SolReserveTests(unittest.TestCase):
    def test_funding_reserves_sol_overhead_not_flat_cap(self):
        """The SOL kept for rent/gas must not be sold back as surplus."""
        strat = strategy()
        strat["_pool"] = pool()
        # SOL = overhead + small buffer; USDC/TOKEN funded.
        assets = usdc_token_assets(sol_raw=OVERHEAD + 5_000_000)
        common = dict(wallet_mtime=NOW - 30)

        legacy = plan_funding([strat], assets, **common)
        legacy_sells = [s for s in legacy["prep_swaps"] if s["direction"] == "sell"]
        self.assertTrue(any(s["output_mint"] == USDC_MINT for s in legacy_sells),
                        "legacy 0.02 cap liquidates the rent reserve")

        fixed = plan_funding([strat], assets, sol_reserve_lamports=OVERHEAD, **common)
        fixed_sells = [s for s in fixed["prep_swaps"]
                       if s["direction"] == "sell" and s["input_mint"] == SOL_MINT]
        self.assertEqual(fixed_sells, [], "rent reserve must stay unspent")


class WriteSignalsBackstopTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.signals_dir = os.path.join(self.root, "signals")
        self.pos_dir = os.path.join(self.root, "positions")
        os.makedirs(self.pos_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _report(self, readiness):
        v = {
            "action": "OPEN_CANDIDATE", "pool_address": "p1", "dex": "meteora",
            "score": 90.0, "evidence": {}, "_pool": pool(),
        }
        return {
            "wallet": {"wallet_id": "main", "dust_assets": []},
            "verdicts": [v],
            "open_skipped": [],
            "strategies": [strategy()],
            "funding": {"funded": [{"pool_address": "p1"}], "unfunded": [],
                        "prep_swaps_allowed": [], "prep_swaps_blocked": []},
            "readiness": readiness,
        }

    def test_open_blocked_without_readiness_object(self):
        created, review = write_signals(self._report({}), self.signals_dir,
                                        self.pos_dir)
        actions = []
        for path in created:
            with open(path, encoding="utf-8") as fh:
                actions.append(json.load(fh)["action"])
        self.assertNotIn("open", actions)
        self.assertTrue(any("readiness gate" in r["reason"] for r in review))

    def test_open_allowed_with_passing_readiness(self):
        ready = {"p1": {"ready": True, "status": OPEN_READY}}
        created, _review = write_signals(self._report(ready), self.signals_dir,
                                         self.pos_dir)
        actions = []
        for path in created:
            with open(path, encoding="utf-8") as fh:
                actions.append(json.load(fh)["action"])
        self.assertIn("open", actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
