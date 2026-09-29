#!/usr/bin/env python3
"""Unit tests for the dynamic scoring layer."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dynamic
import lp_scoring


class PercentileTests(unittest.TestCase):
    def test_rank_empty(self):
        self.assertEqual(dynamic.percentile_rank([], 5.0), 0.5)

    def test_rank_basic(self):
        vals = sorted([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(dynamic.percentile_rank(vals, 0.0), 0.0)
        self.assertEqual(dynamic.percentile_rank(vals, 4.0), 1.0)
        self.assertEqual(dynamic.percentile_rank(vals, 2.5), 0.5)

    def test_norms_min_samples(self):
        scans = [
            [{"name": "SOL-USDC", "realized_fee_apr": 10.0, "volume_window": 100, "tvl": 1000}],
        ]
        norms = dynamic.build_norms(scans, ["fee_yield"], min_samples=5)
        self.assertEqual(norms, {})


class RegimeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = lp_scoring.get_config()

    def test_neutral_regime(self):
        prior = [
            [{"name": "SOL-USDC", "realized_fee_apr": 50.0, "volatility": 5.0,
              "token_x_symbol": "SOL", "token_y_symbol": "USDC"}],
        ]
        current = [{"name": "SOL-USDC", "realized_fee_apr": 55.0, "volatility": 5.2,
                    "token_x_symbol": "SOL", "token_y_symbol": "USDC"}]
        regime = dynamic.detect_regime(prior, current, self.cfg)
        self.assertEqual(regime["name"], "neutral")

    def test_fee_boom(self):
        prior = [
            [{"name": "SOL-USDC", "realized_fee_apr": 50.0, "volatility": 5.0,
              "token_x_symbol": "SOL", "token_y_symbol": "USDC"}],
        ]
        current = [{"name": "SOL-USDC", "realized_fee_apr": 200.0, "volatility": 5.0,
                    "token_x_symbol": "SOL", "token_y_symbol": "USDC"}]
        regime = dynamic.detect_regime(prior, current, self.cfg)
        self.assertEqual(regime["name"], "fee_boom")

    def test_weight_shift(self):
        base = {"fee_yield": 30.0, "turnover": 20.0, "depth": 15.0,
                "volatility_fit": 35.0}
        shifted = dynamic.regime_weight_adjust(base, "high_vol", self.cfg)
        self.assertAlmostEqual(sum(shifted.values()), 100.0, places=2)
        self.assertGreater(shifted["volatility_fit"], base["volatility_fit"])
        self.assertLess(shifted["fee_yield"], base["fee_yield"])


class AdaptiveThresholdsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = lp_scoring.get_config()

    def test_too_few_returns_none(self):
        scores = [10.0, 20.0, 30.0]
        self.assertIsNone(dynamic.adaptive_pool_thresholds(scores, self.cfg))

    def test_thresholds_respect_order(self):
        scores = list(range(100))
        thr = dynamic.adaptive_pool_thresholds(scores, self.cfg)
        self.assertIsNotNone(thr)
        self.assertLessEqual(thr["watch"], thr["open"])
        self.assertTrue(55.0 <= thr["open"] <= 90.0)


class ExpectedPnLTests(unittest.TestCase):
    def setUp(self):
        self.cfg = lp_scoring.get_config()

    def test_healthy_position_holds(self):
        pos = {
            "lower_price": 100.0, "upper_price": 120.0, "current_price": 110.0,
            "current_value_usd": 1000.0, "lower_bound": -50, "upper_bound": 50,
        }
        pool = {"realized_fee_apr": 200.0, "volatility": 5.0}
        pnl = dynamic.expected_pnl_verdict(pos, pool, self.cfg)
        self.assertIsNotNone(pnl)
        self.assertEqual(pnl["action"], "HOLD")
        self.assertTrue(pnl["decisive"])

    def test_out_of_range_closes(self):
        pos = {
            "lower_price": 100.0, "upper_price": 120.0, "current_price": 200.0,
            "current_value_usd": 1000.0, "lower_bound": -50, "upper_bound": 50,
        }
        # Low fee + high vol makes holding a losing out-of-range position.
        pool = {"realized_fee_apr": 5.0, "volatility": 30.0}
        pnl = dynamic.expected_pnl_verdict(pos, pool, self.cfg)
        self.assertIsNotNone(pnl)
        self.assertEqual(pnl["action"], "CLOSE")

    def test_missing_data_returns_none(self):
        pos = {"current_value_usd": 1000.0}
        pool = {"realized_fee_apr": 100.0, "volatility": 5.0}
        self.assertIsNone(dynamic.expected_pnl_verdict(pos, pool, self.cfg))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self._orig = lp_scoring.get_config()

    def tearDown(self):
        lp_scoring.set_config(self._orig)

    def test_pool_score_changes_with_regime(self):
        cfg = json.loads(json.dumps(self._orig))
        cfg["dynamic"]["regime"]["enabled"] = True
        cfg["dynamic"]["regime"]["history_scans"] = 1
        # Force a fee_boom regime by raising fee_boom threshold and lowering history.
        cfg["dynamic"]["regime"]["fee_boom_apr_pct"] = 5.0
        cfg["dynamic"]["position_pnl"]["enabled"] = False
        lp_scoring.set_config(cfg)

        prior = [[{"name": "SOL-USDC", "realized_fee_apr": 1.0, "volatility": 1.0,
                   "token_x_symbol": "SOL", "token_y_symbol": "USDC"}]]
        current = [{"name": "SOL-USDC", "realized_fee_apr": 200.0, "volatility": 1.0,
                    "token_x_symbol": "SOL", "token_y_symbol": "USDC"}]
        ctx = dynamic.build_context_from_prior(current, prior, cfg)
        self.assertEqual(ctx["regime"]["name"], "fee_boom")
        # fee_yield weight should drop relative to neutral.
        self.assertLess(ctx["weights"]["stable_bluechip"]["fee_yield"],
                        cfg["pool_profiles"]["stable_bluechip"]["weights"]["fee_yield"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
