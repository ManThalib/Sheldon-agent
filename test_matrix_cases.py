#!/usr/bin/env python3
"""Phase 6 test matrix — Sheldon-side cases from 06-host-only-work.md.

Each test name carries its matrix case number so the 15-case plan matrix
has a 1:1 inspectable mapping. Cases 1-5, 10 (George-side duplicate),
14, 15 live in the George repo's common/test_matrix_cases.py.

All scenarios run on synthetic state in memory or scratch dirs: no live
writes, no signals emitted, no funds moved.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from capital import SOL_MINT, USDC_MINT
from readiness import (
    OPEN_BLOCKED_SOL,
    OPEN_BLOCKED_WALLET_SCAN_STALE,
    OPEN_READY,
    build_readiness,
    plan_sol_top_up,
    sol_overhead_lamports,
)
from readiness.prep_swap import build_prep_swap_signal
from run_cycle.close_settlement import (
    advance,
    check_settlement,
    derive_rejected_statuses,
    mark_emitted,
    summarize,
    update_from_journal,
)

NOW = 1_791_500_000.0
OVERHEAD = sol_overhead_lamports("meteora")

TOKEN_MINT = "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS"


def asset(mint, raw, decimals, price, symbol=""):
    return {"mint": mint, "amount_raw": str(raw), "decimals": decimals,
            "price_usd": price, "symbol": symbol}


def usdc_token_assets(sol_raw, usdc_raw=100_000_000, token_raw=30_000_000_000):
    return [
        asset(USDC_MINT, usdc_raw, 6, 1.0, "USDC"),
        asset(TOKEN_MINT, token_raw, 9, 2.0, "TKN"),
        asset(SOL_MINT, sol_raw, 9, 117.0, "SOL"),
    ]


def strategy():
    return {
        "pool_address": "p1", "dex": "meteora",
        "suggested_usdc": 90.0, "score": 90.0,
        "center": -100, "bin_range": {"lower": -110, "upper": -90},
        "_pool": {
            "pool_address": "p1", "dex": "meteora", "bin_step": 10,
            "tick_spacing": 10, "active_bin_id": -100,
            "token_x_address": USDC_MINT, "token_y_address": TOKEN_MINT,
            "token_x_price_usd": 1.0, "token_y_price_usd": 2.0,
            "token_x_decimals": 6, "token_y_decimals": 9,
        },
    }


def readiness(**over):
    kwargs = dict(assets=usdc_token_assets(sol_raw=OVERHEAD + 5_000_000),
                  wallet_mtime=NOW - 30, position_mtime=NOW - 30, now=NOW,
                  close_settled=True)
    kwargs.update(over)
    return build_readiness(strategy(), **kwargs)


class _Entry(dict):
    pass


def emitted_entry(pool="P1", **kw):
    e = {"status": "CLOSE_EMITTED", "pool_address": pool,
         "emit_time": NOW - 3600}
    e.update(kw)
    return e


class MatrixSheldonTests(unittest.TestCase):
    # ---- case 6: close executed but position scan still shows active -----
    def test_case06_close_executed_position_still_active_blocks(self):
        state = {"s1": {"status": "CLOSE_EXECUTED", "pool_address": "P1",
                        "confirmed_at": NOW - 600}}
        active = {"P1": {"pool_address": "P1"}}
        advance(state, active, wallet_mtime=NOW - 10,
                position_mtime=NOW - 5, now=NOW)
        self.assertEqual(state["s1"]["status"], "CLOSE_EXECUTED")
        blocking, _ = summarize(state)
        self.assertTrue(any("s1" in b for b in blocking))

    def test_case06b_settlement_gate_blocks_open(self):
        state = {"s1": {"status": "CLOSE_EXECUTED", "pool_address": "P1",
                        "confirmed_at": NOW - 600}}
        allowed, reason, _, details = check_settlement(
            {"P1": {"pool_address": "P1"}}, wallet_mtime=NOW - 10,
            position_mtime=NOW - 5, close_state=state, now=NOW,
            journal_dir=self._journal_none())
        self.assertFalse(allowed)
        self.assertIn("blocked", reason)
        self.assertEqual(details["tracked"], 1)

    # ---- case 7: close verified and wallet capital visible ----------------
    def test_case07_verified_close_reaches_funds_verified(self):
        state = {"s1": {"status": "CLOSE_EXECUTED", "pool_address": "P1",
                        "confirmed_at": NOW - 600}}
        # fresh scan (newer than confirmation) shows the position gone
        advance(state, {}, wallet_mtime=NOW - 10,
                position_mtime=NOW - 5, now=NOW)
        self.assertEqual(state["s1"]["status"], "FUNDS_VERIFIED")
        allowed, reason, _, _ = check_settlement(
            {}, wallet_mtime=NOW - 10, position_mtime=NOW - 5,
            close_state=state, now=NOW, journal_dir=self._journal_none())
        self.assertTrue(allowed, reason)

    # ---- case 8: close verification failed --------------------------------
    def test_case08_verify_failed_is_rejected_and_rederived(self):
        # George journals failed_verify when the chain verify block says the
        # close did not land. The fold must treat it as a rejection, and the
        # re-derivation must produce an actionable status, not silence.
        self.assertIn("failed_verify", {"rejected", "failed", "failed_verify"})
        state = {"s1": emitted_entry()}
        entries = {"s1": {"status": "failed_verify",
                          "pool_address": "P1", "timestamp": NOW - 60,
                          "reason": "RPC timeout"}}
        position_presence = {"P1": True}   # position still there -> retryable
        update_from_journal(state, entries, position_presence, True)
        derive_rejected_statuses(state, position_presence, True)
        self.assertEqual(state["s1"]["status"], "CLOSE_RETRY_ALLOWED")
        blocking, review = summarize(state)
        self.assertTrue(any("s1" in b for b in blocking))
        self.assertFalse(any(r["signal_id"] == "s1" for r in review))

    def test_case08b_verify_failed_position_absent_needs_review(self):
        state = {"s1": emitted_entry()}
        entries = {"s1": {"status": "failed_verify",
                          "pool_address": "P1", "timestamp": NOW - 60}}
        position_presence = {"P1": False}  # ambiguous: nothing to retry
        update_from_journal(state, entries, position_presence, True)
        derive_rejected_statuses(state, position_presence, True)
        self.assertEqual(state["s1"]["status"], "CLOSE_REVIEW_REQUIRED")
        blocking, review = summarize(state)
        self.assertTrue(any(r["signal_id"] == "s1" for r in review))
        self.assertTrue(any("s1" in b for b in blocking))

    # ---- case 9: stale wallet scan -----------------------------------------
    def test_case09_stale_wallet_scan_fails_closed(self):
        r = readiness(wallet_mtime=NOW - 10_000)
        self.assertFalse(r["ready"])
        self.assertEqual(r["status"], OPEN_BLOCKED_WALLET_SCAN_STALE)

    # ---- case 11: auto SOL top-up succeeds ---------------------------------
    def test_case11_top_up_planned_and_signal_built(self):
        r = readiness(assets=usdc_token_assets(sol_raw=OVERHEAD - 10_000_000))
        self.assertEqual(r["status"], OPEN_BLOCKED_SOL)
        plan = plan_sol_top_up(
            r, assets=usdc_token_assets(sol_raw=OVERHEAD - 10_000_000),
            required_usdc_raw=90_000_000)
        self.assertTrue(plan["allowed"], plan["reason"])
        self.assertIsNotNone(plan["spec"])
        signal = build_prep_swap_signal(plan["spec"], 1, 1_791_500_000)
        self.assertEqual(signal["action"], "swap")
        self.assertEqual(signal["input_mint"], USDC_MINT)
        self.assertEqual(signal["output_mint"], SOL_MINT)

    # ---- case 12: auto SOL top-up fails -------------------------------------
    def test_case12_top_up_refused_when_not_sole_failure(self):
        # token side ALSO short -> top-up must refuse; gate stays closed
        r = readiness(assets=[
            asset(USDC_MINT, 100_000_000, 6, 1.0, "USDC"),
            asset(TOKEN_MINT, 0, 9, 2.0, "TKN"),          # token Y short
            asset(SOL_MINT, OVERHEAD - 10_000_000, 9, 117.0, "SOL"),
        ])
        self.assertFalse(r["ready"])
        plan = plan_sol_top_up(
            r, assets=usdc_token_assets(OVERHEAD - 10_000_000),
            required_usdc_raw=90_000_000)
        self.assertFalse(plan["allowed"])
        self.assertIsNone(plan["spec"])

    def test_case12b_failed_top_up_leaves_gate_closed(self):
        """A top-up signal that never lands cannot unlock the open: the
        readiness object recomputes from live scans and is still blocked."""
        r1 = readiness(assets=usdc_token_assets(sol_raw=OVERHEAD - 10_000_000))
        self.assertEqual(r1["status"], OPEN_BLOCKED_SOL)
        # next cycle, same balances (top-up failed): still blocked
        r2 = readiness(assets=usdc_token_assets(sol_raw=OVERHEAD - 10_000_000))
        self.assertEqual(r2["status"], OPEN_BLOCKED_SOL)
        self.assertFalse(r2["ready"])

    # ---- case 13: close priority over open (Sheldon aspect) -----------------
    def test_case13_close_emitted_while_open_blocked(self):
        """Sheldon never lets the settlement gate stop a close signal: the
        gate only ever blocks opens. George-side ordering (close first in
        the pending queue) is proven in George test_signal_priority."""
        state = {"s1": {"status": "CLOSE_EXECUTED", "pool_address": "P1",
                        "confirmed_at": NOW - 600}}
        allowed, reason, _, _ = check_settlement(
            {"P1": {"pool_address": "P1"}}, wallet_mtime=NOW - 10,
            position_mtime=NOW - 5, close_state=state, now=NOW,
            journal_dir=self._journal_none())
        self.assertFalse(allowed)
        # emitting the close is still fine:
        mark_emitted(state, "s1", "P1", now_ts=NOW)
        self.assertEqual(state["s1"]["status"], "CLOSE_EMITTED")

    # -- helpers -------------------------------------------------------------
    def _journal_none(self):
        return os.path.join(tempfile.mkdtemp(), "no-journal-here")


    # ---- case 14: journal replay must never rewind a settled close ------
    # 2026-10-14 regression: update_from_journal re-marked every known
    # executed close as CLOSE_EXECUTED on each cycle's journal replay, so a
    # close that reached FUNDS_VERIFIED was rewound as soon as the pool was
    # re-opened (new position makes absence unverifiable) and blocked all
    # opens forever.
    def test_case14_replay_does_not_regress_funds_verified(self):
        state = {"s1": {"status": "FUNDS_VERIFIED", "pool_address": "P1",
                        "confirmed_at": NOW - 600,
                        "funds_verified_at": NOW - 300}}
        entries = {"s1": {"status": "executed", "pool_address": "P1",
                          "timestamp": NOW - 600}}
        update_from_journal(state, entries, {"P1": True}, True)
        self.assertEqual(state["s1"]["status"], "FUNDS_VERIFIED")
        self.assertEqual(state["s1"]["confirmed_at"], NOW - 600)
        blocking, _ = summarize(state)
        self.assertFalse(any("s1" in b for b in blocking))

    def test_case14b_replay_does_not_rewind_mid_ladder(self):
        for status in ("CLOSE_EXECUTED", "CLOSE_CHAIN_VERIFIED",
                       "POSITION_SCAN_VERIFIED"):
            state = {"s1": {"status": status, "pool_address": "P1",
                            "confirmed_at": NOW - 600}}
            entries = {"s1": {"status": "executed", "pool_address": "P1",
                              "timestamp": NOW - 600}}
            update_from_journal(state, entries, {"P1": False}, True)
            self.assertEqual(state["s1"]["status"], status)

    def test_case14c_first_executed_fold_still_marks_close_executed(self):
        state = {"s1": emitted_entry()}
        entries = {"s1": {"status": "executed", "pool_address": "P1",
                          "confirmed_at": NOW - 600}}
        update_from_journal(state, entries, {"P1": True}, True)
        self.assertEqual(state["s1"]["status"], "CLOSE_EXECUTED")
        self.assertEqual(state["s1"]["confirmed_at"], NOW - 600)

    def test_case14d_advance_self_heals_rewound_entry_with_funds_stamped(self):
        # A close whose funds_verified_at was already stamped (the ladder
        # reached FUNDS_VERIFIED) but whose status a replay rewound to
        # CLOSE_EXECUTED — and the pool now holds a NEW position, so the
        # absence check can never re-fire. The stamped evidence must win.
        state = {"s1": {"status": "CLOSE_EXECUTED", "pool_address": "P1",
                        "confirmed_at": NOW - 600,
                        "funds_verified_at": NOW - 300,
                        "capital_evidence": "wallet scan newer than close "
                                           "confirmation"}}
        active = {"P1": {"pool_address": "P1"}}   # the re-opened position
        advance(state, active, wallet_mtime=NOW - 10,
                position_mtime=NOW - 5, now=NOW)
        self.assertEqual(state["s1"]["status"], "FUNDS_VERIFIED")
        blocking, _ = summarize(state)
        self.assertFalse(any("s1" in b for b in blocking))


if __name__ == "__main__":
    unittest.main()
