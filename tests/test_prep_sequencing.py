#!/usr/bin/env python3
"""Capital prep must run AFTER the sizing gate, not before it.

Regression target (live 2026-10-24): ``plan_funding`` runs in run_cycle
before the readiness gate and before the capital split, so ``write_signals``
wrote every allowed prep swap with no check that the prep's target survived
the gates that run later. Result: 3 prep BUY legs + 1 surplus SELL paid
swap costs for an open whose suggested size ($14.77) never cleared the $20
floor, leaving $19.69 USDC stranded against $83.87 unusable SOL.

Contract pinned here:
  - a prep is emitted only when its target pool has an OPEN_CANDIDATE that
    passes every open gate this cycle (in strategies, supported dex,
    score floor, center + bin_range present, suggested_usdc >= floor);
  - a prep whose target fails any sizing gate becomes a review item with
    reason "prep deferred: ..." instead of a chain transaction.

Hermetic: pure dict inputs, tmp signal dirs, no RPC, no signing.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capital import SOL_MINT  # noqa: E402
from run_cycle.signals import write_signals  # noqa: E402


class PrepFollowsSizingGate(unittest.TestCase):
    """Prep emission must follow the same gates the open signal follows."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prep-seq-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.signals_dir = os.path.join(self.tmp, "signals")
        self.positions_dir = os.path.join(self.tmp, "positions")
        os.makedirs(self.signals_dir)
        os.makedirs(self.positions_dir)

    def _report(self, suggested, score=96.0, in_strategies=True):
        strategies = [{
            "pool_address": "POOL1", "score": score,
            "suggested_usdc": suggested, "center": -2300,
            "bin_range": {"lower": -2320, "upper": -2280},
        }] if in_strategies else []
        return {
            "wallet": {},
            "verdicts": [{
                "action": "OPEN_CANDIDATE", "pool_address": "POOL1",
                "dex": "meteora", "score": score,
                "evidence": {"fee_yield": 25.0},
            }],
            "strategies": strategies,
            "funding": {"prep_swaps_allowed": [{
                "direction": "buy", "for_pool": "POOL1", "symbol": "SOL",
                "input_mint": SOL_MINT, "output_mint": SOL_MINT,
                "amount_raw": "1000", "usd": 8.93,
                "need_ui": 0.12, "have_ui": 0.0}],
                "prep_swaps_blocked": []},
        }

    def test_prep_emitted_when_candidate_passes_sizing(self):
        created, _review = write_signals(
            self._report(24.0), signals_dir=self.signals_dir,
            positions_dir=self.positions_dir,
            min_open_score=90.0, supported_dexes={"meteora"})
        self.assertEqual(len(created), 1)

    def test_prep_deferred_when_size_below_floor(self):
        # The live 2026-10-24 shape: suggested $14.77 < $20 floor.
        created, review = write_signals(
            self._report(14.77), signals_dir=self.signals_dir,
            positions_dir=self.positions_dir,
            min_open_score=90.0, supported_dexes={"meteora"})
        self.assertEqual(created, [])
        self.assertTrue(
            any("prep deferred" in (r.get("reason") or "") for r in review),
            f"expected a deferred-prep review item, got {review}")

    def test_prep_deferred_when_pool_dropped_from_strategies(self):
        # Capital split / readiness dropped it after plan_funding ran.
        created, review = write_signals(
            self._report(24.0, in_strategies=False),
            signals_dir=self.signals_dir, positions_dir=self.positions_dir,
            min_open_score=90.0, supported_dexes={"meteora"})
        self.assertEqual(created, [])
        self.assertTrue(
            any("prep deferred" in (r.get("reason") or "") for r in review),
            f"expected a deferred-prep review item, got {review}")

    def test_prep_deferred_when_score_below_floor(self):
        # Sized pool but the verdict score is under min_open_score: the open
        # cannot be built this cycle, so the prep must not pay for it.
        created, review = write_signals(
            self._report(24.0, score=80.0), signals_dir=self.signals_dir,
            positions_dir=self.positions_dir,
            min_open_score=90.0, supported_dexes={"meteora"})
        self.assertEqual(created, [])
        self.assertTrue(
            any("prep deferred" in (r.get("reason") or "") for r in review),
            f"expected a deferred-prep review item, got {review}")

    def test_no_prep_items_when_funding_has_no_allowed_preps(self):
        report = self._report(24.0)
        report["funding"]["prep_swaps_allowed"] = []
        created, review = write_signals(
            report, signals_dir=self.signals_dir,
            positions_dir=self.positions_dir,
            min_open_score=90.0, supported_dexes={"meteora"})
        self.assertEqual(created, [])
        self.assertEqual(
            [r for r in review if "prep deferred" in (r.get("reason") or "")],
            [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
