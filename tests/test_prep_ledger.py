#!/usr/bin/env python3
"""Tests for the prep swap ledger (state-based dedupe) and anti-oscillation.

Covers the repeating CAPITAL PREP loop: an unmet open re-emits the same buy
every cycle, and the token it buys is sold back as "surplus" the next cycle.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from capital import SOL_MINT, USDC_MINT
from readiness import (
    PREP_CONFIRM_GRACE_SECONDS,
    gate_prep_swaps,
    plan_funding,
    prep_ledger,
)

TZ = timezone(timedelta(hours=8))
MINT_A = "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS"  # the real bought mint


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def buy_spec(mint=MINT_A, pool="poolX", cost_raw="15000000"):
    return {
        "direction": "buy", "for_pool": pool,
        "input_mint": USDC_MINT, "output_mint": mint,
        "amount_raw": cost_raw, "usd": 15.0,
        "need_ui": 0.011, "have_ui": 0.0, "symbol": "TKN",
    }


def sell_spec(mint=MINT_A, raw="975250"):
    return {
        "direction": "sell", "for_pool": None,
        "input_mint": mint, "output_mint": USDC_MINT,
        "amount_raw": raw, "usd": 15.39,
        "need_ui": 0.0, "have_ui": 0.011, "symbol": "TKN",
    }


class LedgerUnitTests(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def test_key_includes_pool_and_direction(self):
        self.assertNotEqual(
            prep_ledger.ledger_key(buy_spec(pool="a")),
            prep_ledger.ledger_key(buy_spec(pool="b")),
        )
        self.assertNotEqual(
            prep_ledger.ledger_key(buy_spec()),
            prep_ledger.ledger_key(sell_spec()),
        )

    def test_record_and_load(self):
        prep_ledger.record_emitted(buy_spec(), "sheldon-prep-1-1",
                                   state_dir=self.state_dir, now=1000.0)
        led = prep_ledger.load_ledger(self.state_dir)
        self.assertEqual(len(led["entries"]), 1)
        self.assertEqual(led["entries"][0]["status"], "pending")
        self.assertEqual(led["entries"][0]["signal_id"], "sheldon-prep-1-1")

    def test_missing_ledger_fails_open(self):
        led = prep_ledger.load_ledger(self.state_dir)
        self.assertEqual(led, {"entries": []})

    def test_corrupt_ledger_fails_open(self):
        with open(prep_ledger.state_path(self.state_dir), "w") as fh:
            fh.write("{not json")
        self.assertEqual(prep_ledger.load_ledger(self.state_dir), {"entries": []})

    def test_pending_suppresses_within_ttl(self):
        prep_ledger.record_emitted(buy_spec(), "sid", state_dir=self.state_dir, now=1000.0)
        entry = prep_ledger.newest_entry(prep_ledger.load_ledger(self.state_dir),
                                         prep_ledger.ledger_key(buy_spec()))
        reason = prep_ledger.suppression_reason(entry, wallet_mtime=2000.0,
                                                now=1000.0 + 60)
        self.assertIn("pending", reason)

    def test_pending_expires_after_ttl(self):
        prep_ledger.record_emitted(buy_spec(), "sid", state_dir=self.state_dir, now=1000.0)
        entry = prep_ledger.newest_entry(prep_ledger.load_ledger(self.state_dir),
                                         prep_ledger.ledger_key(buy_spec()))
        now = 1000.0 + prep_ledger.PREP_LEDGER_TTL_SECONDS + 1
        reason = prep_ledger.suppression_reason(entry, wallet_mtime=now, now=now)
        self.assertIsNone(reason)
        self.assertEqual(entry["status"], "expired")

    def test_executed_blocks_until_scan_after_grace(self):
        entry = {"status": "executed", "signal_id": "sid",
                 "emitted_at": 1000.0, "confirmed_at": 1000.0}
        # scan taken before confirmation + grace -> block
        self.assertIn("grace", prep_ledger.suppression_reason(
            entry, wallet_mtime=1000.0 + 10, now=1100.0))
        # scan taken after confirmation + grace -> allow
        self.assertIsNone(prep_ledger.suppression_reason(
            entry, wallet_mtime=1000.0 + PREP_CONFIRM_GRACE_SECONDS + 5, now=1100.0))

    def test_rejected_cooldown(self):
        entry = {"status": "rejected", "signal_id": "sid",
                 "emitted_at": 1000.0, "confirmed_at": 1000.0}
        self.assertIn("rejected", prep_ledger.suppression_reason(
            entry, wallet_mtime=2000.0, now=1000.0 + 60))
        self.assertIsNone(prep_ledger.suppression_reason(
            entry, wallet_mtime=2000.0,
            now=1000.0 + prep_ledger.PREP_REJECT_COOLDOWN_SECONDS + 1))

    def test_recent_buy_mints(self):
        led = {"entries": [
            {"direction": "buy", "status": "executed", "output_mint": MINT_A,
             "confirmed_at": 1000.0, "emitted_at": 1000.0},
            {"direction": "buy", "status": "executed", "output_mint": "OLD",
             "confirmed_at": 10.0, "emitted_at": 10.0},
            {"direction": "sell", "status": "executed", "output_mint": USDC_MINT,
             "confirmed_at": 1000.0, "emitted_at": 1000.0},
        ]}
        mints = prep_ledger.recent_buy_mints(led, now=4000.0, window=3600.0)
        self.assertEqual(mints, {MINT_A})

    def test_resolve_from_journal_parses_amounts(self):
        journal_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, journal_dir, ignore_errors=True)
        prep_ledger.record_emitted(buy_spec(), "sheldon-prep-9-1",
                                   state_dir=self.state_dir, now=1000.0)
        rec = {
            "timestamp": iso(1010.0), "signal_id": "sheldon-prep-9-1",
            "action": "swap", "decision": "executed",
            "details": {"result": {"notes": "jupiter ExactIn in=USDC out=TKN "
                                             "inAmount=12885823 outAmount=975250 "
                                             "minOut=965498"}},
        }
        with open(os.path.join(journal_dir, "2026-10-05.jsonl"), "w") as fh:
            fh.write(json.dumps(rec) + "\n")
        led = prep_ledger.load_and_resolve(self.state_dir, journal_dir)
        e = led["entries"][0]
        self.assertEqual(e["status"], "executed")
        self.assertEqual(e["in_amount"], "12885823")
        self.assertEqual(e["out_amount"], "975250")  # partial-fill truth
        self.assertAlmostEqual(e["confirmed_at"], 1010.0)

    def test_resolve_rejected(self):
        journal_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, journal_dir, ignore_errors=True)
        prep_ledger.record_emitted(buy_spec(), "sheldon-prep-9-2",
                                   state_dir=self.state_dir, now=1000.0)
        rec = {"timestamp": iso(1010.0), "signal_id": "sheldon-prep-9-2",
               "action": "swap", "decision": "rejected",
               "details": {"stage": "simulate", "error": "simulation failed"}}
        with open(os.path.join(journal_dir, "2026-10-05.jsonl"), "w") as fh:
            fh.write(json.dumps(rec) + "\n")
        led = prep_ledger.load_and_resolve(self.state_dir, journal_dir)
        self.assertEqual(led["entries"][0]["status"], "rejected")


    def test_read_journal_surfaces_failed_close(self):
        journal_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, journal_dir, ignore_errors=True)
        rec = {"timestamp": iso(1020.0), "signal_id": "sheldon-rot-1-1",
               "action": "close", "decision": "failed",
               "details": {"stage": "send", "error": "blockhash expired"}}
        with open(os.path.join(journal_dir, "2026-10-05.jsonl"), "w") as fh:
            fh.write(json.dumps(rec) + "\n")
        out = prep_ledger.read_journal(journal_dir)
        self.assertEqual(out["sheldon-rot-1-1"]["status"], "failed")
        self.assertIn("blockhash expired", out["sheldon-rot-1-1"]["reason"])
        self.assertAlmostEqual(out["sheldon-rot-1-1"]["confirmed_at"], 1020.0)

    def test_read_journal_keeps_newest_decision_per_signal(self):
        journal_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, journal_dir, ignore_errors=True)
        path = os.path.join(journal_dir, "2026-10-05.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"timestamp": iso(1000.0),
                                 "signal_id": "sheldon-rot-2-1",
                                 "decision": "failed"}) + "\n")
            fh.write(json.dumps({"timestamp": iso(1100.0),
                                 "signal_id": "sheldon-rot-2-1",
                                 "decision": "executed"}) + "\n")
        out = prep_ledger.read_journal(journal_dir)
        self.assertEqual(out["sheldon-rot-2-1"]["status"], "executed")

    def test_failed_prep_cools_down_like_rejection(self):
        now = time.time()
        prep_ledger.record_emitted(buy_spec(), "sheldon-prep-9-3",
                                   state_dir=self.state_dir, now=1000.0)
        journal_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, journal_dir, ignore_errors=True)
        with open(os.path.join(journal_dir, "2026-10-05.jsonl"), "w") as fh:
            fh.write(json.dumps({"timestamp": iso(now - 10.0),
                                 "signal_id": "sheldon-prep-9-3",
                                 "decision": "failed",
                                 "details": {"error": "rpc timeout"}}) + "\n")
        led = prep_ledger.load_and_resolve(self.state_dir, journal_dir)
        self.assertEqual(led["entries"][0]["status"], "failed")
        reason = prep_ledger.suppression_reason(led["entries"][0],
                                                wallet_mtime=now - 5, now=now)
        self.assertIsNotNone(reason)
        self.assertIn("failed", reason)


class GateLedgerTests(unittest.TestCase):
    """gate_prep_swaps with the ledger enabled (state_dir set)."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.pending = os.path.join(self.root, "pending")
        self.processed = os.path.join(self.root, "processed")
        self.state_dir = os.path.join(self.root, "state")
        self.journal_dir = os.path.join(self.root, "journal")
        for d in (self.pending, self.processed, self.state_dir, self.journal_dir):
            os.makedirs(d)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _journal(self, signal_id, decision, epoch):
        rec = {"timestamp": iso(epoch), "signal_id": signal_id,
               "action": "swap", "decision": decision,
               "details": {"result": {"notes": "inAmount=100 outAmount=200"}}}
        path = os.path.join(self.journal_dir, "2026-10-05.jsonl")
        with open(path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _gate(self, wallet_mtime, now):
        return gate_prep_swaps([buy_spec()], self.pending, wallet_mtime,
                               now=now, state_dir=self.state_dir,
                               journal_dir=self.journal_dir)

    def test_pending_suppresses_reemit(self):
        now = time.time()
        prep_ledger.record_emitted(buy_spec(), "sheldon-prep-1-1",
                                   state_dir=self.state_dir, now=now - 60)
        allowed, blocked = self._gate(wallet_mtime=now - 5, now=now)
        self.assertEqual(allowed, [])
        self.assertIn("pending", blocked[0]["reason"])

    def test_executed_before_scan_grace_blocks(self):
        now = time.time()
        prep_ledger.record_emitted(buy_spec(), "sheldon-prep-2-1",
                                   state_dir=self.state_dir, now=now - 120)
        self._journal("sheldon-prep-2-1", "executed", now - 110)
        # scan taken 10s after confirmation -> still inside grace
        allowed, blocked = self._gate(wallet_mtime=now - 100, now=now)
        self.assertEqual(allowed, [])
        self.assertIn("grace", blocked[0]["reason"])

    def test_executed_scan_after_grace_allows(self):
        now = time.time()
        prep_ledger.record_emitted(buy_spec(), "sheldon-prep-3-1",
                                   state_dir=self.state_dir, now=now - 300)
        self._journal("sheldon-prep-3-1", "executed", now - 290)
        # fresh scan well after confirmation -> shortfall genuinely persists
        allowed, blocked = self._gate(wallet_mtime=now - 5, now=now)
        self.assertEqual(len(allowed), 1, blocked)
        self.assertEqual(blocked, [])

    def test_rejected_cools_down_then_retries(self):
        now = time.time()
        prep_ledger.record_emitted(buy_spec(), "sheldon-prep-4-1",
                                   state_dir=self.state_dir, now=now - 300)
        self._journal("sheldon-prep-4-1", "rejected", now - 290)
        allowed, blocked = self._gate(wallet_mtime=now - 5, now=now)
        self.assertEqual(allowed, [])
        self.assertIn("rejected", blocked[0]["reason"])
        # after the cooldown, a retry is allowed (scan must also be recent)
        later_now = now + prep_ledger.PREP_REJECT_COOLDOWN_SECONDS + 1
        allowed2, blocked2 = self._gate(
            wallet_mtime=later_now - 5, now=later_now)
        self.assertEqual(len(allowed2), 1, blocked2)

    def test_ledger_disabled_when_state_dir_none(self):
        now = time.time()
        allowed, blocked = gate_prep_swaps([buy_spec()], self.pending,
                                           wallet_mtime=now - 5, now=now)
        self.assertEqual(len(allowed), 1)
        self.assertEqual(blocked, [])


class AntiOscillationTests(unittest.TestCase):
    """plan_funding must not sell back a token just bought for an open."""

    def _strategy(self, pool="poolX"):
        return {
            "pool_address": pool, "score": 90.0, "dex": "meteora",
            "suggested_usdc": 30.0,
            "_pool": {
                "token_x_address": USDC_MINT, "token_x_price_usd": 1.0,
                "token_x_decimals": 6, "token_y_address": MINT_A,
                "token_y_price_usd": 1300.0, "token_y_decimals": 9,
            },
        }

    def _assets(self):
        return [
            {"mint": USDC_MINT, "amount_raw": 35_000_000, "amount_ui": 35.0,
             "decimals": 6, "price_usd": 1.0, "symbol": "USDC"},
            {"mint": MINT_A, "amount_raw": 14_000_000, "amount_ui": 0.014,
             "decimals": 9, "price_usd": 1300.0, "symbol": "TKN"},
            {"mint": SOL_MINT, "amount_raw": 100_000_000, "amount_ui": 0.1,
             "decimals": 9, "price_usd": 150.0, "symbol": "SOL"},
        ]

    def test_recent_buy_holds_surplus(self):
        # Everything selected is funded -> the surplus-sell branch would fire.
        plan = plan_funding([self._strategy()], self._assets(),
                            recent_buy_mints={MINT_A})
        held = [s for s in plan["prep_swaps"]
                if s["direction"] == "sell" and s["input_mint"] == MINT_A]
        self.assertEqual(held, [])
        self.assertTrue(any("anti-oscillation" in n for n in plan["notes"]))

    def test_without_recent_buy_surplus_sells(self):
        plan = plan_funding([self._strategy()], self._assets(),
                            recent_buy_mints=set())
        sells = [s for s in plan["prep_swaps"]
                 if s["direction"] == "sell" and s["input_mint"] == MINT_A]
        self.assertEqual(len(sells), 1)


class OscillatorReplayTests(unittest.TestCase):
    """End-to-end replay of the real 2026-10-05 buy -> sell churn."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.pending = os.path.join(self.root, "pending")
        self.processed = os.path.join(self.root, "processed")
        self.state_dir = os.path.join(self.root, "state")
        self.journal_dir = os.path.join(self.root, "journal")
        for d in (self.pending, self.processed, self.state_dir, self.journal_dir):
            os.makedirs(d)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_second_buy_and_immediate_sell_suppressed(self):
        t0 = time.time() - 7200

        # --- Cycle 1: unfunded open -> prep buy emitted and recorded.
        prep_ledger.record_emitted(buy_spec(), "sheldon-prep-1791189803-1",
                                   state_dir=self.state_dir, now=t0)
        # George executes it 2s later.
        with open(os.path.join(self.journal_dir, "2026-10-05.jsonl"), "w") as fh:
            fh.write(json.dumps({
                "timestamp": iso(t0 + 2), "signal_id": "sheldon-prep-1791189803-1",
                "action": "swap", "decision": "executed",
                "details": {"result": {"notes": "inAmount=15463216 outAmount=975250"}},
            }) + "\n")

        # --- Cycle 2 (a few seconds later): Sheldon re-derives the same buy.
        # The gate must suppress it (executed, scan not past grace yet).
        allowed, blocked = gate_prep_swaps(
            [buy_spec()], self.pending, wallet_mtime=t0 + 3, now=t0 + 5,
            state_dir=self.state_dir, journal_dir=self.journal_dir)
        self.assertEqual(allowed, [], "duplicate buy must be suppressed")
        self.assertIn("grace", blocked[0]["reason"])

        # --- Cycle 3: the token is now present, so all strategies are funded
        # and the surplus-sell branch would fire. Anti-oscillation holds it.
        led = prep_ledger.load_and_resolve(self.state_dir, self.journal_dir)
        recent = prep_ledger.recent_buy_mints(led, now=t0 + 60, window=3600.0)
        self.assertIn(MINT_A, recent)
        plan = plan_funding(
            [{"pool_address": "poolX", "score": 90.0, "dex": "meteora",
              "suggested_usdc": 30.0,
              "_pool": {"token_x_address": USDC_MINT, "token_x_price_usd": 1.0,
                        "token_x_decimals": 6, "token_y_address": MINT_A,
                        "token_y_price_usd": 1300.0, "token_y_decimals": 9}}],
            [{"mint": USDC_MINT, "amount_raw": 5_000_000, "amount_ui": 5.0,
              "decimals": 6, "price_usd": 1.0, "symbol": "USDC"},
             {"mint": MINT_A, "amount_raw": 11_000_000, "amount_ui": 0.011,
              "decimals": 9, "price_usd": 1300.0, "symbol": "TKN"}],
            recent_buy_mints=recent)
        self.assertEqual([s for s in plan["prep_swaps"] if s["direction"] == "sell"], [])

        # --- Cycle 4: after the window expires, the surplus sell is allowed
        # again (a genuine long-lived surplus must not be held forever).
        later = prep_ledger.recent_buy_mints(led, now=t0 + 3700, window=3600.0)
        self.assertEqual(later, set())


if __name__ == "__main__":
    unittest.main()
