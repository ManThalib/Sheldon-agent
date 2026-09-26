#!/usr/bin/env python3
"""Unit tests for Sheldon's LP scoring engine (stdlib unittest).

Run:  python3 test_lp_scoring.py -v
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lp_scoring
from lp_scoring import (
    ConfigError,
    classify_pair,
    estimate_expected_fees,
    estimate_il_pct,
    load_config,
    run_cycle,
    score_pool,
    score_position,
    score_pool_depeg_safety,
    score_pool_depth,
    score_pool_fee_yield,
    score_pool_turnover,
    score_pool_volatility_fit,
    score_position_fee_capture,
    score_position_range_status,
    score_position_staleness,
    set_config,
    _fees_usd_or_none,
    DEFAULT_CONFIG,
)


def _cfg():
    return lp_scoring.get_config()


def _pos_thresholds():
    c = _cfg()
    return (float(c["position_thresholds"]["close"]),
            float(c["position_thresholds"]["review"]))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._orig = lp_scoring._CONFIG

    def tearDown(self):
        lp_scoring._CONFIG = self._orig

    def test_profiles_json_valid_and_loaded(self):
        path = os.path.join(os.path.dirname(lp_scoring.__file__), "profiles.json")
        cfg = load_config(path)
        self.assertEqual(cfg["pool_thresholds"]["open"], 70.0)
        self.assertEqual(cfg["position_thresholds"]["close"], 40.0)

    def test_missing_file_uses_defaults(self):
        cfg = load_config("/nonexistent/profiles.json")
        self.assertEqual(cfg, DEFAULT_CONFIG)

    def test_bad_weight_sum_raises(self):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["pool_profiles"]["stable_stable"]["weights"]["fee_yield"] = 45.0
        with self.assertRaises(ConfigError):
            set_config(cfg)

    def test_bad_threshold_order_raises(self):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["pool_thresholds"] = {"open": 50.0, "watch": 70.0}
        with self.assertRaises(ConfigError):
            set_config(cfg)

    def test_override_merge(self):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["position_thresholds"] = {"close": 30.0, "review": 55.0}
        set_config(cfg)
        self.assertEqual(_pos_thresholds(), (30.0, 55.0))
        # Untouched values keep defaults.
        self.assertEqual(_cfg()["pool_thresholds"]["open"], 70.0)


class ClassifyTests(unittest.TestCase):
    def test_stable_stable(self):
        pc, _, _ = classify_pair({"token_x_symbol": "USDC", "token_y_symbol": "USDT"})
        self.assertEqual(pc, "stable_stable")

    def test_stable_bluechip(self):
        pc, _, _ = classify_pair({"token_x_symbol": "SOL", "token_y_symbol": "USDC"})
        self.assertEqual(pc, "stable_bluechip")

    def test_bluechip_bluechip(self):
        pc, _, _ = classify_pair({"token_x_symbol": "SOL", "token_y_symbol": "WBTC"})
        self.assertEqual(pc, "bluechip_bluechip")

    def test_off_universe(self):
        pc, _, _ = classify_pair({"token_x_symbol": "BONK", "token_y_symbol": "USDC"})
        self.assertEqual(pc, "off_universe")

    def test_unknown(self):
        pc, _, _ = classify_pair({"name": ""})
        self.assertEqual(pc, "unknown")

    def test_name_fallback(self):
        pc, x, y = classify_pair({"name": "SOL-USDC (bin 4)"})
        self.assertEqual(pc, "stable_bluechip")
        self.assertEqual((x, y), ("SOL", "USDC"))


class PoolComponentTests(unittest.TestCase):
    def test_fee_yield_cap(self):
        self.assertEqual(score_pool_fee_yield({"realized_fee_apr": 600}, 30.0, 300.0), 30.0)
        self.assertEqual(score_pool_fee_yield({"realized_fee_apr": 150}, 30.0, 300.0), 15.0)
        self.assertEqual(score_pool_fee_yield({"realized_fee_apr": 0}, 30.0, 300.0), 0.0)

    def test_turnover(self):
        self.assertEqual(score_pool_turnover({"tvl": 0, "volume_window": 100}, 20.0), 0.0)
        # turnover_max (5x) over the window caps the component.
        self.assertEqual(score_pool_turnover({"tvl": 100, "volume_window": 500}, 20.0), 20.0)
        self.assertEqual(score_pool_turnover({"tvl": 100, "volume_window": 250}, 20.0), 10.0)

    def test_depth_log_scale(self):
        self.assertEqual(score_pool_depth({"tvl": 10_000}, 15.0), 0.0)
        self.assertEqual(score_pool_depth({"tvl": 5_000_000}, 15.0), 15.0)
        mid = score_pool_depth({"tvl": 500_000}, 15.0)
        self.assertTrue(0 < mid < 15.0)

    def test_volatility_triangular(self):
        self.assertEqual(score_pool_volatility_fit({"volatility": 8.0}, 20.0, 8.0), 20.0)
        self.assertEqual(score_pool_volatility_fit({"volatility": 4.0}, 20.0, 8.0), 10.0)
        self.assertEqual(score_pool_volatility_fit({"volatility": 24.0}, 20.0, 8.0), 0.0)
        self.assertEqual(score_pool_volatility_fit({"volatility": 0}, 20.0, 8.0), 0.0)

    def test_depeg_safety(self):
        pool = {"token_x_price_usd": 1.0, "token_y_price_usd": 0.99}
        # 1% from $1 is fully depegged at 0.5% zero-distance.
        self.assertEqual(score_pool_depeg_safety(pool, 25.0, "USDC", "USDT"), 0.0)
        pool = {"token_x_price_usd": 1.0, "token_y_price_usd": 1.0}
        self.assertEqual(score_pool_depeg_safety(pool, 25.0, "USDC", "USDT"), 25.0)
        # Unknown stable price -> half credit.
        self.assertEqual(score_pool_depeg_safety({"token_x_price_usd": 1.0},
                                                 25.0, "USDC", "USDT"), 12.5)
        # No stable side -> full credit (not applicable).
        self.assertEqual(score_pool_depeg_safety({}, 35.0, "SOL", "WBTC"), 35.0)


class PoolScoreTests(unittest.TestCase):
    def test_off_universe_ignored(self):
        result = score_pool({"name": "BONK-USDC", "pool_address": "p1"})
        self.assertEqual(result["verdict"], "IGNORE")
        self.assertEqual(result["score"], 0.0)

    def test_strong_pool_opens(self):
        pool = {
            "name": "SOL-USDC", "pool_address": "p2", "dex": "meteora",
            "token_x_symbol": "SOL", "token_y_symbol": "USDC",
            "tvl": 2_000_000.0, "volume_window": 20_000_000.0,
            "realized_fee_apr": 150.0, "volatility": 8.0,
            "token_x_price_usd": 117.0, "token_y_price_usd": 1.0,
        }
        result = score_pool(pool)
        self.assertEqual(result["verdict"], "OPEN_CANDIDATE")
        self.assertGreaterEqual(result["score"], 70.0)
        self.assertAlmostEqual(sum(result["components"].values()), result["score"], places=2)

    def test_weights_sum_to_100(self):
        for section in ("pool_profiles", "position_profiles"):
            for cls, prof in _cfg()[section].items():
                self.assertAlmostEqual(sum(prof["weights"].values()), 100.0,
                                       places=2, msg=f"{section}.{cls}")


class PositionComponentTests(unittest.TestCase):
    def test_range_status_in_range_full(self):
        pos = {"lower_price": 100.0, "upper_price": 120.0, "current_price": 110.0}
        pts, known = score_position_range_status(pos, 30.0)
        self.assertEqual(pts, 30.0)
        self.assertTrue(known)

    def test_range_status_out_of_range_zero(self):
        pos = {"lower_price": 100.0, "upper_price": 120.0, "current_price": 130.0}
        pts, known = score_position_range_status(pos, 30.0)
        self.assertEqual((pts, known), (0.0, True))

    def test_range_status_unknown(self):
        pts, known = score_position_range_status({}, 30.0)
        self.assertEqual(pts, 15.0)  # unknown_credit 0.5
        self.assertFalse(known)

    def test_in_range_flag_only(self):
        pts, known = score_position_range_status({"in_range": True}, 30.0)
        self.assertEqual((pts, known), (30.0, True))
        pts, known = score_position_range_status({"in_range": False}, 30.0)
        self.assertEqual((pts, known), (0.0, True))

    def test_staleness_grace(self):
        pts, known = score_position_staleness({"days_open": 10}, 20.0)
        self.assertEqual((pts, known), (20.0, True))
        pts, known = score_position_staleness({"days_open": None}, 20.0)
        self.assertEqual(pts, 10.0)
        self.assertFalse(known)

    def test_staleness_erosion(self):
        # 75 days is halfway between grace (30) and zero (120).
        pts, _ = score_position_staleness({"days_open": 75}, 20.0)
        self.assertAlmostEqual(pts, 10.0, places=2)

    def test_fees_sentinel_unknown(self):
        pos = {"fees_usd": 0.0, "fees_owed_raw": [18446744073709551615, 0]}
        self.assertIsNone(_fees_usd_or_none(pos))

    def test_fees_normal(self):
        pos = {"fees_usd": 3.5, "fees_owed_raw": [1000, 2000]}
        self.assertEqual(_fees_usd_or_none(pos), 3.5)

    def test_fee_capture_estimated(self):
        # value=1000, apr=150%, 1 day -> expected ~= 1000*1.5/365 = 4.11
        pos = {"fees_usd": 4.11, "current_value_usd": 1000.0, "days_open": 1.0}
        pool = {"realized_fee_apr": 150.0}
        pts, known = score_position_fee_capture(pos, 20.0, pool)
        self.assertTrue(known)
        self.assertAlmostEqual(pts, 20.0, places=1)

    def test_estimate_expected_fees_math(self):
        pos = {"current_value_usd": 1000.0, "days_open": 10.0}
        pool = {"realized_fee_apr": 365.0}  # 1%/day = $10/day on $1000
        expected, source = estimate_expected_fees(pos, pool, True)
        self.assertEqual(source, "estimated")
        self.assertAlmostEqual(expected, 1000.0 * 3.65 * 10 / 365.0, places=6)

    def test_estimate_expected_fees_capped_days(self):
        pos = {"current_value_usd": 36500.0, "days_open": 100.0}
        pool = {"realized_fee_apr": 100.0}
        expected, _ = estimate_expected_fees(pos, pool, True)
        # Capped at fee_expect_max_days (30): 36500 * 1.0 * 30/365 = 3000.
        self.assertAlmostEqual(expected, 3000.0, places=6)

    def test_estimate_il_from_volatility(self):
        pos = {"lower_bound": -100, "upper_bound": 100, "days_open": 1.0}
        pool = {"volatility": 5.0}  # 5% daily
        il, source = estimate_il_pct(pos, pool, 1.0)
        self.assertEqual(source, "estimated")
        # conc = clamp(2000/200, 1, 4) = 4; il = 4 * (0.05)^2 / 8 * 100 * 1 = 0.125
        self.assertAlmostEqual(il, 0.125, places=4)

    def test_estimate_il_reported_wins(self):
        il, source = estimate_il_pct({"il_estimate_pct": 3.0}, {"volatility": 8}, 5)
        self.assertEqual((il, source), (3.0, "reported"))


class PositionVerdictTests(unittest.TestCase):
    def _base_pos(self, **over):
        pos = {
            "position_id": "pos1", "pool_address": "p2",
            "token_x_symbol": "SOL", "token_y_symbol": "USDC",
            "lower_price": 100.0, "upper_price": 120.0, "current_price": 110.0,
            "fees_usd": 1.0, "fees_owed_raw": [500, 500],
            "current_value_usd": 500.0, "days_open": 5.0,
        }
        pos.update(over)
        return pos

    def _pool(self):
        return {"name": "SOL-USDC", "pool_address": "p2",
                "token_x_symbol": "SOL", "token_y_symbol": "USDC",
                "realized_fee_apr": 100.0, "volatility": 8.0}

    def test_healthy_position_holds(self):
        result = score_position(self._base_pos(), {"p2": self._pool()})
        self.assertEqual(result["verdict"], "HOLD")
        self.assertEqual(result["data_quality"]["unknown_components"], [])

    def test_bad_position_with_full_data_closes(self):
        # Out of range (range_status 0), no fees (fee_capture 0), stale.
        pos = self._base_pos(current_price=200.0, fees_usd=0.0, days_open=130.0)
        result = score_position(pos, {"p2": self._pool()})
        self.assertEqual(result["verdict"], "CLOSE")
        self.assertEqual(result["data_quality"]["unknown_components"], [])

    def test_missing_data_never_closes(self):
        # Same bad position but fees unknown -> REVIEW, not CLOSE.
        pos = self._base_pos(current_price=200.0, fees_usd=0.0,
                             fees_owed_raw=[18446744073709551615, 0],
                             days_open=130.0)
        result = score_position(pos, {"p2": self._pool()})
        self.assertEqual(result["verdict"], "REVIEW")
        self.assertIn("fee_capture", result["data_quality"]["unknown_components"])

    def test_missing_range_data_never_closes(self):
        pos = self._base_pos()
        del pos["lower_price"], pos["upper_price"], pos["current_price"]
        pos["fees_usd"] = 0.0
        pos["days_open"] = 130.0
        result = score_position(pos, {"p2": self._pool()})
        self.assertEqual(result["verdict"], "REVIEW")
        self.assertIn("range_status", result["data_quality"]["unknown_components"])

    def test_policy_close_off_universe(self):
        pos = {"position_id": "x", "pool_address": "p9",
               "token_x_symbol": "BONK", "token_y_symbol": "USDC"}
        result = score_position(pos, {})
        self.assertEqual(result["verdict"], "CLOSE")
        self.assertTrue(result["data_quality"]["policy_close"])

    def test_collect_fees(self):
        # fees 8 on value 500 -> 8 >= max(5, 1%*500=5) -> collect.
        pos = self._base_pos(fees_usd=8.0)
        result = score_position(pos, {"p2": self._pool()})
        self.assertTrue(result["collect_fees"])

    def test_no_collect_on_unknown_fees(self):
        pos = self._base_pos(fees_usd=0.0,
                             fees_owed_raw=[18446744073709551615, 0])
        result = score_position(pos, {"p2": self._pool()})
        self.assertFalse(result["collect_fees"])

    def test_stable_stable_depeg_component(self):
        pos = {
            "position_id": "s1", "pool_address": "p3",
            "token_x_symbol": "USDC", "token_y_symbol": "USDT",
            "lower_price": 0.99, "upper_price": 1.01, "current_price": 1.0,
            "fees_usd": 1.0, "fees_owed_raw": [100, 100],
            "current_value_usd": 100.0, "days_open": 2.0,
        }
        pool = {"name": "USDC-USDT", "pool_address": "p3",
                "token_x_price_usd": 1.0, "token_y_price_usd": 0.9999}
        result = score_position(pos, {"p3": pool})
        self.assertIn("depeg_exposure", result["components"])
        self.assertNotIn("il_risk", result["components"])


class RunCycleTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.pools_dir = os.path.join(self.root, "pools")
        self.pos_dir = os.path.join(self.root, "positions")
        os.makedirs(self.pools_dir)
        os.makedirs(self.pos_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _touch(self, path):
        now = time.time()
        os.utime(path, (now, now))

    def test_stale_data_fails(self):
        report = run_cycle(self.pools_dir, self.pos_dir, max_age_seconds=0.0)
        self.assertTrue(report["failures"])
        self.assertIn("pool data missing or stale", report["failures"][0])

    def test_end_to_end(self):
        pool = {
            "name": "SOL-USDC", "pool_address": "p2", "dex": "meteora",
            "token_x_symbol": "SOL", "token_y_symbol": "USDC",
            "tvl": 2_000_000.0, "volume_window": 20_000_000.0,
            "realized_fee_apr": 150.0, "volatility": 8.0,
            "token_x_price_usd": 117.0, "token_y_price_usd": 1.0,
        }
        pool_path = os.path.join(self.pools_dir, "pool_scan-x.json")
        with open(pool_path, "w") as fh:
            json.dump([pool], fh)
        self._touch(pool_path)

        pos = {
            "position_id": "pos1", "pool_address": "p2", "dex": "meteora",
            "token_x_symbol": "SOL", "token_y_symbol": "USDC",
            "lower_price": 100.0, "upper_price": 120.0, "current_price": 110.0,
            "fees_usd": 1.0, "fees_owed_raw": [500, 500],
            "current_value_usd": 500.0, "days_open": 5.0,
        }
        pos_path = os.path.join(self.pos_dir, "position_scan-x.json")
        with open(pos_path, "w") as fh:
            json.dump({"positions": [pos]}, fh)
        self._touch(pos_path)

        report = run_cycle(self.pools_dir, self.pos_dir)
        self.assertEqual(report["failures"], [])
        self.assertEqual(len(report["pool_scores"]), 1)
        self.assertEqual(len(report["position_scores"]), 1)
        self.assertEqual(report["position_scores"][0]["verdict"], "HOLD")

    def test_end_to_end_close_with_full_data(self):
        pool = {
            "name": "SOL-USDC", "pool_address": "p2", "dex": "meteora",
            "token_x_symbol": "SOL", "token_y_symbol": "USDC",
            "tvl": 100_000.0, "volume_window": 10_000.0,
            "realized_fee_apr": 5.0, "volatility": 30.0,
        }
        pool_path = os.path.join(self.pools_dir, "pool_scan-x.json")
        with open(pool_path, "w") as fh:
            json.dump([pool], fh)
        self._touch(pool_path)

        pos = {
            "position_id": "pos1", "pool_address": "p2", "dex": "meteora",
            "token_x_symbol": "SOL", "token_y_symbol": "USDC",
            "lower_price": 100.0, "upper_price": 120.0, "current_price": 200.0,
            "fees_usd": 0.0, "fees_owed_raw": [0, 0],
            "current_value_usd": 10.0, "days_open": 130.0,
        }
        pos_path = os.path.join(self.pos_dir, "position_scan-x.json")
        with open(pos_path, "w") as fh:
            json.dump({"positions": [pos]}, fh)
        self._touch(pos_path)

        report = run_cycle(self.pools_dir, self.pos_dir)
        self.assertEqual(report["failures"], [])
        ps = report["position_scores"][0]
        self.assertEqual(ps["verdict"], "CLOSE")
        self.assertEqual(ps["data_quality"]["unknown_components"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
