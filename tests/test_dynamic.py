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


class PositionPnlAccountingTests(unittest.TestCase):
    """CLOSE/ROTATE must credit the fees a close would actually claim, and
    ROTATE must charge the candidate's projected impermanent loss."""

    def setUp(self):
        self._orig = lp_scoring.get_config()
        cfg = json.loads(json.dumps(self._orig))
        pnl = dict((cfg.get("dynamic") or {}).get("position_pnl") or {})
        pnl.update({"enabled": True, "horizon_days": 1.0,
                    "entry_cost_bps": 50.0, "exit_cost_bps": 50.0,
                    "claim_cost_usd": 0.02, "min_pnl_margin_usd": 0.01})
        cfg.setdefault("dynamic", {})["position_pnl"] = pnl
        self.cfg = cfg
        # Out of range: no forward fees on HOLD, so CLOSE/ROTATE are the
        # only options that can win.
        self.pos = {"current_value_usd": 284.61, "days_open": 2.97,
                    "fees_usd": 5.08, "rewards_usd": 0.5,
                    "lower_price": 100.0, "upper_price": 120.0,
                    "current_price": 200.0,
                    "lower_bound": -50, "upper_bound": 50}
        self.pool = {"realized_fee_apr": 5.0, "volatility": 30.0}
        self.cand = {"realized_fee_apr": 120.0, "volatility": 1.5,
                     "lower_bound": -50, "upper_bound": 50}

    def tearDown(self):
        lp_scoring.set_config(self._orig)

    def test_close_credits_accrued_claims(self):
        pnl = dynamic.expected_pnl_verdict(self.pos, self.pool, self.cfg)
        exit_cost = 284.61 * 50.0 / 10000.0 + 0.02
        self.assertAlmostEqual(pnl["accrued_claims_usd"], 5.58, places=4)
        self.assertAlmostEqual(pnl["expected_close_usd"],
                               round(5.58 - exit_cost, 4), places=4)
        self.assertEqual(pnl["action"], "CLOSE")

    def test_rotate_credits_claims_and_charges_candidate_il(self):
        pnl = dynamic.expected_pnl_verdict(self.pos, self.pool, self.cfg,
                                           candidate_pool=self.cand)
        self.assertAlmostEqual(pnl["accrued_claims_usd"], 5.58, places=4)
        self.assertIsNotNone(pnl["il_rotate_usd"])
        self.assertGreater(pnl["il_rotate_usd"], 0.0)
        fwd_cand = 284.61 * (120.0 / 100.0) * 1.0 / 365.0
        # entry + (exit incl. the claim cost) + swap — the claim cost is
        # charged exactly once, inside exit_cost.
        costs = (284.61 * 50.0 / 10000.0 * 2
                 + 284.61 * 100.0 / 10000.0 + 0.02)
        self.assertAlmostEqual(
            pnl["expected_rotate_usd"],
            round(5.58 + fwd_cand - pnl["il_rotate_usd"] - costs, 4), places=4)

    def test_rotate_il_makes_rotation_less_attractive(self):
        volatile = dict(self.cand, volatility=25.0)
        pnl = dynamic.expected_pnl_verdict(self.pos, self.pool, self.cfg,
                                           candidate_pool=volatile)
        # Without the IL term the volatile candidate would look strictly
        # better than a quiet one with the same APR.
        quiet = dynamic.expected_pnl_verdict(
            self.pos, self.pool, self.cfg,
            candidate_pool=dict(self.cand, volatility=0.5))
        self.assertLess(pnl["expected_rotate_usd"],
                        quiet["expected_rotate_usd"])

    def test_unknown_candidate_volatility_marks_lower_confidence(self):
        cand = dict(self.cand)
        cand.pop("volatility")
        pnl = dynamic.expected_pnl_verdict(self.pos, self.pool, self.cfg,
                                           candidate_pool=cand)
        self.assertIsNotNone(pnl)
        self.assertIsNone(pnl["il_rotate_usd"])
        self.assertEqual(pnl["rotate_confidence"], "lower")
        # Current behavior preserved: no invented IL term.
        self.assertEqual(pnl["expected_rotate_usd"],
                         round(pnl["expected_rotate_usd"], 4))
        self.assertIsNotNone(pnl["expected_rotate_usd"])

    def test_hold_option_unchanged_by_claim_credit(self):
        pnl = dynamic.expected_pnl_verdict(self.pos, self.pool, self.cfg)
        # HOLD does not claim: forward fees (0 out of range) minus IL only.
        self.assertEqual(pnl["forward_fees_usd"], 0.0)
        self.assertAlmostEqual(pnl["expected_hold_usd"],
                               round(-pnl["il_hold_usd"], 4), places=4)


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
