#!/usr/bin/env python3
"""A close-family verdict carrying only pool-rubric evidence must never
become a signal.

Regression target (live 2026-10-24, signal sheldon-1792856108-2): close
cycles emitted TWO close signals for the same position — the real
close-rubric verdict ("CLOSE score=57.89" with range_status/fee_capture/
staleness/depeg_exposure/il_risk) and an artifact ("CLOSE score=94.17"
carrying pool-rubric evidence only: fee_yield/depth/turnover/
volatility_fit, the shape the OPEN path uses). The artifact is the one
George executed, closing a position the real rubric scored at HOLD.

Contract pinned here:
  - a close-family verdict (CLOSE/REBALANCE/ROTATE/RECENTER) whose evidence
    has neither a position-rubric component nor OOR provenance is demoted to
    a review item, never written as a signal;
  - a position-rubric close passes unchanged;
  - an OOR-promoted close passes because range_state stamps oor_side into
    the evidence, and that provenance survives into the signal reason;
  - COLLECT_FEES never carries a rubric, so it is outside the check.

Hermetic: pure dict inputs, tmp signal dirs, no RPC, no signing.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from range_state import out_of_range_position_verdict  # noqa: E402
from run_cycle.signals import write_signals  # noqa: E402


class OpenRubricCloseArtifact(unittest.TestCase):
    """Close-family verdicts must be evidenced by the position rubric or by
    stamped OOR provenance."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="close-art-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.signals_dir = os.path.join(self.tmp, "signals")
        self.positions_dir = os.path.join(self.tmp, "positions")
        os.makedirs(self.signals_dir)
        os.makedirs(self.positions_dir)

    def _report(self, evidence, action="CLOSE", oor=False):
        ev = dict(evidence)
        if oor:
            ev["oor_side"] = "right"
        return {"verdicts": [{
            "action": action, "pool_address": "POOL1", "dex": "meteora",
            "position": "POS1", "score": 94.17, "evidence": ev,
            "lower_bound": 100, "upper_bound": 200}]}

    def _write(self, report):
        return write_signals(
            report, signals_dir=self.signals_dir,
            positions_dir=self.positions_dir,
            min_open_score=90.0, supported_dexes={"meteora"})

    def test_pool_rubric_close_is_demoted(self):
        created, review = self._write(self._report({
            "fee_yield": 24.9, "depth": 25.0,
            "turnover": 24.7, "volatility_fit": 19.5}))
        self.assertEqual(created, [])
        self.assertTrue(
            any("open-rubric close artifact" in (r.get("reason") or "")
                for r in review),
            f"expected a demoted-artifact review item, got {review}")

    def test_position_rubric_close_passes(self):
        created, _review = self._write(self._report({
            "range_status": 0.0, "fee_capture": 26.3,
            "staleness": 0.0, "depeg_exposure": 0.0, "il_risk": 31.5}))
        self.assertEqual(len(created), 1)

    def test_oor_promoted_close_passes_via_provenance(self):
        created, _review = self._write(
            self._report({"fee_yield": 24.9}, oor=True))
        self.assertEqual(len(created), 1)
        with open(created[0], "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertIn("oor_side", payload["reason"])  # provenance survives

    def test_other_close_family_actions_are_checked(self):
        # REBALANCE / ROTATE / RECENTER share the rail: none may reach
        # George on pool-rubric evidence alone.
        for action in ("REBALANCE", "ROTATE", "RECENTER"):
            created, review = self._write(self._report(
                {"fee_yield": 24.9, "depth": 25.0}, action=action))
            self.assertEqual(created, [], f"{action} must be demoted")
            self.assertTrue(
                any("open-rubric close artifact" in (r.get("reason") or "")
                    for r in review),
                f"{action} must produce a review item")


class OorEvidenceProvenance(unittest.TestCase):
    """range_state stamps OOR markers into evidence on promotion."""

    def test_right_side_promotion_stamps_evidence(self):
        verdict = {"verdict": "HOLD", "pool_address": "POOL1",
                   "score": 94.17,
                   "evidence": {"fee_yield": 24.9, "depth": 25.0}}
        state = {"POOL1": {"side": "right", "consecutive_runs": 1}}
        out = out_of_range_position_verdict(verdict, state)
        self.assertEqual(out["verdict"], "CLOSE")
        self.assertEqual(out["evidence"]["oor_side"], "right")
        self.assertEqual(out["evidence"]["oor_consecutive_runs"], 1)

    def test_left_side_deferral_stamps_evidence(self):
        verdict = {"verdict": "CLOSE", "pool_address": "POOL1",
                   "score": 57.89,
                   "evidence": {"range_status": 0.0, "fee_capture": 26.32}}
        state = {"POOL1": {"side": "left", "consecutive_runs": 1}}
        out = out_of_range_position_verdict(verdict, state)
        self.assertEqual(out["verdict"], "HOLD")
        self.assertEqual(out["evidence"]["oor_side"], "left")
        self.assertEqual(out["evidence"]["oor_consecutive_runs"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
