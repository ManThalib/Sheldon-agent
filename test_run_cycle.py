#!/usr/bin/env python3
"""Tests for Sheldon run_cycle, capital, and strategy modules."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lp_scoring
from capital import RESERVED_MINTS, SOL_MINT, USDC_MINT, summarize_wallet
from run_cycle import write_signals
from strategy import (
    DEFAULT_MAX_POSITION_USD,
    MAX_POSITION_OPEN_PER_CYCLE,
    MIN_POSITION_USD,
    adaptive_half_width,
    build_strategies,
    capital_plan,
)
from run_cycle import _build_dust_swap_signal, _build_open_signal


class WalletCapitalTests(unittest.TestCase):
    def test_summarize_wallet_idle_usdc_and_dust(self):
        data = {
            "wallet": "W1",
            "total_usd": 200.0,
            "assets": [
                {"mint": USDC_MINT, "amount_raw": "15000000", "amount_ui": 15.0,
                 "decimals": 6, "price_usd": 1.0, "total_value_usd": 15.0,
                 "is_native_sol": False},
                {"mint": "DUSTMINT", "symbol": "DUST", "amount_raw": "2000000000",
                 "amount_ui": 2.0, "decimals": 9, "price_usd": 2.0,
                 "total_value_usd": 4.0, "is_native_sol": False},
                {"mint": SOL_MINT, "symbol": "SOL", "amount_raw": "10000000",
                 "amount_ui": 0.01, "decimals": 9, "price_usd": 120.0,
                 "total_value_usd": 1.2, "is_native_sol": True},
                {"mint": "SMALL", "symbol": "SML", "amount_raw": "1",
                 "amount_ui": 1e-9, "decimals": 9, "price_usd": 1.0,
                 "total_value_usd": 0.5, "is_native_sol": False},
            ],
        }
        summary = summarize_wallet(data)
        self.assertEqual(summary["idle_usdc"], 15.0)
        self.assertEqual(summary["dust_total_usdc"], 4.0)
        self.assertEqual(summary["reserved_total_usdc"], 1.2)
        self.assertEqual(summary["deployable_usdc"], 19.0)
        self.assertEqual(len(summary["dust_assets"]), 1)
        self.assertEqual(summary["dust_assets"][0]["mint"], "DUSTMINT")

    def test_custom_reserved_mint_never_dust(self):
        reserved = list(RESERVED_MINTS - {SOL_MINT})[0]
        data = {
            "wallet": "W1",
            "total_usd": 100.0,
            "assets": [
                {"mint": reserved, "symbol": "RSV", "amount_raw": "1000000000",
                 "amount_ui": 1.0, "decimals": 9, "price_usd": 10.0,
                 "total_value_usd": 10.0, "is_native_sol": False},
            ],
        }
        summary = summarize_wallet(data)
        self.assertEqual(summary["dust_total_usdc"], 0.0)
        self.assertEqual(summary["reserved_total_usdc"], 10.0)

    def test_usdc_not_dust(self):
        data = {
            "wallet": "W1",
            "total_usd": 20.0,
            "assets": [
                {"mint": USDC_MINT, "amount_raw": "20000000", "amount_ui": 20.0,
                 "decimals": 6, "price_usd": 1.0, "total_value_usd": 20.0,
                 "is_native_sol": False},
            ],
        }
        summary = summarize_wallet(data)
        self.assertEqual(summary["dust_assets"], [])
        self.assertEqual(summary["idle_usdc"], 20.0)


class CapitalPlanTests(unittest.TestCase):
    def test_open_eligible_idle_and_suggested_both_clear(self):
        wallet = {"idle_usdc": 100.0, "deployable_usdc": 200.0}
        plan = capital_plan(wallet)
        self.assertTrue(plan["open_eligible"])
        self.assertEqual(plan["suggested_position_usdc"], 50.0)

    def test_open_not_eligible_low_idle(self):
        wallet = {"idle_usdc": 10.0, "deployable_usdc": 200.0}
        self.assertFalse(capital_plan(wallet)["open_eligible"])

    def test_open_not_eligible_low_deployable(self):
        wallet = {"idle_usdc": 100.0, "deployable_usdc": 40.0}
        plan = capital_plan(wallet)
        self.assertFalse(plan["open_eligible"])
        # suggested_position_usd should still be min(deployable*0.25, 100)
        self.assertEqual(plan["suggested_position_usdc"], 10.0)

    def test_suggested_capped_at_max(self):
        wallet = {"idle_usdc": 1000.0, "deployable_usdc": 1000.0}
        plan = capital_plan(wallet)
        self.assertEqual(plan["suggested_position_usdc"], DEFAULT_MAX_POSITION_USD)


class AdaptiveRangeTests(unittest.TestCase):
    def test_meteora_low_volatility(self):
        pool = {"dex": "meteora", "volatility": 1.77, "bin_step": 4, "tick_spacing": 4}
        half = adaptive_half_width(pool)
        self.assertGreaterEqual(half, 10)
        # With small vol and bin_step 4, half-width should be modest.
        self.assertLessEqual(half, 1000)

    def test_meteora_one_bps(self):
        pool = {"dex": "meteora", "volatility": 1.87, "bin_step": 1, "tick_spacing": 1}
        half = adaptive_half_width(pool)
        self.assertGreaterEqual(half, 10)

    def test_raydium_tick_spacing(self):
        pool = {"dex": "raydium", "volatility": 2.0, "bin_step": 0, "tick_spacing": 1}
        half = adaptive_half_width(pool)
        self.assertGreaterEqual(half, 10)

    def test_zero_volatility_defaults_to_min(self):
        pool = {"dex": "meteora", "volatility": 0.0, "bin_step": 4, "tick_spacing": 4}
        self.assertEqual(adaptive_half_width(pool), 10)

    def test_extreme_volatility_clamped(self):
        pool = {"dex": "meteora", "volatility": 100.0, "bin_step": 1, "tick_spacing": 1}
        self.assertEqual(adaptive_half_width(pool), 1000)


class StrategyTests(unittest.TestCase):
    def _pool(self, score: float, volatility: float, bin_step: int,
              active_bin_id: int = 0, dex: str = "meteora"):
        return {
            "pool_address": f"p{score}",
            "name": "SOL-USDC",
            "dex": dex,
            "volatility": volatility,
            "bin_step": bin_step,
            "tick_spacing": bin_step,
            "active_bin_id": active_bin_id,
            "token_x_decimals": 9,
            "token_y_decimals": 6,
            "token_x_price_usd": 120.0,
            "token_y_price_usd": 1.0,
        }

    def _candidate(self, score: float, pool: dict) -> dict:
        return {
            "action": "OPEN_CANDIDATE",
            "pool": pool["name"],
            "pool_address": pool["pool_address"],
            "dex": pool["dex"],
            "pair_class": "stable_bluechip",
            "score": score,
            "evidence": {},
            "reason": "test",
            "_pool": pool,
        }

    def test_build_strategies_caps_at_three(self):
        wallet = {"idle_usdc": 100.0, "deployable_usdc": 100.0}
        candidates = [self._candidate(float(i), self._pool(float(i), 1.0, 4)) for i in range(5)]
        strategies = build_strategies(candidates, wallet)
        self.assertEqual(len(strategies), 3)
        # Highest scores should be selected.
        self.assertEqual(strategies[0]["score"], 4.0)
        self.assertEqual(strategies[1]["score"], 3.0)

    def test_build_strategies_returns_empty_when_not_eligible(self):
        wallet = {"idle_usdc": 5.0, "deployable_usdc": 5.0}
        candidates = [self._candidate(90.0, self._pool(90.0, 1.0, 4))]
        self.assertEqual(build_strategies(candidates, wallet), [])

    def test_strategy_sizing_is_25_percent(self):
        wallet = {"idle_usdc": 100.0, "deployable_usdc": 200.0}
        candidate = self._candidate(90.0, self._pool(90.0, 1.0, 4))
        strategies = build_strategies([candidate], wallet)
        self.assertEqual(strategies[0]["suggested_usdc"], 50.0)

    def test_strategy_range_centered(self):
        wallet = {"idle_usdc": 100.0, "deployable_usdc": 100.0}
        candidate = self._candidate(90.0, self._pool(90.0, 1.0, 4, active_bin_id=-100))
        strategies = build_strategies([candidate], wallet)
        s = strategies[0]
        half = s["half_width"]
        self.assertEqual(s["center"], -100)
        self.assertEqual(s["bin_range"]["lower"], -100 - half)
        self.assertEqual(s["bin_range"]["upper"], -100 + half)


class SignalTests(unittest.TestCase):
    def _strategy(self):
        return {
            "suggested_usdc": 50.0,
            "center": -5299,
            "half_width": 23,
            "bin_range": {"lower": -5322, "upper": -5276},
        }

    def _verdict(self):
        return {
            "action": "OPEN_CANDIDATE",
            "pool": "SOL-USDC",
            "pool_address": "p1",
            "dex": "meteora",
            "score": 90.0,
            "evidence": {},
            "_pool": {
                "pool_address": "p1",
                "token_x_decimals": 9,
                "token_y_decimals": 6,
                "token_x_price_usd": 120.0,
                "token_y_price_usd": 1.0,
            },
        }

    def test_build_open_signal(self):
        signal = _build_open_signal(self._strategy(), self._verdict(), 1, 12345)
        self.assertEqual(signal["action"], "open")
        self.assertEqual(signal["dex"], "meteora")
        self.assertEqual(signal["bin_range"]["lower"], -5322)
        self.assertEqual(signal["bin_range"]["upper"], -5276)
        self.assertIn("amount_x", signal["liquidity"])
        self.assertIn("amount_y", signal["liquidity"])
        # 50/50 split of $50 => $25 each side.
        self.assertEqual(signal["liquidity"]["amount_x"], str(int(25 / 120 * 1e9)))
        self.assertEqual(signal["liquidity"]["amount_y"], str(int(25 / 1 * 1e6)))

    def test_build_dust_swap_signal(self):
        asset = {
            "mint": "DUST",
            "symbol": "DUST",
            "decimals": 9,
            "amount_raw": "1000000000",
            "amount_ui": 1.0,
            "value_usd": 5.0,
        }
        signal = _build_dust_swap_signal(asset, 1, 12345)
        self.assertEqual(signal["action"], "swap_to_usdc")
        self.assertEqual(signal["mint"], "DUST")
        self.assertEqual(signal["value_usd"], 5.0)


class RunCycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.pools_dir = os.path.join(self.root, "pools")
        self.pos_dir = os.path.join(self.root, "positions")
        self.wallet_dir = os.path.join(self.root, "wallet_scans")
        for d in (self.pools_dir, self.pos_dir, self.wallet_dir):
            os.makedirs(d)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _touch(self, path):
        now = time.time()
        os.utime(path, (now, now))

    def _make_pool(self):
        return {
            "name": "SOL-USDC", "pool_address": "p2", "dex": "meteora",
            "token_x_symbol": "SOL", "token_y_symbol": "USDC",
            "tvl": 2_000_000.0, "volume_window": 20_000_000.0,
            "realized_fee_apr": 150.0, "volatility": 8.0,
            "token_x_price_usd": 117.0, "token_y_price_usd": 1.0,
            "token_x_decimals": 9, "token_y_decimals": 6,
            "bin_step": 4, "tick_spacing": 4,
            "active_bin_id": -5299,
        }

    def test_wallet_and_strategies_in_report(self):
        pool = self._make_pool()
        with open(os.path.join(self.pools_dir, "pool_scan-x.json"), "w") as fh:
            json.dump([pool], fh)
        self._touch(os.path.join(self.pools_dir, "pool_scan-x.json"))

        with open(os.path.join(self.pos_dir, "position_scan-x.json"), "w") as fh:
            json.dump({"positions": []}, fh)
        self._touch(os.path.join(self.pos_dir, "position_scan-x.json"))

        wallet_scan = {
            "wallet": "W1",
            "total_usd": 200.0,
            "assets": [
                {"mint": USDC_MINT, "amount_raw": "100000000", "amount_ui": 100.0,
                 "decimals": 6, "price_usd": 1.0, "total_value_usd": 100.0,
                 "is_native_sol": False},
                {"mint": "DUST", "symbol": "DUST", "amount_raw": "1000000000",
                 "amount_ui": 1.0, "decimals": 9, "price_usd": 20.0,
                 "total_value_usd": 20.0, "is_native_sol": False},
            ],
        }
        with open(os.path.join(self.wallet_dir, "wallet_scan-x.json"), "w") as fh:
            json.dump(wallet_scan, fh)
        self._touch(os.path.join(self.wallet_dir, "wallet_scan-x.json"))

        report = lp_scoring.run_cycle(
            self.pools_dir, self.pos_dir, self.wallet_dir, max_age_seconds=3900.0
        )

        self.assertEqual(report["failures"], [])
        self.assertIn("wallet", report)
        self.assertEqual(report["wallet"]["idle_usdc"], 100.0)
        self.assertEqual(report["wallet"]["dust_total_usdc"], 20.0)
        self.assertEqual(report["wallet"]["deployable_usdc"], 120.0)
        # 25% of deployable = 30, idle > 15 => eligible.

    def test_write_signals_creates_dust_and_open(self):
        pool = self._make_pool()
        with open(os.path.join(self.pools_dir, "pool_scan-x.json"), "w") as fh:
            json.dump([pool], fh)
        self._touch(os.path.join(self.pools_dir, "pool_scan-x.json"))

        with open(os.path.join(self.pos_dir, "position_scan-x.json"), "w") as fh:
            json.dump({"positions": []}, fh)
        self._touch(os.path.join(self.pos_dir, "position_scan-x.json"))

        wallet_scan = {
            "wallet": "W1",
            "total_usd": 200.0,
            "assets": [
                {"mint": USDC_MINT, "amount_raw": "100000000", "amount_ui": 100.0,
                 "decimals": 6, "price_usd": 1.0, "total_value_usd": 100.0,
                 "is_native_sol": False},
                {"mint": "DUST", "symbol": "DUST", "amount_raw": "1000000000",
                 "amount_ui": 1.0, "decimals": 9, "price_usd": 20.0,
                 "total_value_usd": 20.0, "is_native_sol": False},
            ],
        }
        with open(os.path.join(self.wallet_dir, "wallet_scan-x.json"), "w") as fh:
            json.dump(wallet_scan, fh)
        self._touch(os.path.join(self.wallet_dir, "wallet_scan-x.json"))

        report = lp_scoring.run_cycle(
            self.pools_dir, self.pos_dir, self.wallet_dir, max_age_seconds=3900.0
        )
        report["capital_plan"] = capital_plan(report["wallet"])
        open_candidates = [v for v in report["verdicts"]
                           if v.get("action") == "OPEN_CANDIDATE"]
        report["strategies"] = build_strategies(open_candidates, report["wallet"])

        signals_dir = os.path.join(self.root, "signals")
        created, review = write_signals(report, signals_dir, self.pos_dir)

        actions = []
        for path in created:
            with open(path, "r", encoding="utf-8") as fh:
                signal = json.load(fh)
            actions.append(signal["action"])

        self.assertIn("swap_to_usdc", actions)
        self.assertIn("open", actions)
        self.assertEqual(actions.count("swap_to_usdc"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
