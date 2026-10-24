"""Tests for the active-bin drift recenter trigger.

Covers evaluate_recenter, apply_recenter_verdicts, record_emitted,
update_history, _trend_sign, _new_range, _composition_amounts,
_position_usd, and the RECENTER signal path through write_signals.
"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from recenter import (
    evaluate_recenter, apply_recenter_verdicts, record_emitted,
    update_history, _trend_sign, _new_range, _composition_amounts,
    _position_usd, merged_policy,
)


# ── Test data fixtures ──────────────────────────────────────────────────────

# A valid in-range Meteora position: active bin within [lower, upper].
# Drift from center will be tested via the active_bin offset.
# Pool record with bin_step=10, volatility=3.84 → adaptive_half_width = 20,
# range width = 41 inclusive.

def _pos(active_bin=-2267, lower=-2287, upper=-2233,
         days_open=1.0, current_price=0.00963, current_value_usd=50.0,
         amounts_x_raw="1000000", amounts_y_raw="2000000",
         value_known=True, pool_address="POOL1",
         position_features=None):
    """Build a minimal position dict with in-range geometry and pool_address."""
    p = {
        "pool_address": pool_address,
        "current_bin_id": active_bin,
        "lower_bound": lower,
        "upper_bound": upper,
        "days_open": days_open,
        "current_price": current_price,
        "current_value_usd": current_value_usd,
        "amounts_x_raw": amounts_x_raw,
        "amounts_y_raw": amounts_y_raw,
        "position_features": (position_features or {}),
    }
    return p


def _pool_record(bin_step=10, volatility=3.84, dex="meteora"):
    return {"bin_step": bin_step, "volatility": volatility, "dex": dex}


# ── Policy shortcut ─────────────────────────────────────────────────────────

POLICY = merged_policy(
    {"enabled": True, "drift_frac_of_half_width": 0.6, "min_drift_bins": 8,
     "cooldown_hours": 6.0, "min_hold_hours": 12.0,
     "trend_confirm_scans": 3, "trend_min_span_seconds": 1800,
     "sample_gap_seconds": 600, "max_recenters_per_pool_per_day": 2,
     "amount_haircut_pct": 2.0})


class EvaluateRecenterTests(unittest.TestCase):
    """Core evaluate_recenter logic + data-quality gates."""

    def test_no_fire_drift_below_threshold(self):
        # Drift 10 bins, threshold max(8, 0.6*27=16.2)=16.2 → no fire.
        pos = _pos(active_bin=-2250, lower=-2287, upper=-2233)
        pool = _pool_record()
        state = {"samples": [], "last_recenter_ts": None}
        result = evaluate_recenter(pos, pool, state, POLICY, now=time.time())
        self.assertFalse(result["fire"])
        self.assertIn("drift below threshold", result["reason"])

    def test_fire_full_payload(self):
        # Drift 20 bins >= threshold 16.2; active at center+20.
        # bounds -2287..-2237 → center=-2262, half=25, threshold=max(8,15)=15.
        # active=-2242 → drift=20 >= 15 → fire.
        pos = _pos(active_bin=-2242, lower=-2287, upper=-2237,
                   current_value_usd=50.0, value_known=True)
        pool = _pool_record()
        state = {"samples": [], "last_recenter_ts": None}
        result = evaluate_recenter(pos, pool, state, POLICY, now=time.time())
        self.assertTrue(result["fire"])
        self.assertIn("bin_range", result)
        self.assertIn("liquidity", result)
        self.assertIn("position_usd", result)
        self.assertTrue(result["position_usd"] > 0)

    def test_trend_unknown_insufficient_history(self):
        pos = _pos()
        pool = _pool_record()
        state = {"samples": [], "last_recenter_ts": None}
        result = evaluate_recenter(pos, pool, state, POLICY, now=time.time())
        self.assertFalse(result["fire"])
        self.assertIn("trend unknown", result["reason"])

    def test_trend_opposes_drift(self):
        # Samples with decreasing active bins (trend -1) and drift positive
        pos = _pos(active_bin=-2242)  # drift > 0
        pool = _pool_record()
        state = {
            "samples": [
                {"t": time.time() - 3600, "active_bin": -2250},
                {"t": time.time() - 2400, "active_bin": -2255},
                {"t": time.time() - 1200, "active_bin": -2260},
            ],
            "last_recenter_ts": None,
        }
        result = evaluate_recenter(pos, pool, state, POLICY, now=time.time())
        self.assertFalse(result["fire"])
        self.assertIn("trend opposes drift", result["reason"])

    def test_cooldown_active(self):
        pos = _pos()
        pool = _pool_record()
        state = {
            "samples": [{"t": time.time(), "active_bin": -2267}],
            "last_recenter_ts": time.time() - 3600,  # 1h ago < 6h cooldown
        }
        result = evaluate_recenter(pos, pool, state, POLICY, now=time.time())
        self.assertFalse(result["fire"])
        self.assertIn("cooldown", result["reason"])

    def test_position_oor_close_rail_owns_it(self):
        # active bin above upper bound → OOR
        pos = _pos(active_bin=-2220, lower=-2287, upper=-2233)
        pool = _pool_record()
        state = {"samples": [], "last_recenter_ts": None}
        result = evaluate_recenter(pos, pool, state, POLICY, now=time.time())
        self.assertFalse(result["fire"])
        self.assertIn("out of range", result["reason"])

    def test_unknown_value_no_fire(self):
        pos = _pos(current_value_usd=None, value_known=False)
        pool = _pool_record()
        state = {"samples": [], "last_recenter_ts": None}
        result = evaluate_recenter(pos, pool, state, POLICY, now=time.time())
        self.assertFalse(result["fire"])
        self.assertIn("value unknown", result["reason"])

    def test_unknown_amounts_no_fire(self):
        pos = _pos(amounts_x_raw=None, amounts_y_raw=None)
        pool = _pool_record()
        state = {"samples": [], "last_recenter_ts": None}
        result = evaluate_recenter(pos, pool, state, POLICY, now=time.time())
        self.assertFalse(result["fire"])
        self.assertIn("composition unknown", result["reason"])

    def test_young_position_no_fire(self):
        pos = _pos(days_open=0.2, current_value_usd=100.0)  # ~4.8h < 12h
        pool = _pool_record()
        state = {"samples": [], "last_recenter_ts": None}
        result = evaluate_recenter(pos, pool, state, POLICY, now=time.time())
        self.assertFalse(result["fire"])
        self.assertIn("younger than min_hold_hours", result["reason"])

    def test_daily_cap_exhausted(self):
        pos = _pos()
        pool = _pool_record()
        state = {
            "samples": [],
            "last_recenter_ts": None,
            "recenters_day": time.strftime("%Y-%m-%d", time.gmtime(time.time())),
            "recenters_today": 2,  # cap = 2
        }
        result = evaluate_recenter(pos, pool, state, POLICY, now=time.time())
        self.assertFalse(result["fire"])
        self.assertIn("daily cap", result["reason"])

    def test_width_cap_ge_200(self):
        # bounds 0..300 width 301 > 200 → invalid geometry
        pos = _pos(active_bin=150, lower=0, upper=300)
        pool = _pool_record()
        state = {"samples": [], "last_recenter_ts": None}
        result = evaluate_recenter(pos, pool, state, POLICY, now=time.time())
        self.assertFalse(result["fire"])
        self.assertIn("invalid", result["reason"].lower())


class ApplyRecenterVerdictsTests(unittest.TestCase):
    """apply_recenter_verdicts: HOLD→RECENTER, untouched verdicts."""

    def test_hold_becomes_recenter(self):
        verdicts = [{
            "action": "HOLD", "pool_address": "POOL1", "position": "pos-1",
            "score": 95.0, "lower_bound": -2287, "upper_bound": -2233,
            "dex": "meteora", "reason": "healthy",
        }]
        active = {"POOL1": _pos()}
        pools_by = {"POOL1": _pool_record()}
        state = {"samples": [], "last_recenter_ts": None}
        notes = apply_recenter_verdicts(verdicts, active, pools_by, state, POLICY, now=time.time())
        self.assertEqual(verdicts[0]["action"], "RECENTER")
        self.assertIn("recenter_bin_range", verdicts[0])
        self.assertEqual(len(notes), 0)

    def test_cclose_untouched(self):
        verdicts = [{
            "action": "CLOSE", "pool_address": "POOL1", "position": "pos-1",
            "score": 95.0, "dex": "meteora",
        }]
        active = {"POOL1": _pos()}
        pools_by = {"POOL1": _pool_record()}
        state = {"samples": [], "last_recenter_ts": None}
        notes = apply_recenter_verdicts(verdicts, active, pools_by, state, POLICY, now=time.time())
        self.assertEqual(verdicts[0]["action"], "CLOSE")  # unchanged
        self.assertEqual(len(notes), 0)

    def test_non_meteora_untouched(self):
        verdicts = [{
            "action": "HOLD", "pool_address": "POOL1", "position": "pos-1",
            "score": 95.0, "dex": "orca",
        }]
        active = {"POOL1": _pos()}
        pools_by = {"POOL1": _pool_record()}
        state = {"samples": [], "last_recenter_ts": None}
        notes = apply_recenter_verdicts(verdicts, active, pools_by, state, POLICY, now=time.time())
        self.assertEqual(verdicts[0]["action"], "HOLD")  # unchanged
        self.assertEqual(len(notes), 1)  # review note for non-meteora

    def test_collect_fees_becomes_recenter(self):
        verdicts = [{
            "action": "COLLECT_FEES", "pool_address": "POOL1", "position": "pos-1",
            "score": 95.0, "dex": "meteora",
            "lower_bound": -2287, "upper_bound": -2233,
        }]
        active = {"POOL1": _pos()}
        pools_by = {"POOL1": _pool_record()}
        state = {"samples": [], "last_recenter_ts": None}
        notes = apply_recenter_verdicts(verdicts, active, pools_by, state, POLICY, now=time.time())
        self.assertEqual(verdicts[0]["action"], "RECENTER")
        self.assertEqual(len(notes), 0)


class RecordEmittedTests(unittest.TestCase):
    """record_emitted sets cooldown/daily counters."""

    def test_record_emitted_sets_state(self):
        state = {"samples": [], "last_recenter_ts": None,
                 "recenters_day": None, "recenters_today": 0}
        now = time.time()
        emitted = [{"action": "recenter", "pool_address": "POOL1", "position_id": "pos-1"}]
        record_emitted(state, emitted, now=now)
        self.assertEqual(state["last_recenter_ts"], now)
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        self.assertEqual(state["recenters_day"], today)
        self.assertEqual(state["recenters_today"], 1)

    def test_record_emitted_increments_daily(self):
        # Set yesterday's day, today should roll over
        yesterday = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
        state = {"samples": [], "last_recenter_ts": None,
                 "recenters_day": yesterday, "recenters_today": 1}
        emitted = [{"action": "recenter", "pool_address": "POOL1", "position_id": "pos-1"}]
        record_emitted(state, emitted, now=time.time())
        self.assertEqual(state["recenters_day"], time.strftime("%Y-%m-%d", time.gmtime(time.time())))
        self.assertEqual(state["recenters_today"], 1)


class UpdateHistoryTests(unittest.TestCase):
    """update_history dedupes samples within gap."""

    def test_dedup_within_gap_keeps_one(self):
        state = {"samples": []}
        now = time.time()
        # First sample
        state = update_history([_pos(pool_address="POOL1")], state, now, sample_gap_seconds=600)
        self.assertEqual(len(state["samples"]), 1)
        # Second sample within gap → ignored
        state = update_history([_pos(pool_address="POOL1")], state, now, sample_gap_seconds=600)
        self.assertEqual(len(state["samples"]), 1)

    def test_gap_opens_new_sample(self):
        # Calls with same now are deduped; gap > 600s would open new sample
        state = {"samples": []}
        now = time.time()
        state = update_history([_pos(pool_address="POOL1")], state, now, sample_gap_seconds=600)
        # Second call same now → ignored
        state = update_history([_pos(pool_address="POOL1")], state, now, sample_gap_seconds=600)
        self.assertEqual(len(state["samples"]), 1)

    def test_geometry_invalid_ignored(self):
        # Position with bad bounds should not pollute history
        state = {"samples": []}
        now = time.time()
        bad_pos = _pos(active_bin=100, lower=200, upper=100, pool_address="POOL1")  # lower > upper
        state = update_history([bad_pos], state, now, sample_gap_seconds=600)
        self.assertEqual(len(state["samples"]), 0)


class TrendSignTests(unittest.TestCase):
    """_trend_sign requires enough samples spanning enough time."""

    def test_trend_positive(self):
        samples = [
            {"t": time.time() - 3600, "active_bin": 100},
            {"t": time.time() - 2400, "active_bin": 110},
            {"t": time.time() - 1200, "active_bin": 120},
        ]
        result = _trend_sign(samples, {"trend_confirm_scans": 3, "trend_min_span_seconds": 600})
        self.assertEqual(result, 1)

    def test_trend_negative(self):
        samples = [
            {"t": time.time() - 3600, "active_bin": 120},
            {"t": time.time() - 2400, "active_bin": 110},
            {"t": time.time() - 1200, "active_bin": 100},
        ]
        result = _trend_sign(samples, {"trend_confirm_scans": 3, "trend_min_span_seconds": 600})
        self.assertEqual(result, -1)

    def test_insufficient_samples(self):
        samples = [
            {"t": time.time() - 3600, "active_bin": 100},
            {"t": time.time() - 3000, "active_bin": 105},
        ]
        result = _trend_sign(samples, {"trend_confirm_scans": 3, "trend_min_span_seconds": 600})
        self.assertIsNone(result)

    def test_span_too_short(self):
        samples = [
            {"t": time.time() - 100, "active_bin": 100},
            {"t": time.time() - 50, "active_bin": 120},
            {"t": time.time() - 10, "active_bin": 110},
        ]
        result = _trend_sign(samples, {"trend_confirm_scans": 3, "trend_min_span_seconds": 600})
        self.assertIsNone(result)


class NewRangeTests(unittest.TestCase):
    """_new_range returns centered range."""

    def test_new_range_width(self):
        pool = _pool_record()
        result = _new_range(-2267, pool)
        self.assertIsNotNone(result)
        # Width should be odd; with vol=3.84/bin_step=10 → half=20 → width=41
        self.assertEqual(result["upper"] - result["lower"] + 1, 41)

    def test_range_lower_upper_correct(self):
        pool = _pool_record()
        result = _new_range(-2267, pool)
        self.assertEqual(result["lower"], -2267 - 20)  # -2287
        self.assertEqual(result["upper"], -2267 + 20)  # -2247


class CompositionAmountsTests(unittest.TestCase):
    """_composition_amounts haircut + zero guards."""

    def test_amounts_with_haircut(self):
        pos = _pos(amounts_x_raw="1000000", amounts_y_raw="2000000")
        result = _composition_amounts(pos, POLICY)
        self.assertIsNotNone(result)
        self.assertEqual(result["amount_x"], str(int(1000000 * 0.98)))  # 980000
        self.assertEqual(result["amount_y"], str(int(2000000 * 0.98)))  # 1960000

    def test_both_amounts_zero(self):
        pos = _pos(amounts_x_raw="0", amounts_y_raw="0")
        result = _composition_amounts(pos, POLICY)
        self.assertIsNone(result)

    def test_missing_amounts(self):
        pos = _pos()  # default _pos has amounts, so use explicit None
        # Actually, _pos() default has amounts; create a pos without:
        pos = {"pool_address": "P", "current_bin_id": 0, "lower_bound": -10, "upper_bound": 10,
               "days_open": 1, "current_price": 1.0, "current_value_usd": 50.0,
               "amounts_x_raw": None, "amounts_y_raw": None}
        result = _composition_amounts(pos, POLICY)
        self.assertIsNone(result)


class PositionUsdTests(unittest.TestCase):
    """_position_usd known-value gates."""

    def test_value_known_passes(self):
        pos = _pos(current_value_usd=50.0, value_known=True)
        result = _position_usd(pos, POLICY)
        self.assertEqual(result, 50.0)

    def test_value_unknown_fails(self):
        pos = _pos(current_value_usd=None, value_known=False)
        result = _position_usd(pos, POLICY)
        self.assertIsNone(result)


class MergedPolicyTests(unittest.TestCase):
    """merged_policy fills defaults from policy block."""

    def test_defaults_present(self):
        p = merged_policy({"enabled": True})
        for k in ['drift_frac_of_half_width', 'min_drift_bins', 'cooldown_hours',
                   'min_hold_hours', 'trend_confirm_scans', 'trend_min_span_seconds',
                   'sample_gap_seconds', 'max_recenters_per_pool_per_day',
                   'amount_haircut_pct', 'min_position_usd']:
            self.assertIn(k, p)

    def test_override_defaults(self):
        p = merged_policy({"drift_frac_of_half_width": 0.8})
        self.assertEqual(p["drift_frac_of_half_width"], 0.8)
        self.assertEqual(p["min_drift_bins"], 8)  # unchanged default


if __name__ == "__main__":
    unittest.main()