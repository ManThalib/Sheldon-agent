"""Phase 2 tests: close settlement state machine + rotation enforcement.

Covers the 2026-10-09 plan Changes E (FUNDS_VERIFIED chain), F (rejected
close handling) and G (CLOSE_EMITTED written at emit time).
"""

import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_cycle.close_settlement import (
    classify_submission_state,
    advance,
    derive_rejected_statuses,
    check_settlement,
    classify_rejection,
    load_close_state,
    mark_emitted,
    prune_settled,
    save_close_state,
    summarize,
    update_from_journal,
)
from run_cycle import record_emitted_closes
from run_cycle.rotation import _settle_rotations

NOW = 1_791_500_000.0


def _entry(**kw):
    base = {"status": "CLOSE_EMITTED", "pool_address": "P1"}
    base.update(kw)
    return base


class ClassificationTests(unittest.TestCase):
    def test_pre_submission_causes_are_retryable(self):
        # Owner ruling 2026-10-19: only provably pre-submission failures may
        # be retried. These all fail before a transaction could be sent.
        for reason in ("slippage exceeded", "SOLANA_AGENT_WALLET not set",
                       "signal validation failed", "insufficient funds for fees",
                       "preflight: INSUFFICIENT_SOL_FOR_OPEN"):
            self.assertEqual(classify_submission_state(reason),
                             "pre_submission", reason)
            self.assertEqual(classify_rejection(reason), "retryable", reason)

    def test_ambiguous_causes_are_never_retryable(self):
        # A timeout/RPC/blockhash failure may have landed. Retrying it could
        # close an already-closed position, so it stays in review even when
        # the position is still visible in a fresh scan.
        for reason in ("RPC timeout", "rate limit hit", "blockhash not found",
                       "network error", "fetch failed", "429",
                       "transaction expired"):
            self.assertEqual(classify_submission_state(reason),
                             "ambiguous", reason)
            self.assertEqual(classify_rejection(reason), "review", reason)

    def test_unknown_causes_require_review(self):
        for reason in ("", None, "position not found", "unknown error",
                       "weird failure"):
            self.assertEqual(classify_rejection(reason), "review", reason)


class MarkEmittedTests(unittest.TestCase):
    def test_records_emit_with_pool(self):
        state = {}
        mark_emitted(state, "sig-1", "P1", now_ts=NOW)
        self.assertEqual(state["sig-1"]["status"], "CLOSE_EMITTED")
        self.assertEqual(state["sig-1"]["pool_address"], "P1")
        self.assertEqual(state["sig-1"]["emit_time"], NOW)

    def test_new_emit_supersedes_prior_rejections_for_same_pool(self):
        state = {"sig-old": _entry(status="CLOSE_RETRY_ALLOWED",
                                   pool_address="P1")}
        mark_emitted(state, "sig-new", "P1", now_ts=NOW)
        self.assertEqual(state["sig-old"]["status"], "CLOSE_RESOLVED")
        self.assertEqual(state["sig-old"]["superseded_by"], "sig-new")
        self.assertEqual(state["sig-new"]["status"], "CLOSE_EMITTED")

    def test_new_emit_does_not_touch_review_required_or_manual(self):
        state = {
            "sig-review": _entry(status="CLOSE_REVIEW_REQUIRED"),
            "sig-manual": _entry(status="CLOSE_REVIEW_REQUIRED",
                                 manual=True),
            "sig-other": _entry(status="CLOSE_RETRY_ALLOWED",
                                pool_address="P2"),
        }
        mark_emitted(state, "sig-new", "P1", now_ts=NOW)
        self.assertEqual(state["sig-review"]["status"], "CLOSE_REVIEW_REQUIRED")
        self.assertEqual(state["sig-manual"]["status"], "CLOSE_REVIEW_REQUIRED")
        self.assertEqual(state["sig-other"]["status"], "CLOSE_RETRY_ALLOWED")


class JournalFoldTests(unittest.TestCase):
    def test_executed_decision_records_confirmation(self):
        state = {"sig-1": _entry()}
        update_from_journal(
            state, {"sig-1": {"status": "executed",
                              "confirmed_at": NOW - 5, "reason": "ok"}},
            {"P1": True}, True)
        derive_rejected_statuses(state, {"P1": True}, True)
        self.assertEqual(state["sig-1"]["status"], "CLOSE_EXECUTED")
        self.assertEqual(state["sig-1"]["confirmed_at"], NOW - 5)

    def test_rejected_with_position_present_and_retryable(self):
        state = {"sig-1": _entry()}
        update_from_journal(
            state, {"sig-1": {"status": "rejected",
                              "confirmed_at": NOW - 5,
                              "reason": "slippage exceeded"}},
            {"P1": True}, True)
        derive_rejected_statuses(state, {"P1": True}, True)
        self.assertEqual(state["sig-1"]["status"], "CLOSE_RETRY_ALLOWED")

    def test_rejected_with_position_absent_is_ambiguous(self):
        state = {"sig-1": _entry()}
        update_from_journal(
            state, {"sig-1": {"status": "failed",
                              "confirmed_at": NOW - 5,
                              "reason": "slippage exceeded"}},
            {"P1": False}, True)
        derive_rejected_statuses(state, {"P1": False}, True)
        self.assertEqual(state["sig-1"]["status"], "CLOSE_REVIEW_REQUIRED")

    def test_rejected_with_unknown_cause_requires_review(self):
        state = {"sig-1": _entry()}
        update_from_journal(
            state, {"sig-1": {"status": "failed_verify",
                              "confirmed_at": NOW - 5,
                              "reason": "signature mismatch"}},
            {"P1": True}, True)
        derive_rejected_statuses(state, {"P1": True}, True)
        self.assertEqual(state["sig-1"]["status"], "CLOSE_REVIEW_REQUIRED")

    def test_stale_duplicate_resolves_once_pool_settled(self):
        # sig-old was rejected, but the pool settled (FUNDS_VERIFIED) at a
        # later time: the rejection was a duplicate emit racing the scan
        # refresh -> CLOSE_RESOLVED, re-derived on every cycle.
        state = {
            "sig-old": _entry(status="CLOSE_REJECTED", rejected_at=NOW - 100),
            "sig-good": _entry(status="FUNDS_VERIFIED",
                               confirmed_at=NOW - 50),
        }
        update_from_journal(
            state, {"sig-old": {"status": "rejected",
                                "confirmed_at": NOW - 100,
                                "reason": "position not found"}},
            {"P1": False}, True)
        derive_rejected_statuses(state, {"P1": False}, True)
        self.assertEqual(state["sig-old"]["status"], "CLOSE_RESOLVED")
        self.assertEqual(state["sig-good"]["status"], "FUNDS_VERIFIED")

    def test_manual_resolution_never_overridden(self):
        state = {"sig-1": _entry(status="CLOSE_RESOLVED", manual=True)}
        update_from_journal(
            state, {"sig-1": {"status": "rejected",
                              "confirmed_at": NOW, "reason": "rpc"}},
            {"P1": True}, True)
        derive_rejected_statuses(state, {"P1": True}, True)
        self.assertEqual(state["sig-1"]["status"], "CLOSE_RESOLVED")

    def test_legacy_close_accepted_mapped_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_close_state(tmp, {"sig-1": {"status": "CLOSE_ACCEPTED"}})
            state = load_close_state(tmp)
        self.assertEqual(state["sig-1"]["status"], "CLOSE_EXECUTED")
        self.assertEqual(state["sig-1"]["legacy_alias"], "CLOSE_ACCEPTED")


class AdvanceTests(unittest.TestCase):
    def _executed(self, conf):
        return {"sig-1": _entry(status="CLOSE_EXECUTED", confirmed_at=conf)}

    def test_full_chain_reaches_funds_verified(self):
        state = self._executed(NOW - 100)
        advance(state, active_positions={},   # source gone
                wallet_mtime=NOW - 10,        # wallet after confirmation
                position_mtime=NOW - 10,      # scan after confirmation
                now=NOW)
        blocking, review = summarize(state)
        self.assertEqual(state["sig-1"]["status"], "FUNDS_VERIFIED")
        self.assertEqual(blocking, [])
        self.assertEqual(review, [])

    def test_stale_position_scan_parks_at_chain_verified(self):
        # Scan is newer than the close confirmation (chain-verified) but
        # older than the max scan age: the position-scan confirmation step
        # refuses stale evidence and the entry parks, still blocking.
        state = self._executed(NOW - 100_000)
        advance(state, {}, wallet_mtime=NOW - 10,
                position_mtime=NOW - 5_000, now=NOW,
                max_scan_age_seconds=900.0)
        blocking, _ = summarize(state)
        self.assertEqual(state["sig-1"]["status"], "CLOSE_CHAIN_VERIFIED")
        self.assertEqual(len(blocking), 1)

    def test_scan_older_than_confirmation_never_chain_verifies(self):
        # Absence proof must postdate the close confirmation.
        state = self._executed(NOW - 100)
        advance(state, {}, wallet_mtime=NOW - 10,
                position_mtime=NOW - 100_000, now=NOW)
        blocking, _ = summarize(state)
        self.assertEqual(state["sig-1"]["status"], "CLOSE_EXECUTED")
        self.assertEqual(len(blocking), 1)

    def test_stale_wallet_scan_blocks_funds_verification(self):
        state = self._executed(NOW - 100)
        advance(state, {}, wallet_mtime=NOW - 200,
                position_mtime=NOW - 10, now=NOW)
        blocking, _ = summarize(state)
        self.assertEqual(state["sig-1"]["status"], "POSITION_SCAN_VERIFIED")
        self.assertEqual(len(blocking), 1)

    def test_position_still_present_blocks(self):
        state = self._executed(NOW - 100)
        advance(state, {"P1": {"pool_address": "P1"}},
                wallet_mtime=NOW - 10, position_mtime=NOW - 10, now=NOW)
        blocking, _ = summarize(state)
        self.assertEqual(state["sig-1"]["status"], "CLOSE_EXECUTED")
        self.assertEqual(len(blocking), 1)

    def test_executed_without_confirmation_does_not_settle(self):
        state = {"sig-1": _entry(status="CLOSE_EXECUTED")}
        advance(state, {}, wallet_mtime=NOW, position_mtime=NOW, now=NOW)
        blocking, _ = summarize(state)
        self.assertEqual(state["sig-1"]["status"], "CLOSE_EXECUTED")
        self.assertEqual(len(blocking), 1)

    def test_emitted_entry_times_out_after_ttl(self):
        state = {"sig-1": _entry(emit_time=NOW - 8 * 24 * 3600)}
        advance(state, {}, wallet_mtime=NOW, position_mtime=NOW, now=NOW)
        blocking, review = summarize(state)
        self.assertEqual(state["sig-1"]["status"], "CLOSE_UNKNOWN_TIMEOUT")
        self.assertEqual(len(blocking), 1)      # still blocking
        self.assertEqual(len(review), 1)        # and surfaced for review

    def test_policy_can_relax_wallet_confirmation(self):
        state = self._executed(NOW - 100)
        advance(state, {}, wallet_mtime=NOW - 200,
                position_mtime=NOW - 10, now=NOW,
                policy={"require_wallet_capital_confirmation": False})
        self.assertEqual(state["sig-1"]["status"], "FUNDS_VERIFIED")

    def test_gate_blocks_other_pools_too(self):
        # Global semantics preserved: one unsettled close blocks opens for
        # every pool, never just its own.
        state = {"sig-1": _entry()}             # plain CLOSE_EMITTED
        advance(state, {"P9": {"pool_address": "P9"}},
                wallet_mtime=NOW, position_mtime=NOW, now=NOW)
        blocking, _ = summarize(state)
        self.assertEqual(len(blocking), 1)


class CheckSettlementTests(unittest.TestCase):
    def test_empty_state_allows(self):
        allowed, reason, state, details = check_settlement(
            {}, NOW, NOW, {}, now=NOW, journal_dir=_no_journal(tmp_dir()))
        self.assertTrue(allowed)
        self.assertEqual(details["tracked"], 0)

    def test_settled_only_when_funds_verified(self):
        state = {"sig-1": _entry(status="CLOSE_EXECUTED",
                                 confirmed_at=NOW - 100)}
        allowed, _, _, details = check_settlement(
            {}, wallet_mtime=NOW - 10, position_mtime=NOW - 10,
            close_state=state, now=NOW, journal_dir=_no_journal(tmp_dir()))
        self.assertTrue(allowed)
        self.assertEqual(state["sig-1"]["status"], "FUNDS_VERIFIED")

    def test_rejected_needs_resolution_before_open(self):
        state = {"sig-1": _entry()}
        allowed, _, _, details = check_settlement(
            {"P1": {"pool_address": "P1"}}, NOW, NOW, state, now=NOW,
            journal_dir=_no_journal(tmp_dir()))
        self.assertFalse(allowed)


class RotationGuardTests(unittest.TestCase):
    DAY = "2026-10-09"

    def _rot(self):
        return {"SRC": {"candidate_pool": "CAND", "cycle_day": self.DAY,
                        "status": "awaiting_close",
                        "close_signal_id": "sig-rot"}}

    def test_stale_position_scan_defers_settlement(self):
        rot = self._rot()
        _settle_rotations(rot, {}, {}, now_ts=NOW,
                          position_mtime=NOW - 100_000,
                          max_position_scan_age=900.0)
        self.assertEqual(rot["SRC"]["status"], "awaiting_close")
        self.assertIn("not current", rot["SRC"].get("settlement_note", ""))

    def test_missing_position_mtime_defers_settlement(self):
        rot = self._rot()
        _settle_rotations(rot, {}, {}, now_ts=NOW, position_mtime=0.0)
        self.assertEqual(rot["SRC"]["status"], "awaiting_close")

    def test_wallet_older_than_confirmation_defers_settlement(self):
        rot = self._rot()
        _settle_rotations(
            rot, {},
            {"sig-rot": {"status": "executed", "confirmed_at": NOW - 10}},
            now_ts=NOW, position_mtime=NOW,
            wallet_mtime=NOW - 100)
        self.assertEqual(rot["SRC"]["status"], "awaiting_close")
        self.assertIn("wallet scan predates",
                      rot["SRC"].get("settlement_note", ""))

    def test_wallet_confirmation_not_required_without_journal(self):
        # No journal record for the close: absence from a current scan is
        # the settle evidence for the reservation (the OPEN itself remains
        # gated by the settlement machine).
        rot = self._rot()
        _settle_rotations(rot, {}, {}, now_ts=NOW, position_mtime=NOW,
                          wallet_mtime=NOW)
        self.assertEqual(rot["SRC"]["status"], "settled")


class RecordEmittedClosesTests(unittest.TestCase):
    """Change G wiring: close signals written -> CLOSE_EMITTED persisted."""

    def _write_signal(self, directory, signal):
        path = os.path.join(directory, f"{signal['signal_id']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(signal, fh)
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state_dir)

    def test_close_signals_marked_emitted(self):
        close = self._write_signal(self.tmp.name, {
            "signal_id": "sig-c1", "action": "close",
            "pool_address": "P1"})
        claim = self._write_signal(self.tmp.name, {
            "signal_id": "sig-c2", "action": "claim_fees",
            "pool_address": "P1"})
        emitted = record_emitted_closes(self.state_dir, [close, claim])
        self.assertEqual([e["signal_id"] for e in emitted], ["sig-c1"])
        state = load_close_state(self.state_dir)
        self.assertEqual(state["sig-c1"]["status"], "CLOSE_EMITTED")
        self.assertEqual(state["sig-c1"]["pool_address"], "P1")
        self.assertNotIn("sig-c2", state)

    def test_new_emit_supersedes_prior_rejection(self):
        save_close_state(self.state_dir, {
            "sig-old": {"status": "CLOSE_RETRY_ALLOWED",
                        "pool_address": "P1"}})
        close = self._write_signal(self.tmp.name, {
            "signal_id": "sig-new", "action": "close",
            "pool_address": "P1"})
        record_emitted_closes(self.state_dir, [close])
        state = load_close_state(self.state_dir)
        self.assertEqual(state["sig-old"]["status"], "CLOSE_RESOLVED")
        self.assertEqual(state["sig-old"]["superseded_by"], "sig-new")
        self.assertEqual(state["sig-new"]["status"], "CLOSE_EMITTED")

    def test_no_close_signals_leaves_state_untouched(self):
        swap = self._write_signal(self.tmp.name, {
            "signal_id": "sig-s1", "action": "swap_to_usdc"})
        emitted = record_emitted_closes(self.state_dir, [swap])
        self.assertEqual(emitted, [])
        self.assertEqual(load_close_state(self.state_dir), {})


class PruneTests(unittest.TestCase):
    def test_old_settled_entries_pruned(self):
        state = {
            "old": {"status": "FUNDS_VERIFIED",
                    "funds_verified_at": NOW - 40 * 24 * 3600},
            "new": {"status": "FUNDS_VERIFIED",
                    "funds_verified_at": NOW - 100},
            "stuck": {"status": "CLOSE_REVIEW_REQUIRED",
                      "rejected_at": NOW - 40 * 24 * 3600},
        }
        prune_settled(state, now=NOW)
        self.assertNotIn("old", state)
        self.assertIn("new", state)
        self.assertIn("stuck", state)   # blocking entries are never pruned

    def test_iso_resolved_at_does_not_crash_prune(self):
        # resolve_entry (--resolve-close) once wrote resolved_at as an ISO
        # string while mark_emitted wrote an epoch float; the numeric max()
        # in prune_settled then raised TypeError and every later cycle died.
        # Both shapes must be accepted.
        state = {
            "manual-iso": {"status": "CLOSE_RESOLVED", "manual": True,
                           "resolved_at": "2026-10-13T23:01:21Z"},
            "manual-old-iso": {"status": "CLOSE_RESOLVED", "manual": True,
                               "resolved_at": "2020-01-01T00:00:00Z"},
            "manual-float": {"status": "CLOSE_RESOLVED", "manual": True,
                             "resolved_at": NOW - 100},
        }
        prune_settled(state, now=NOW)      # must not raise
        self.assertIn("manual-iso", state)
        self.assertNotIn("manual-old-iso", state)   # genuinely old -> pruned
        self.assertIn("manual-float", state)

    def test_resolve_entry_writes_epoch_float(self):
        # The CLI resolution path must store a numeric timestamp so the
        # state file stays prunable by later cycles.
        from run_cycle.close_settlement import resolve_entry
        path = tmp_dir()
        save_close_state(path, {"sig-x": {
            "status": "CLOSE_REVIEW_REQUIRED", "pool_address": "P1"}})
        rc = resolve_entry(path, "sig-x", note="test")
        self.assertEqual(rc, 0)
        entry = load_close_state(path)["sig-x"]
        self.assertEqual(entry["status"], "CLOSE_RESOLVED")
        self.assertTrue(entry["manual"])
        self.assertIsInstance(entry["resolved_at"], float)
        prune_settled(load_close_state(path), now=entry["resolved_at"] + 1)


def tmp_dir():
    return tempfile.mkdtemp()


def _no_journal(path):
    """An empty journal dir: read_journal globs *.jsonl and finds none."""
    os.makedirs(path, exist_ok=True)
    return path


if __name__ == "__main__":
    unittest.main()
