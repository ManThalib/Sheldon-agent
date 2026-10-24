#!/usr/bin/env python3
"""Position-scan hygiene: closed/history rows must never be scored.

Regression target (2026-10-18): ``run_cycle`` scored every row of Missy's
position scan, including rows tagged ``status="closed"``. A 15-row scan
produced 12 close signals, 10 of them for positions that no longer existed
on chain, and George failed each with "Position not found". The
close-settlement gate then treated those failures as in-flight closes and
blocked opens in the affected pools.

Hermetic: fixtures are written to a temp dir; no network, no live data dir.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lp_scoring  # noqa: E402


def _position(pid, status="active"):
    return {
        "position_address": pid,
        "pool_address": "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6",
        "dex": "meteora",
        "pair": "SOL-USDC",
        "status": status,
        "value_usd": 40.0,
        "liquidity_usd": 1000000.0,
        "volume_24h_usd": 2000000.0,
        "fee_24h_usd": 1200.0,
        "in_range": True,
        "opened_at": "2026-10-17T10:00:00+08:00",
    }


class FilterUnitTests(unittest.TestCase):
    def test_closed_rows_dropped(self):
        rows = [
            _position("live-1"),
            _position("dead-1", status="closed"),
            _position("live-2"),
        ]
        kept = lp_scoring.filter_live_positions(rows)
        self.assertEqual([p["position_address"] for p in kept], ["live-1", "live-2"])

    def test_all_closed_yields_empty(self):
        rows = [_position("dead-1", status="closed"), _position("dead-2", status="closed")]
        self.assertEqual(lp_scoring.filter_live_positions(rows), [])

    def test_missing_status_is_treated_as_live(self):
        row = _position("no-status")
        row.pop("status")
        self.assertEqual(len(lp_scoring.filter_live_positions([row])), 1)

    def test_non_dict_rows_dropped(self):
        rows = [_position("live-1"), None, "junk", 42]
        kept = lp_scoring.filter_live_positions(rows)
        self.assertEqual([p["position_address"] for p in kept], ["live-1"])

    def test_non_list_input_is_empty(self):
        self.assertEqual(lp_scoring.filter_live_positions(None), [])
        self.assertEqual(lp_scoring.filter_live_positions({"positions": []}), [])

    def test_case_sensitive_status_only(self):
        """Only the exact lowercase marker Missy writes is history."""
        row = _position("weird", status="CLOSED")
        self.assertEqual(len(lp_scoring.filter_live_positions([row])), 1)


class RunCycleIntegrationTests(unittest.TestCase):
    """End-to-end: a scan containing history must score only live rows."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.pools = self.dir / "pools"
        self.positions = self.dir / "positions"
        self.pools.mkdir()
        self.positions.mkdir()
        self.ts = int(time.time())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write_pools(self):
        pool = {
            "pool_address": "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6",
            "dex": "meteora",
            "pair": "SOL-USDC",
            "bin_step": 20,
            "liquidity_usd": 1000000.0,
            "volume_24h_usd": 2000000.0,
            "fee_24h_usd": 1200.0,
            "apr": 30.0,
            "base_mint": "So11111111111111111111111111111111111111112",
            "quote_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        }
        (self.pools / f"pool_scan-{self.ts}.json").write_text(
            json.dumps([pool]), encoding="utf-8"
        )

    def _write_positions(self, rows):
        (self.positions / f"position_scan-{self.ts}.json").write_text(
            json.dumps({"positions": rows}), encoding="utf-8"
        )

    def test_closed_rows_do_not_reach_scoring(self):
        self._write_pools()
        self._write_positions([
            _position("live-A"),
            _position("dead-A", status="closed"),
            _position("dead-B", status="closed"),
            _position("live-B"),
        ])
        report = lp_scoring.run_cycle(
            str(self.pools), str(self.positions), None, max_age_seconds=600
        )
        scored = {p.get("position_id") or p.get("position_address")
                  for p in report.get("position_scores", [])}
        self.assertNotIn("dead-A", scored)
        self.assertNotIn("dead-B", scored)
        self.assertLessEqual(len(scored), 2, scored)

    def test_no_close_verdict_for_closed_rows(self):
        self._write_pools()
        self._write_positions([
            _position("dead-A", status="closed"),
            _position("dead-B", status="closed"),
        ])
        report = lp_scoring.run_cycle(
            str(self.pools), str(self.positions), None, max_age_seconds=600
        )
        actions = [v.get("action") for v in report.get("verdicts", [])]
        self.assertNotIn("CLOSE", actions, report.get("verdicts"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
