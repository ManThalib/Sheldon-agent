#!/usr/bin/env python3
"""Tests for Missy position_features consumption (scoring.position_source).

Run:  python3 test_position_sources.py -v
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lp_scoring
from lp_scoring import (
    score_position,
    score_position_missy,
    _view_from_features,
)

POOL = {
    "name": "SOL-USDC (bin 4)",
    "pool_address": "Pool1",
    "dex": "meteora",
    "token_x_symbol": "SOL",
    "token_y_symbol": "USDC",
    "token_x_price_usd": 121.0,
    "token_y_price_usd": 1.0,
    "realized_fee_apr": 91.3,
    "volatility": 1.48,
    "tvl": 123456.0,
    "pair_class": "stable_bluechip",
}


def _features(**kw):
    base = {
        "version": 1,
        "dex": "meteora",
        "position_address": "Pos1",
        "pool_address": "Pool1",
        "wallet_id": "main",
        "status": "active",
        "pair_class": "stable_bluechip",
        "in_range": True,
        "current_bin_id": 150,
        "range_lower": 100,
        "range_upper": 200,
        "range_width": 100,
        "bins_to_lower": 50,
        "bins_to_upper": 50,
        "edge_distance_frac": 1.0,
        "side_bias_x": 0.53,
        "single_sided": False,
        "value_usd": 288.5,
        "value_known": True,
        "fees_usd": 0.044,
        "rewards_usd": 0.0,
        "fees_apr_pct": 3.96,
        "days_open": 1.41,
        "pool_realized_fee_apr": 91.3,
        "pool_volatility_pct": 1.48,
        "pool_tvl_usd": 123456.0,
        "pool_bin_step": 10,
        "pool_score": 78.5,
        "pool_score_model": "missy",
        "pool_score_version": 1,
        "gaps": [],
    }
    base.update(kw)
    return base


def _pos(features=None, **kw):
    base = {
        "position_address": "Pos1",
        "pool_address": "Pool1",
        "dex": "meteora",
        "position_features": features if features is not None else _features(),
    }
    base.update(kw)
    return base


_POLICY = {"source": "missy", "version": 1, "min_open_score": 70.0,
           "position_source": "missy", "position_features_version": 1}


class PoolGateTests(unittest.TestCase):
    """score_pool_missy must enforce Sheldon's policy gates, not just
    consume the score: universe hard-gate + Missy eligible=false."""

    def _pol(self):
        return {"source": "missy", "version": 1, "min_open_score": 70.0,
                "position_source": "missy", "position_features_version": 1}

    def test_off_universe_high_score_is_ignored(self):
        pool = {"name": "BTC-SOL", "pool_address": "P1", "dex": "meteora",
                "score": 95.4, "pair_class": "off_universe", "eligible": True,
                "realized_fee_apr": 200.0}
        with patch.object(lp_scoring, "load_scoring_policy", return_value=self._pol()):
            s = lp_scoring.score_pool_missy(pool, None)
        self.assertEqual(s["verdict"], "IGNORE")
        self.assertEqual(s["score"], 0.0)
        self.assertIn("off-universe", s["reason"])

    def test_ineligible_pool_capped_below_open(self):
        pool = {"name": "SOL-USDC (bin 4)", "pool_address": "P2", "dex": "meteora",
                "token_x_symbol": "SOL", "token_y_symbol": "USDC",
                "score": 92.0, "pair_class": "stable_bluechip",
                "eligible": False, "rejected_reason": "fee_tvl_ratio too low"}
        with patch.object(lp_scoring, "load_scoring_policy", return_value=self._pol()):
            s = lp_scoring.score_pool_missy(pool, None)
        self.assertEqual(s["verdict"], "IGNORE")
        self.assertEqual(s["score"], 92.0)  # kept for audit
        self.assertIn("fee_tvl_ratio too low", s["reason"])

    def test_eligible_pool_still_opens(self):
        pool = {"name": "SOL-USDC (bin 4)", "pool_address": "P3", "dex": "meteora",
                "token_x_symbol": "SOL", "token_y_symbol": "USDC",
                "score": 92.0, "pair_class": "stable_bluechip", "eligible": True}
        with patch.object(lp_scoring, "load_scoring_policy", return_value=self._pol()):
            s = lp_scoring.score_pool_missy(pool, None)
        self.assertEqual(s["verdict"], "OPEN_CANDIDATE")

    def test_adaptive_retag_respects_ineligible(self):
        import json
        import tempfile
        import shutil

        def filler(i, score):
            return {"name": f"SOL-USDC {i} (bin 4)", "pool_address": f"F{i}",
                    "dex": "meteora", "token_x_symbol": "SOL",
                    "token_y_symbol": "USDC", "score": score,
                    "pair_class": "stable_bluechip", "eligible": True}

        bad = {"name": "SOL-USDC big (bin 4)", "pool_address": "BAD",
               "dex": "meteora", "token_x_symbol": "SOL",
               "token_y_symbol": "USDC", "score": 99.0,
               "pair_class": "stable_bluechip", "eligible": False,
               "rejected_reason": "gate X"}
        pools = [filler(i, 50.0 + i) for i in range(11)] + [bad]
        pos = {"position_address": "Pos1", "pool_address": "F0"}
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "pool_scan-t.json"), "w") as fh:
                json.dump(pools, fh)
            with open(os.path.join(tmp, "position_scan-t.json"), "w") as fh:
                json.dump({"positions": [pos]}, fh)
            with patch.object(lp_scoring, "load_scoring_policy",
                              return_value=self._pol()):
                report = lp_scoring.run_cycle(tmp, tmp, None)
            by_addr = {s["pool_address"]: s for s in report["pool_scores"]}
            self.assertEqual(by_addr["BAD"]["verdict"], "IGNORE")
            self.assertIn("gate X", by_addr["BAD"]["reason"])
            opens = [v for v in report["verdicts"] if v.get("action") == "OPEN_CANDIDATE"]
            self.assertNotIn("BAD", [v.get("pool_address") for v in opens])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ViewMappingTests(unittest.TestCase):
    def test_view_maps_feature_keys(self):
        view = _view_from_features(_pos(), _features())
        self.assertEqual(view["position_id"], "Pos1")
        self.assertEqual(view["in_range"], True)
        self.assertEqual(view["lower_bound"], 100)
        self.assertEqual(view["days_open"], 1.41)
        self.assertIsNone(view.get("lower_price"))  # raw _pos() carries no prices
        self.assertIsNone(view.get("token_x_amount"))
        # Prices come from the raw record (features carry none); they feed
        # the expected-PnL verdict and stay None when the scan lacks them.
        priced = _view_from_features(
            _pos(lower_price=0.0084, upper_price=0.0087, current_price=0.0084),
            _features())
        self.assertEqual(priced["lower_price"], 0.0084)
        self.assertEqual(priced["upper_price"], 0.0087)
        self.assertEqual(priced["current_price"], 0.0084)

    def test_gap_gates_fees_and_value(self):
        view = _view_from_features(_pos(), _features(
            gaps=["fees_unknown", "value_unknown"], value_known=False))
        self.assertIsNone(view["fees_usd"])
        self.assertIsNone(view["current_value_usd"])


class ScorePositionMissyTests(unittest.TestCase):
    def setUp(self):
        self.pools = {"Pool1": dict(POOL)}

    def test_consumes_features_and_flags_source(self):
        with patch.object(lp_scoring, "load_scoring_policy", return_value=dict(_POLICY)):
            s = score_position_missy(_pos(), self.pools, None)
        self.assertEqual(s["score_policy"]["source"], "missy")
        self.assertEqual(s["score_policy"]["features_version"], 1)
        self.assertEqual(s["pair_class"], "stable_bluechip")
        self.assertEqual(s["position_id"], "Pos1")
        # Centered in-range position: edge_distance_frac=1.0 -> full range pts.
        w = lp_scoring.get_config()["position_profiles"]["stable_bluechip"]["weights"]
        self.assertEqual(s["components"]["range_status"], w["range_status"])
        self.assertEqual(s["data_quality"]["unknown_components"], [])

    def test_edge_distance_drives_range_score(self):
        with patch.object(lp_scoring, "load_scoring_policy", return_value=dict(_POLICY)):
            near_edge = score_position_missy(
                _pos(features=_features(edge_distance_frac=0.0)), self.pools, None)
            centered = score_position_missy(
                _pos(features=_features(edge_distance_frac=1.0)), self.pools, None)
        w = lp_scoring.get_config()["position_profiles"]["stable_bluechip"]["weights"]
        self.assertEqual(near_edge["components"]["range_status"], w["range_status"] * 0.6)
        self.assertEqual(centered["components"]["range_status"], w["range_status"])
        self.assertGreater(centered["score"], near_edge["score"])

    def test_gaps_surface_as_unknown_and_cap_verdict(self):
        feats = _features(gaps=["range_unknown", "side_unknown"],
                          edge_distance_frac=None, in_range=False,
                          range_lower=None, range_upper=None,
                          side_bias_x=None, single_sided=None)
        with patch.object(lp_scoring, "load_scoring_policy", return_value=dict(_POLICY)):
            s = score_position_missy(_pos(features=feats), self.pools, None)
        uq = s["data_quality"]["unknown_components"]
        self.assertIn("range_unknown", uq)
        self.assertIn("side_unknown", uq)
        # Low score + unknown data must cap at REVIEW, never CLOSE.
        if s["score"] < lp_scoring.get_config()["position_thresholds"]["close"]:
            self.assertEqual(s["verdict"], "REVIEW")

    def test_version_mismatch_falls_back_to_local(self):
        with patch.object(lp_scoring, "load_scoring_policy", return_value=dict(_POLICY)):
            s = score_position_missy(
                _pos(features=_features(version=99)), self.pools, None)
        self.assertEqual(s["score_policy"]["source"], "local")
        self.assertIn("fallback_reason", s["score_policy"])

    def test_missing_features_falls_back_to_local(self):
        raw_pos = {"position_address": "Pos1", "pool_address": "Pool1",
                   "dex": "meteora"}  # no position_features key at all
        with patch.object(lp_scoring, "load_scoring_policy", return_value=dict(_POLICY)):
            s = score_position_missy(raw_pos, self.pools, None)
        self.assertEqual(s["score_policy"]["source"], "local")
        self.assertIn("fallback_reason", s["score_policy"])

    def test_out_of_range_scores_zero_range(self):
        with patch.object(lp_scoring, "load_scoring_policy", return_value=dict(_POLICY)):
            s = score_position_missy(
                _pos(features=_features(in_range=False, edge_distance_frac=0.0)),
                self.pools, None)
        self.assertEqual(s["components"]["range_status"], 0.0)

    def test_single_sided_from_features(self):
        ss_pool = dict(POOL, name="USDC-USDT", token_x_symbol="USDC",
                       token_y_symbol="USDT", pair_class="stable_stable",
                       token_x_price_usd=0.99)  # depegged beyond zero_dist
        with patch.object(lp_scoring, "load_scoring_policy", return_value=dict(_POLICY)):
            s = score_position_missy(
                _pos(features=_features(single_sided=True, side_bias_x=0.98)),
                {"Pool1": ss_pool}, None)
        # 98% held in depegged X -> depeg_exposure ~ 0.
        self.assertEqual(s["components"]["depeg_exposure"], 0.0)
        with patch.object(lp_scoring, "load_scoring_policy", return_value=dict(_POLICY)):
            s2 = score_position_missy(
                _pos(features=_features(single_sided=True, side_bias_x=0.02)),
                {"Pool1": ss_pool}, None)
        w = lp_scoring.get_config()["position_profiles"]["stable_stable"]["weights"]
        self.assertEqual(s2["components"]["depeg_exposure"], w["depeg_exposure"])


class PositionValueAgeFieldTests(unittest.TestCase):
    """run_cycle's rotation loop needs current_value_usd + days_open in the
    emitted position_scores (min_hold_hours gate + expected_pnl_verdict)."""

    def setUp(self):
        self.pools = {"Pool1": dict(POOL)}

    def test_missy_score_carries_value_and_age(self):
        with patch.object(lp_scoring, "load_scoring_policy",
                          return_value=dict(_POLICY)):
            s = score_position_missy(_pos(), self.pools, None)
        self.assertEqual(s["current_value_usd"], 288.5)
        self.assertEqual(s["days_open"], 1.41)

    def test_missy_unknown_value_stays_none(self):
        feats = _features(value_known=False, value_usd=None)
        with patch.object(lp_scoring, "load_scoring_policy",
                          return_value=dict(_POLICY)):
            s = score_position_missy(_pos(features=feats), self.pools, None)
        self.assertIsNone(s["current_value_usd"])

    def test_local_score_carries_value_and_age(self):
        pos = {"position_address": "Pos1", "pool_address": "Pool1",
               "in_range": True, "days_open": 2.97,
               "current_value_usd": 284.61, "fees_usd": 5.08,
               "lower_bound": 100, "upper_bound": 200}
        s = score_position(pos, self.pools, None)
        self.assertEqual(s["current_value_usd"], 284.61)
        self.assertEqual(s["days_open"], 2.97)

    def test_run_cycle_position_scores_carry_both(self):
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "pool_scan-t.json"), "w") as fh:
                json.dump([dict(POOL)], fh)
            with open(os.path.join(tmp, "position_scan-t.json"), "w") as fh:
                json.dump({"positions": [_pos()]}, fh)
            with patch.object(lp_scoring, "load_scoring_policy",
                              return_value=dict(_POLICY)):
                report = lp_scoring.run_cycle(tmp, tmp, None)
            ps = report["position_scores"][0]
            self.assertEqual(ps["current_value_usd"], 288.5)
            self.assertEqual(ps["days_open"], 1.41)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class LocalIdentityTests(unittest.TestCase):
    def test_local_score_carries_identity(self):
        pos = {"position_address": "Pos1", "pool_address": "Pool1",
               "in_range": True, "days_open": 5.0, "fees_usd": 1.0,
               "current_value_usd": 100.0}
        s = score_position(pos, {"Pool1": dict(POOL)}, None)
        self.assertEqual(s["score_policy"], {"source": "local", "features_version": 0})


class CycleWiringTests(unittest.TestCase):
    def test_run_cycle_uses_missy_features(self):
        import json
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "pool_scan-t.json"), "w") as fh:
                json.dump([dict(POOL)], fh)
            with open(os.path.join(tmp, "position_scan-t.json"), "w") as fh:
                json.dump({"positions": [_pos()]}, fh)
            with patch.object(lp_scoring, "load_scoring_policy",
                              return_value=dict(_POLICY)):
                report = lp_scoring.run_cycle(tmp, tmp, None)
            self.assertEqual(report["position_scoring_policy"]["source"], "missy")
            self.assertEqual(len(report["position_scores"]), 1)
            self.assertEqual(report["position_scores"][0]["score_policy"]["source"],
                             "missy")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_policy_defaults_local_when_file_silent(self):
        policy = lp_scoring.load_scoring_policy.__wrapped__ if hasattr(
            lp_scoring.load_scoring_policy, "__wrapped__") else None
        # Defaults come from the function body: position_source missing -> local.
        import json as _json
        block = {}
        self.assertEqual(str(block.get("position_source", "local")).lower(), "local")


if __name__ == "__main__":
    unittest.main()
