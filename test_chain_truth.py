"""Tests for the dry_run chain-truth reconciliation (owner ruling 2026-10-19).

The gate this replaces was absolute: settle only on a live journal decision,
which dry_run can never produce, so every emitted close blocked opens forever
and got re-emitted the next cycle (18 ghosts over 9 cycles). The fix proves
closure on chain instead — but only in dry_run, only with a positive control,
and never for an in-flight close.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_cycle import chain_truth  # noqa: E402
from run_cycle.close_settlement import (  # noqa: E402
    CHAIN_TRUTH_CANDIDATE_STATUSES,
    check_settlement,
    reconcile_chain_truth,
)

POS = "89VQC8qBfxrjZKW6FAC7FFCCJzbujJXmqJwqrmvrwuPY"
POOL = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"
ABSENT = {"absent": True, "status": "absent", "reason": "proved",
          "checked_at": 1.0, "rpc": "***"}
PRESENT = {"absent": False, "status": "present",
           "reason": "position account still exists on chain"}
UNKNOWN = {"absent": False, "status": "unknown", "reason": "rpc timeout"}


def _state(status="CLOSE_EMITTED", **kw):
    entry = {"status": status, "pool_address": POOL, "position_id": POS,
             "emit_time": 1000.0, "signal_id": "sheldon-1-1"}
    entry.update(kw)
    return {"sheldon-1-1": entry}


class ReconcileTest(unittest.TestCase):
    def _run(self, state, verdict=ABSENT, mode="dry_run", policy=None,
             pending=None, rpc="http://rpc.invalid", **kw):
        calls = []

        def fake_check(url, pid, pool):
            calls.append((url, pid, pool))
            return dict(verdict)

        report = reconcile_chain_truth(
            state, chain_check=fake_check, rpc_url=rpc, mode=mode,
            now=2000.0, policy=policy or {},
            pending_position_ids=pending or set())
        return report, state, calls

    def test_dry_run_absent_resolves_without_manual_flag(self):
        report, state, calls = self._run(_state())
        entry = state["sheldon-1-1"]
        self.assertEqual(entry["status"], "CLOSE_RESOLVED")
        self.assertIs(entry["manual"], False)
        self.assertEqual(entry["resolution"], "chain_truth")
        self.assertIn("chain_evidence", entry)
        self.assertEqual(len(report["resolved"]), 1)
        self.assertEqual(calls[0][1], POS)

    def test_live_mode_never_resolves(self):
        for mode in ("auto", "confirm_each", "", None, "unknown"):
            report, state, calls = self._run(_state(), mode=mode)
            self.assertEqual(state["sheldon-1-1"]["status"], "CLOSE_EMITTED",
                             f"mode={mode!r} must not reconcile")
            self.assertFalse(report["enabled"])
            self.assertEqual(calls, [])

    def test_present_position_is_not_resolved(self):
        _, state, _ = self._run(_state(), verdict=PRESENT)
        self.assertEqual(state["sheldon-1-1"]["status"], "CLOSE_EMITTED")

    def test_unknown_verdict_is_not_resolved(self):
        _, state, _ = self._run(_state(), verdict=UNKNOWN)
        self.assertEqual(state["sheldon-1-1"]["status"], "CLOSE_EMITTED")

    def test_policy_can_disable(self):
        report, state, _ = self._run(
            _state(), policy={"dry_run_chain_truth_reconciliation": False})
        self.assertEqual(state["sheldon-1-1"]["status"], "CLOSE_EMITTED")
        self.assertFalse(report["enabled"])

    def test_in_flight_close_is_skipped(self):
        _, state, calls = self._run(_state(), pending={POS})
        self.assertEqual(state["sheldon-1-1"]["status"], "CLOSE_EMITTED")
        self.assertEqual(calls, [], "must not even query for an in-flight close")

    def test_manual_entry_untouched(self):
        _, state, _ = self._run(_state(manual=True))
        self.assertEqual(state["sheldon-1-1"]["status"], "CLOSE_EMITTED")
        self.assertIs(state["sheldon-1-1"]["manual"], True)

    def test_already_settled_untouched(self):
        for st in ("CLOSE_RESOLVED", "FUNDS_VERIFIED"):
            _, state, _ = self._run(_state(status=st))
            self.assertEqual(state["sheldon-1-1"]["status"], st)

    def test_one_rpc_verdict_per_position(self):
        state = {}
        for i in range(9):
            state[f"s-{i}"] = {"status": "CLOSE_EMITTED", "signal_id": f"s-{i}",
                               "pool_address": POOL, "position_id": POS,
                               "emit_time": 1000.0 + i}
        report, state, calls = self._run(state)
        self.assertEqual(len(calls), 1, "9 ghosts of 1 position = 1 query")
        self.assertEqual(len(report["resolved"]), 9)
        self.assertTrue(all(e["status"] == "CLOSE_RESOLVED"
                            for e in state.values()))

    def test_candidate_statuses_cover_dry_run_ghosts(self):
        for st in ("CLOSE_EMITTED", "CLOSE_UNKNOWN", "CLOSE_UNKNOWN_TIMEOUT",
                   "CLOSE_RETRY_ALLOWED", "CLOSE_REVIEW_REQUIRED"):
            self.assertIn(st, CHAIN_TRUTH_CANDIDATE_STATUSES)

    def test_missing_identifiers_not_queried(self):
        st = _state()
        st["sheldon-1-1"].pop("position_id")
        _, state, calls = self._run(st)
        self.assertEqual(calls, [])
        self.assertEqual(state["sheldon-1-1"]["status"], "CLOSE_EMITTED")


class CheckSettlementIntegrationTest(unittest.TestCase):
    """The chain report must be surfaced, and gates must reflect it."""

    def test_check_settlement_resolves_and_unblocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _state()
            allowed, reason, updated, details = check_settlement(
                active_positions={}, wallet_mtime=0.0, position_mtime=0.0,
                close_state=state, now=2000.0,
                mode="dry_run", chain_rpc_url="http://rpc.invalid",
                chain_check=lambda u, p, a: dict(ABSENT),
                journal_dir=tmp)
            self.assertTrue(allowed, reason)
            self.assertEqual(updated["sheldon-1-1"]["status"],
                             "CLOSE_RESOLVED")
            self.assertEqual(details["chain_truth"]["resolved"][0]["signal_id"],
                             "sheldon-1-1")
            self.assertEqual(details["blocking"], [])

    def test_check_settlement_still_blocks_without_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            allowed, reason, updated, details = check_settlement(
                active_positions={}, wallet_mtime=0.0, position_mtime=0.0,
                close_state=_state(), now=2000.0,
                chain_rpc_url="http://rpc.invalid",
                chain_check=lambda u, p, a: dict(ABSENT),
                journal_dir=tmp)
            self.assertFalse(allowed)
            self.assertEqual(updated["sheldon-1-1"]["status"], "CLOSE_EMITTED")
            self.assertFalse(details["chain_truth"]["enabled"])


class ChainTruthProbeTest(unittest.TestCase):
    """check_position_absent must fail closed on anything short of proof."""

    def test_no_endpoint(self):
        ev = chain_truth.check_position_absent(None, POS, POOL)
        self.assertFalse(ev["absent"])
        self.assertIn("no rpc", ev["reason"])

    def test_bad_addresses_never_queried(self):
        for pos, pool in (("", POOL), (None, POOL), (POS, ""),
                          ("0OIl", POOL), (POS, "not-an-address")):
            ev = chain_truth.check_position_absent("http://rpc.invalid",
                                                   pos, pool)
            self.assertFalse(ev["absent"], f"{pos!r}/{pool!r}")

    def test_rpc_not_reported(self):
        ev = chain_truth.check_position_absent(
            "https://secret-endpoint.example/KEY", "", POOL)
        self.assertEqual(ev["rpc"], chain_truth.MASK)
        self.assertNotIn("secret-endpoint", json.dumps(ev))

    def _patch(self, finalized_pool, finalized_pos, confirmed_pos):
        def fake(endpoint, pubkeys, commitment, timeout):
            if commitment == "finalized":
                return [{"data": ["x", "base64"]} if finalized_pool else None,
                        {"data": ["x", "base64"]} if finalized_pos else None]
            return [{"data": ["x", "base64"]} if confirmed_pos else None]
        self._orig = chain_truth._multiple
        chain_truth._multiple = fake

    def tearDown(self):
        if hasattr(self, "_orig"):
            chain_truth._multiple = self._orig

    def test_absent_needs_pool_control(self):
        self._patch(finalized_pool=False, finalized_pos=False,
                    confirmed_pos=False)
        ev = chain_truth.check_position_absent("http://x.invalid", POS, POOL)
        self.assertFalse(ev["absent"])
        self.assertIn("control failed", ev["reason"])

    def test_absent_needs_both_commitments(self):
        self._patch(finalized_pool=True, finalized_pos=False,
                    confirmed_pos=True)
        ev = chain_truth.check_position_absent("http://x.invalid", POS, POOL)
        self.assertFalse(ev["absent"])

    def test_absent_when_proved(self):
        self._patch(finalized_pool=True, finalized_pos=False,
                    confirmed_pos=False)
        ev = chain_truth.check_position_absent("http://x.invalid", POS, POOL)
        self.assertTrue(ev["absent"])
        self.assertEqual(ev["status"], "absent")

    def test_present_position(self):
        self._patch(finalized_pool=True, finalized_pos=True,
                    confirmed_pos=True)
        ev = chain_truth.check_position_absent("http://x.invalid", POS, POOL)
        self.assertFalse(ev["absent"])
        self.assertEqual(ev["status"], "present")


if __name__ == "__main__":
    unittest.main()
