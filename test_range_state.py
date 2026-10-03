#!/usr/bin/env python3
"""Tests for the out-of-range grace rail.

Policy: a position that leaves its range survives TWO consecutive runs
before Sheldon emits a close. Run 1 => HOLD with review reason, no close
signal. Run 2 => CLOSE/REBALANCE as before. Back in range resets.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import range_state
from run_cycle import write_signals


def _oor_position(addr="POOL1", oor=True):
    """A raw position record, optionally out of range (price below range)."""
    if oor:
        return {"pool_address": addr, "position_id": f"pos-{addr}",
                "status": "active", "dex": "meteora",
                "lower_price": 100.0, "upper_price": 110.0,
                "current_price": 95.0, "in_range": False}
    return {"pool_address": addr, "position_id": f"pos-{addr}",
            "status": "active", "dex": "meteora",
            "lower_price": 100.0, "upper_price": 110.0,
            "current_price": 105.0, "in_range": True}


def _close_verdict(addr="POOL1", action="CLOSE", key="action"):
    v = {"pool": "SOL-USDC", "pool_address": addr, "position": f"pos-{addr}",
         "dex": "meteora", "score": 30.0,
         "lower_bound": -100, "upper_bound": 100,
         "reason": "score 30.0 < close threshold 40.0",
         "note": "score 30.0 < close threshold 40.0"}
    v[key] = action
    return v


class OutOfRangeDetectionTests(unittest.TestCase):
    def test_price_bounds_out_of_range(self):
        self.assertTrue(range_state.position_out_of_range(_oor_position()))
        self.assertFalse(range_state.position_out_of_range(_oor_position(oor=False)))

    def test_current_price_on_boundary_is_in_range(self):
        pos = _oor_position(oor=False)
        pos["current_price"] = 100.0  # exactly on the lower bound
        self.assertFalse(range_state.position_out_of_range(pos))

    def test_in_range_flag_fallback(self):
        # A verifiable out-of-range flag carries decoded bounds; a bare
        # false flag (no bounds fields) is a fallback record shape and
        # treated like unknown data below.
        self.assertTrue(range_state.position_out_of_range(
            {"in_range": False, "lower_bound": -100, "upper_bound": 100}))
        self.assertFalse(range_state.position_out_of_range(
            {"in_range": True, "lower_bound": -100, "upper_bound": 100}))

    def test_null_bounds_flag_false_never_counts_as_out_of_range(self):
        """RPC-fallback record: null tick bounds + in_range=false is not
        verifiable out-of-range evidence; grace counter must not start
        (2026-10-03 false REBALANCE)."""
        self.assertFalse(range_state.position_out_of_range(
            {"in_range": False, "lower_bound": None, "upper_bound": None}))
        self.assertFalse(range_state.position_out_of_range(
            {"in_range": False, "lower_bound": None, "upper_bound": 100}))

    def test_unknown_data_never_counts_as_out_of_range(self):
        self.assertFalse(range_state.position_out_of_range({}))
        self.assertFalse(range_state.position_out_of_range(
            {"lower_price": 1.0, "upper_price": "junk", "current_price": 0.5}))


class CounterTests(unittest.TestCase):
    def test_first_oor_run_counts_one_and_second_counts_two(self):
        counts = {}
        counts = range_state.update_out_of_range_counts([_oor_position()], counts)
        self.assertEqual(counts, {"POOL1": 1})
        counts = range_state.update_out_of_range_counts([_oor_position()], counts)
        self.assertEqual(counts, {"POOL1": 2})

    def test_back_in_range_resets_counter(self):
        counts = {"POOL1": 2}
        counts = range_state.update_out_of_range_counts(
            [_oor_position(oor=False)], counts)
        self.assertEqual(counts, {})
        # A fresh out-of-range excursion starts over at 1.
        counts = range_state.update_out_of_range_counts([_oor_position()], counts)
        self.assertEqual(counts, {"POOL1": 1})

    def test_untracked_position_is_dropped(self):
        counts = {"GONE": 3}
        counts = range_state.update_out_of_range_counts([_oor_position()], counts)
        self.assertNotIn("GONE", counts)
        self.assertIn("POOL1", counts)

    def test_state_file_roundtrip_and_junk_recovery(self):
        root = tempfile.mkdtemp()
        try:
            range_state.save_range_state({"POOL1": 2}, root)
            self.assertEqual(range_state.load_range_state(root), {"POOL1": 2})
            with open(range_state.state_path(root), "w") as fh:
                fh.write("{not json")
            self.assertEqual(range_state.load_range_state(root), {})
            self.assertEqual(range_state.load_range_state(
                os.path.join(root, "missing")), {})
        finally:
            shutil.rmtree(root, ignore_errors=True)


class GraceVerdictTests(unittest.TestCase):
    def test_first_oor_run_defers_close_to_hold(self):
        counts = {"POOL1": 1}
        out = range_state.out_of_range_position_verdict(
            _close_verdict(), counts)
        self.assertEqual(out["action"], "HOLD")
        self.assertIn("grace", out["reason"])
        self.assertIn("1/2", out["reason"])

    def test_second_oor_run_allows_close(self):
        counts = {"POOL1": 2}
        out = range_state.out_of_range_position_verdict(
            _close_verdict(), counts)
        self.assertEqual(out["action"], "CLOSE")

    def test_second_oor_run_allows_rebalance(self):
        counts = {"POOL1": 2}
        out = range_state.out_of_range_position_verdict(
            _close_verdict(action="REBALANCE"), counts)
        self.assertEqual(out["action"], "REBALANCE")

    def test_in_range_close_passes_immediately(self):
        # Policy close on an in-range position: no counter, no deferral.
        out = range_state.out_of_range_position_verdict(
            _close_verdict(), {})
        self.assertEqual(out["action"], "CLOSE")

    def test_hold_verdict_never_touched(self):
        counts = {"POOL1": 1}
        v = _close_verdict(action="HOLD")
        out = range_state.out_of_range_position_verdict(v, counts)
        self.assertEqual(out["action"], "HOLD")

    def test_score_dict_with_verdict_key_supported(self):
        s = _close_verdict(key="verdict")
        out = range_state.out_of_range_position_verdict(s, {"POOL1": 1})
        self.assertEqual(out["verdict"], "HOLD")
        out = range_state.out_of_range_position_verdict(s, {"POOL1": 2})
        self.assertEqual(out["verdict"], "CLOSE")

    def test_original_dict_not_mutated(self):
        v = _close_verdict()
        range_state.out_of_range_position_verdict(v, {"POOL1": 1})
        self.assertEqual(v["action"], "CLOSE")


class RunCycleGraceIntegrationTests(unittest.TestCase):
    """End-to-end over write_signals: the deferred run writes no close."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.signals_dir = os.path.join(self.root, "signals")
        self.state_dir = os.path.join(self.root, "state")
        os.makedirs(self.signals_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _report(self, addr="POOL1"):
        return {
            "wallet": {},
            "verdicts": [_close_verdict(addr)],
            "open_skipped": [],
        }

    def test_run1_no_close_signal_run2_close_signal(self):
        counts = {}
        counts = range_state.update_out_of_range_counts([_oor_position()], counts)
        report = self._report()
        report["verdicts"] = [
            range_state.out_of_range_position_verdict(v, counts)
            for v in report["verdicts"]]
        created, _review = write_signals(report, self.signals_dir, self.root)
        actions = []
        for path in created:
            with open(path) as fh:
                actions.append(json.load(fh)["action"])
        self.assertNotIn("close", actions)

        # Second consecutive out-of-range run: close allowed.
        counts = range_state.update_out_of_range_counts([_oor_position()], counts)
        report = self._report()
        report["verdicts"] = [
            range_state.out_of_range_position_verdict(v, counts)
            for v in report["verdicts"]]
        created, _review = write_signals(report, self.signals_dir, self.root)
        actions = [json.load(open(p))["action"] for p in created]
        self.assertIn("close", actions)

    def test_state_survives_between_runs_via_file(self):
        counts = range_state.update_out_of_range_counts(
            [_oor_position()], range_state.load_range_state(self.state_dir))
        range_state.save_range_state(counts, self.state_dir)
        counts = range_state.update_out_of_range_counts(
            [_oor_position()], range_state.load_range_state(self.state_dir))
        self.assertEqual(counts, {"POOL1": 2})
        report = self._report()
        report["verdicts"] = [
            range_state.out_of_range_position_verdict(v, counts)
            for v in report["verdicts"]]
        created, _review = write_signals(report, self.signals_dir, self.root)
        actions = [json.load(open(p))["action"] for p in created]
        self.assertIn("close", actions)

    def test_back_in_range_then_oor_defers_again(self):
        counts = {"POOL1": 2}
        counts = range_state.update_out_of_range_counts(
            [_oor_position(oor=False)], counts)
        counts = range_state.update_out_of_range_counts([_oor_position()], counts)
        report = self._report()
        report["verdicts"] = [
            range_state.out_of_range_position_verdict(v, counts)
            for v in report["verdicts"]]
        self.assertEqual(report["verdicts"][0]["action"], "HOLD")


if __name__ == "__main__":
    unittest.main(verbosity=2)
