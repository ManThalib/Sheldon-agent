"""Tests for the 2026-10-16 duplicate-close ghost guards.

Covers:
  - close_emit_suppressions (stale scan; pool settled after scan)
  - position_close_pending (position-level idempotency)
  - close_cooldown_pools (reopen cooldown map)
  - mark_emitted / record_emitted_closes position_id persistence
  - _filter_open_candidates reopen-cooldown rail
  - write_signals end-to-end suppression of the close family
"""

import json
import os
import tempfile
import time
import unittest

from run_cycle.close_settlement import (
    close_cooldown_pools,
    close_emit_suppressions,
    load_close_state,
    mark_emitted,
    position_close_pending,
    save_close_state,
)
from run_cycle.gates import _filter_open_candidates

NOW = time.time()
POLICY = {"close_emission": {"require_fresh_scan": True,
                             "max_scan_age_seconds": 900.0,
                             "require_scan_newer_than_last_close": True}}


def _close_verdict(pool="POOL1", pos="PosId1", action="CLOSE"):
    return {"action": action, "pool_address": pool, "dex": "orca",
            "position": pos, "score": 95.0, "lower_bound": 1,
            "upper_bound": 2, "evidence": "test"}


def _write_scan(positions_dir, mtime):
    os.makedirs(positions_dir, exist_ok=True)
    path = os.path.join(positions_dir, "position_scan-20261016-000000.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"positions": [], "history_complete": True}, fh)
    os.utime(path, (mtime, mtime))
    return path


class SuppressionTests(unittest.TestCase):
    def test_fresh_scan_no_suppression(self):
        reason, per_pool = close_emit_suppressions({}, NOW - 60, now=NOW,
                                                   policy=POLICY)
        self.assertIsNone(reason)
        self.assertEqual(per_pool, {})

    def test_stale_scan_suppresses_globally(self):
        reason, _ = close_emit_suppressions({}, NOW - 1000, now=NOW,
                                            policy=POLICY)
        self.assertIsNotNone(reason)
        self.assertIn("stale", reason)

    def test_missing_scan_fails_closed(self):
        reason, _ = close_emit_suppressions({}, 0.0, now=NOW, policy=POLICY)
        self.assertIsNotNone(reason)
        self.assertIn("missing", reason)

    def test_pool_settled_after_scan_suppresses_pool(self):
        state = {"s1": {"status": "FUNDS_VERIFIED", "pool_address": "POOL1",
                        "confirmed_at": NOW - 50,
                        "funds_verified_at": NOW - 40}}
        reason, per_pool = close_emit_suppressions(
            state, NOW - 100, now=NOW, policy=POLICY)
        self.assertIn("POOL1", per_pool)
        self.assertIn("duplicate-emit guard", per_pool["POOL1"])

    def test_pool_settled_before_scan_is_clean(self):
        state = {"s1": {"status": "FUNDS_VERIFIED", "pool_address": "POOL1",
                        "confirmed_at": NOW - 500,
                        "funds_verified_at": NOW - 400}}
        _, per_pool = close_emit_suppressions(state, NOW - 100, now=NOW,
                                              policy=POLICY)
        self.assertEqual(per_pool, {})

    def test_unrelated_pool_not_suppressed(self):
        state = {"s1": {"status": "FUNDS_VERIFIED", "pool_address": "OTHER",
                        "confirmed_at": NOW - 10}}
        _, per_pool = close_emit_suppressions(state, NOW - 100, now=NOW,
                                              policy=POLICY)
        self.assertNotIn("POOL1", per_pool)

    def test_rails_disabled(self):
        off = {"close_emission": {"require_fresh_scan": False,
                                  "require_scan_newer_than_last_close": False}}
        state = {"s1": {"status": "FUNDS_VERIFIED", "pool_address": "POOL1",
                        "confirmed_at": NOW - 10}}
        reason, per_pool = close_emit_suppressions(state, 0.0, now=NOW,
                                                   policy=off)
        self.assertIsNone(reason)
        self.assertEqual(per_pool, {})


class PositionIdempotencyTests(unittest.TestCase):
    def test_in_flight_close_blocks_same_position(self):
        state = {"s1": {"status": "CLOSE_EMITTED", "pool_address": "POOL1",
                        "position_id": "PosId1", "emit_time": NOW - 30}}
        self.assertEqual(position_close_pending(state, "POOL1", "PosId1"),
                         "s1")

    def test_settled_close_does_not_block_new_position(self):
        state = {"s1": {"status": "FUNDS_VERIFIED", "pool_address": "POOL1",
                        "position_id": "PosId1", "confirmed_at": NOW - 30}}
        self.assertIsNone(position_close_pending(state, "POOL1", "PosId1"))

    def test_different_position_not_blocked(self):
        state = {"s1": {"status": "CLOSE_EMITTED", "pool_address": "POOL1",
                        "position_id": "PosId1", "emit_time": NOW - 30}}
        self.assertIsNone(position_close_pending(state, "POOL1", "PosId2"))

    def test_pool_mismatch_not_blocked(self):
        state = {"s1": {"status": "CLOSE_EMITTED", "pool_address": "OTHER",
                        "position_id": "PosId1", "emit_time": NOW - 30}}
        self.assertIsNone(position_close_pending(state, "POOL1", "PosId1"))

    def test_empty_position_id_never_matches(self):
        state = {"s1": {"status": "CLOSE_EMITTED", "pool_address": "POOL1",
                        "position_id": None, "emit_time": NOW - 30}}
        self.assertIsNone(position_close_pending(state, "POOL1", None))
        self.assertIsNone(position_close_pending(state, "POOL1", ""))


class CooldownTests(unittest.TestCase):
    def test_settled_entry_sets_cooldown(self):
        state = {"s1": {"status": "FUNDS_VERIFIED", "pool_address": "POOL1",
                        "funds_verified_at": NOW - 100}}
        cooled = close_cooldown_pools(state, now=NOW,
                                      cooldown_seconds=2700.0)
        self.assertAlmostEqual(cooled["POOL1"], NOW - 100 + 2700.0)

    def test_blocking_entries_do_not_cool(self):
        state = {"s1": {"status": "CLOSE_REVIEW_REQUIRED",
                        "pool_address": "POOL1", "rejected_at": NOW - 100}}
        self.assertEqual(close_cooldown_pools(state, now=NOW), {})

    def test_disabled_cooldown(self):
        state = {"s1": {"status": "FUNDS_VERIFIED", "pool_address": "POOL1",
                        "funds_verified_at": NOW - 100}}
        self.assertEqual(close_cooldown_pools(state, now=NOW,
                                              cooldown_seconds=0), {})

    def test_filter_blocks_active_cooldown(self):
        cand = {"pool_address": "POOL9", "dex": "orca", "score": 99.0,
                "_pool": {"current_tick_index": 100}}
        cooled = {"POOL9": time.time() + 600}
        kept, skipped = _filter_open_candidates(
            [cand], {}, {}, 90.0, 25000.0, 5000.0, {4, 10, 20, 25, 50, 100},
            cooldown_pools=cooled)
        self.assertEqual(kept, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("reopen cooldown", skipped[0]["reason"])

    def test_filter_ignores_expired_cooldown(self):
        cand = {"pool_address": "POOL9", "dex": "orca", "score": 99.0,
                "_pool": {"current_tick_index": 100,
                          "eligible": True,
                          "tvl": 100000.0, "volume_window": 100000.0}}
        cooled = {"POOL9": time.time() - 1}
        kept, _ = _filter_open_candidates(
            [cand], {}, {}, 90.0, 25000.0, 5000.0, {4, 10, 20, 25, 50, 100},
            cooldown_pools=cooled)
        self.assertEqual(len(kept), 1)

    def test_filter_default_off(self):
        cand = {"pool_address": "POOL9", "dex": "orca", "score": 99.0,
                "_pool": {"current_tick_index": 100,
                          "eligible": True,
                          "tvl": 100000.0, "volume_window": 100000.0}}
        kept, _ = _filter_open_candidates(
            [cand], {}, {}, 90.0, 25000.0, 5000.0, {4, 10, 20, 25, 50, 100})
        self.assertEqual(len(kept), 1)


class EmitPersistenceTests(unittest.TestCase):
    def test_mark_emitted_records_position_id(self):
        state = {}
        mark_emitted(state, "sig-1", "POOL1", now_ts=NOW,
                     position_id="PosId1")
        self.assertEqual(state["sig-1"]["position_id"], "PosId1")

    def test_record_emitted_closes_stores_position_id(self):
        from run_cycle.close_settlement import record_emitted_closes
        with tempfile.TemporaryDirectory() as tmp:
            sig = {"signal_id": "sig-9", "action": "close",
                   "pool_address": "POOL1", "position_id": "PosId9"}
            path = os.path.join(tmp, "sig-9.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(sig, fh)
            emitted = record_emitted_closes(tmp, [path])
            self.assertEqual(emitted[0]["position_id"], "PosId9")
            state = load_close_state(tmp)
            self.assertEqual(state["sig-9"]["position_id"], "PosId9")


class WriteSignalsGuardTests(unittest.TestCase):
    def _run(self, verdict, scan_mtime, close_state=None):
        from run_cycle.signals import write_signals
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = os.path.join(tmp, "state")
            sig_dir = os.path.join(tmp, "signals")
            pos_dir = os.path.join(tmp, "positions")
            os.makedirs(state_dir)
            if close_state:
                save_close_state(state_dir, close_state)
            _write_scan(pos_dir, scan_mtime)
            report = {"verdicts": [verdict]}
            created, review = write_signals(
                report, sig_dir, pos_dir, state_dir=state_dir,
                close_emission=POLICY["close_emission"])
            written = []
            for path in created:
                with open(path, encoding="utf-8") as fh:
                    written.append(json.load(fh))
            return created, review, sig_dir, written

    def test_stale_scan_suppresses_close(self):
        created, review, _, written = self._run(
            _close_verdict(), NOW - 2000)
        self.assertEqual(created, [])
        self.assertEqual(written, [])
        self.assertTrue(any("suppressed" in r["reason"] for r in review))

    def test_fresh_scan_emits_close(self):
        created, review, _, written = self._run(_close_verdict(), NOW - 60)
        self.assertEqual(len(created), 1)
        self.assertEqual(written[0]["action"], "close")
        self.assertEqual(written[0]["position_id"], "PosId1")

    def test_pool_settled_after_scan_suppresses(self):
        state = {"s1": {"status": "FUNDS_VERIFIED", "pool_address": "POOL1",
                        "confirmed_at": NOW - 30,
                        "funds_verified_at": NOW - 20}}
        created, review, _, _ = self._run(_close_verdict(), NOW - 100,
                                          close_state=state)
        self.assertEqual(created, [])
        self.assertTrue(any("duplicate-emit guard" in r["reason"]
                            for r in review))

    def test_scan_newer_than_settlement_emits(self):
        state = {"s1": {"status": "FUNDS_VERIFIED", "pool_address": "POOL1",
                        "confirmed_at": NOW - 500,
                        "funds_verified_at": NOW - 400}}
        created, _, _, _ = self._run(_close_verdict(), NOW - 60,
                                     close_state=state)
        self.assertEqual(len(created), 1)

    def test_in_flight_position_suppressed(self):
        state = {"s1": {"status": "CLOSE_EMITTED", "pool_address": "POOL1",
                        "position_id": "PosId1", "emit_time": NOW - 30}}
        created, review, _, _ = self._run(_close_verdict(), NOW - 60,
                                          close_state=state)
        self.assertEqual(created, [])
        self.assertTrue(any("already in flight" in r["reason"]
                            for r in review))

    def test_replacement_position_not_blocked(self):
        state = {"s1": {"status": "CLOSE_EMITTED", "pool_address": "POOL1",
                        "position_id": "PosIdOld", "emit_time": NOW - 30}}
        created, _, _, written = self._run(
            _close_verdict(pos="PosIdNew"), NOW - 60, close_state=state)
        self.assertEqual(len(created), 1)
        self.assertEqual(written[0]["position_id"], "PosIdNew")

    def test_collect_fees_never_suppressed(self):
        # Stale scan + settled-after-scan state: claims must still flow.
        state = {"s1": {"status": "FUNDS_VERIFIED", "pool_address": "POOL1",
                        "confirmed_at": NOW - 30,
                        "funds_verified_at": NOW - 20}}
        created, review, _, written = self._run(
            _close_verdict(action="COLLECT_FEES"), NOW - 2000,
            close_state=state)
        self.assertEqual(len(created), 1)
        self.assertEqual(written[0]["action"], "claim_fees")

    def test_guards_inactive_without_state_dir(self):
        # Legacy callers (no state_dir) keep the old behavior: guards are
        # settlement-state derived and only active in the production wiring.
        from run_cycle.signals import write_signals
        with tempfile.TemporaryDirectory() as tmp:
            sig_dir = os.path.join(tmp, "signals")
            pos_dir = os.path.join(tmp, "positions")
            _write_scan(pos_dir, NOW - 2000)
            created, review = write_signals(
                {"verdicts": [_close_verdict()]}, sig_dir, pos_dir)
            self.assertEqual(len(created), 1)
            self.assertFalse(any("suppressed" in r["reason"]
                                 for r in review))


if __name__ == "__main__":
    unittest.main()
