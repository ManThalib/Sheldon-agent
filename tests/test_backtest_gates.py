#!/usr/bin/env python3
"""Tests for backtest gate-agreement report and scoring-source resolution."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest
from lp_scoring import load_scoring_policy


def _pool(addr, name="SOL-USDC", dex="orca", tvl=500000.0, volume=2000000.0,
          eligible=None, rejected_reason=None):
    p = {
        "pool_address": addr, "name": name, "dex": dex,
        "tvl": tvl, "volume_window": volume,
    }
    if eligible is not None:
        p["eligible"] = eligible
    if rejected_reason is not None:
        p["rejected_reason"] = rejected_reason
    return p


def _write_scan(path, pools):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pools, fh)
    return path


class ResolveScorerTests(unittest.TestCase):
    def test_policy_follows_manifest(self):
        scorer, source = backtest.resolve_scorer(
            type("A", (), {"scoring_source": "policy"})())
        self.assertEqual(source, load_scoring_policy()["source"])
        self.assertIn(source, ("missy", "local"))

    def test_explicit_missy_and_local(self):
        self.assertEqual(
            backtest.resolve_scorer(type("A", (), {"scoring_source": "missy"})())[1],
            "missy")
        self.assertEqual(
            backtest.resolve_scorer(type("A", (), {"scoring_source": "local"})())[1],
            "local")


class GateReportTests(unittest.TestCase):
    def test_gate_report_counts_and_verdict(self):
        root = tempfile.mkdtemp()
        try:
            legacy_scan = _write_scan(os.path.join(root, "pool_scan-20260901-000000.json"),
                                      [_pool("LEG", eligible=None)])
            phase1_scan = _write_scan(os.path.join(root, "pool_scan-20261004-000000.json"), [
                _pool("AGREE", eligible=True),                       # both pass
                _pool("STRICT", tvl=900000.0, volume=90000.0,
                      eligible=False, rejected_reason="fee_tvl_ratio low"),  # Missy stricter
                _pool("LOOSE", tvl=10.0, volume=1.0, eligible=True),  # legacy stricter
            ])
            report = backtest.run_gate_report([legacy_scan, phase1_scan])
            self.assertEqual(report["pools_total"], 4)
            self.assertEqual(report["scans_with_flag_absent"], 1)
            self.assertEqual(report["pools_with_missy_flag"], 3)
            self.assertEqual(report["agreement_count"], 1)
            self.assertEqual(report["missy_strict_count"], 1)
            self.assertEqual(report["legacy_strict_count"], 1)
            self.assertIn("fee_tvl_ratio", str(report["missy_reject_reasons"]))
            # Any legacy_strict or absent flag keeps the backstop.
            self.assertIn("keep backstop", report["backstop_verdict"])
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_backstop_redundant_when_fully_agreeing(self):
        root = tempfile.mkdtemp()
        try:
            scan = _write_scan(os.path.join(root, "pool_scan-20261004-010000.json"), [
                _pool("OK1", eligible=True),
                _pool("OK2", tvl=10.0, volume=1.0, eligible=False,
                      rejected_reason="tvl below policy"),
            ])
            report = backtest.run_gate_report([scan])
            self.assertEqual(report["legacy_strict_count"], 0)
            self.assertEqual(report["scans_with_flag_absent"], 0)
            self.assertIn("redundant", report["backstop_verdict"])
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
