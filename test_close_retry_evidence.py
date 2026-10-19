"""Evidence-gated close retry (owner ruling 2026-10-19).

A close may be retried only when the failure is provably pre-submission, a
fresh scan shows the position still exists, and the retry carries a NEW
signal id. Ambiguous/unknown submission state stays in review regardless of
what the scan shows, and a cooldown never authorizes a retry by itself.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_cycle.close_settlement import (  # noqa: E402
    RETRY_COOLDOWN_SECONDS,
    classify_submission_state,
    derive_rejected_statuses,
    mark_emitted,
    position_close_pending,
    update_from_journal,
)

NOW = 1_792_400_000.0
PRE = "SOLANA_AGENT_WALLET not set"
AMB = "RPC timeout"


def _emitted(pool="P1", position="POS1", ts=NOW - 3600):
    state = {}
    mark_emitted(state, "sig-old", pool, now_ts=ts, position_id=position)
    return state


def _reject(state, reason, ts=NOW - 1800, sid="sig-old"):
    update_from_journal(
        state, {sid: {"status": "failed", "reason": reason,
                      "confirmed_at": ts, "pool_address": "P1"}},
        {"P1": True}, True)


class SubmissionStateTests(unittest.TestCase):
    def test_pre_submission_reasons(self):
        for reason in (PRE, "signal validation failed", "preflight rejected",
                       "slippage exceeded", "insufficient funds for fees",
                       "bin_step 3 below owner minimum 4"):
            self.assertEqual(classify_submission_state(reason),
                             "pre_submission", reason)

    def test_ambiguous_reasons(self):
        for reason in (AMB, "rate limit hit", "blockhash not found",
                       "fetch failed", "429", "network error"):
            self.assertEqual(classify_submission_state(reason),
                             "ambiguous", reason)

    def test_unknown_reasons(self):
        for reason in ("", None, "position not found", "weird failure"):
            self.assertEqual(classify_submission_state(reason), "unknown",
                             reason)


class RetryGateTests(unittest.TestCase):
    def _derive(self, state, present=True, now=NOW):
        derive_rejected_statuses(state, {"P1": present}, True, now=now)

    def test_pre_submission_with_position_present_and_cooldown_is_retryable(self):
        state = _emitted()
        _reject(state, PRE, ts=NOW - 3600)
        self._derive(state)
        self.assertEqual(state["sig-old"]["status"], "CLOSE_RETRY_ALLOWED")
        self.assertEqual(state["sig-old"]["submission_state"], "pre_submission")

    def test_ambiguous_with_position_present_is_still_review(self):
        # The core fix: a timeout may have landed. Position presence does not
        # make an ambiguous failure safe to retry.
        state = _emitted()
        _reject(state, AMB, ts=NOW - 3600)
        self._derive(state)
        self.assertEqual(state["sig-old"]["status"], "CLOSE_REVIEW_REQUIRED")
        self.assertEqual(state["sig-old"]["submission_state"], "ambiguous")

    def test_unknown_submission_state_is_review(self):
        state = _emitted()
        _reject(state, "position not found", ts=NOW - 3600)
        self._derive(state)
        self.assertEqual(state["sig-old"]["status"], "CLOSE_REVIEW_REQUIRED")

    def test_position_absent_is_review_even_if_pre_submission(self):
        state = _emitted()
        _reject(state, PRE, ts=NOW - 3600)
        self._derive(state, present=False)
        self.assertEqual(state["sig-old"]["status"], "CLOSE_REVIEW_REQUIRED")

    def test_cooldown_alone_does_not_authorize_retry(self):
        state = _emitted()
        _reject(state, PRE, ts=NOW - (RETRY_COOLDOWN_SECONDS - 60))
        self._derive(state)
        self.assertEqual(state["sig-old"]["status"], "CLOSE_REVIEW_REQUIRED")

    def test_cooldown_elapsed_still_requires_pre_submission(self):
        state = _emitted()
        _reject(state, AMB, ts=NOW - (RETRY_COOLDOWN_SECONDS * 10))
        self._derive(state)
        self.assertEqual(state["sig-old"]["status"], "CLOSE_REVIEW_REQUIRED")

    def test_stale_scan_blocks_retry(self):
        state = _emitted()
        _reject(state, PRE, ts=NOW - 3600)
        derive_rejected_statuses(state, {"P1": True}, False, now=NOW)
        self.assertEqual(state["sig-old"]["status"], "CLOSE_REVIEW_REQUIRED")


class NewSignalIdTests(unittest.TestCase):
    def test_retry_allowed_does_not_suppress_a_new_close(self):
        # Before the fix, position_close_pending() treated the retry state as
        # "in flight", making the evidence-gated retry unreachable.
        state = _emitted()
        _reject(state, PRE, ts=NOW - 3600)
        derive_rejected_statuses(state, {"P1": True}, True, now=NOW)
        self.assertEqual(state["sig-old"]["status"], "CLOSE_RETRY_ALLOWED")
        self.assertIsNone(position_close_pending(state, "P1", "POS1"))

    def test_in_flight_and_review_still_suppress(self):
        for reason, present in ((None, True), (AMB, True)):
            state = _emitted()
            if reason:
                _reject(state, reason, ts=NOW - 3600)
                derive_rejected_statuses(state, {"P1": present}, True, now=NOW)
            self.assertEqual(position_close_pending(state, "P1", "POS1"),
                             "sig-old", reason)

    def test_new_emit_uses_new_id_and_supersedes_old(self):
        state = _emitted()
        _reject(state, PRE, ts=NOW - 3600)
        derive_rejected_statuses(state, {"P1": True}, True, now=NOW)
        mark_emitted(state, "sig-new", "P1", now_ts=NOW,
                     position_id="POS1")
        self.assertEqual(state["sig-old"]["status"], "CLOSE_RESOLVED")
        self.assertEqual(state["sig-old"]["superseded_by"], "sig-new")
        self.assertEqual(state["sig-new"]["status"], "CLOSE_EMITTED")
        self.assertNotEqual("sig-new", "sig-old")

    def test_review_required_is_never_auto_superseded(self):
        state = _emitted()
        _reject(state, AMB, ts=NOW - 3600)
        derive_rejected_statuses(state, {"P1": True}, True, now=NOW)
        mark_emitted(state, "sig-new", "P1", now_ts=NOW,
                     position_id="POS1")
        self.assertEqual(state["sig-old"]["status"], "CLOSE_REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
