"""Tests for the idle-capital sweep (idle_sweep.py, 2026-10-01).

Covers: idle window math (skip >= idle_max, skip < min_add), committed
capital scan (opens, prep swaps, prior sweeps), headroom cap, Y-side-USDC
guard, y-side room guard, cooldown, day cap, staleness guard, candidate
ranking (opens win capital, floor score), reserve-then-confirm lifecycle,
reservation release via George's queues, and atomic write failure.

Hermetic: tempdirs for all state/signals; no RPC, no live queues.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import idle_sweep
from idle_sweep import (
    execute_sweep,
    plan_sweep,
    release_confirmed_reservation,
    scan_committed_usdc,
    select_add_candidate,
    _y_side_room_ok,
)

POLICY = {
    "add_enabled": True,
    "add_idle_max_usd": 20.0,
    "add_min_usd": 5.0,
    "add_cooldown_hours": 6.0,
    "add_max_per_day": 6,
    "add_y_side_room_pct": 25.0,
    "add_max_wallet_scan_age_seconds": 900.0,
    "default_max_slippage_bps": 100,
    "min_open_score": 70.0,
    "default_max_position_usd": 100.0,
}

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
POOL_A = "BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y"   # tracked, in range
POOL_B = "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"  # tracked, in range
POS_A = "43ivjtQ7s8AweC2suoAULYAhPVtQRapAfsahdXDDs1QM"
POS_B = "ECqCpiyfrkBouNmxAhXxPsQpdH9XTJN2sByR16TDjokY"
NOW = 1_790_000_000.0


def _load(path):
    with open(path) as fh:
        return json.load(fh)


class SweepTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = self._tmp.name
        self.signals_dir = os.path.join(self._tmp.name, "signals", "pending")
        os.makedirs(self.signals_dir, exist_ok=True)
        self.addCleanup(self._tmp.cleanup)

    def _tracked(self, pool=POOL_A, pos=POS_A, value=44.0, lower=-2156,
                 upper=-2108, active=-2120, dex="meteora"):
        return {
            "action": "HOLD",
            "pool_address": pool,
            "position": pos,
            "dex": dex,
            "lower_bound": lower,
            "upper_bound": upper,
            "tracked_value_usd": value,
            "_pool": {
                "pool_address": pool,
                "dex": dex,
                "active_bin_id": active,
                "token_y_address": USDC_MINT,
                "bin_step": 10,
            },
        }

    def _report(self, idle=8.0, tracked=None, pool_scores=None):
        return {
            "wallet": {"idle_usdc": idle},
            "verdicts": tracked if tracked is not None else [self._tracked()],
            "pool_scores": pool_scores or [],
        }

    def _plan(self, report, mtime=None, state=None):
        return plan_sweep(
            report, self.signals_dir,
            mtime if mtime is not None else NOW,
            state if state is not None else {},
            state_dir=self.state_dir,
            now=NOW,
        )


class TestIdleWindow(SweepTestBase):
    def test_emit_in_window(self):
        signal, info = self._plan(self._report(idle=8.0))
        self.assertIsNotNone(signal, info)
        self.assertEqual(signal["action"], "add_liquidity")
        self.assertEqual(signal["position_usd"], 8.0)
        self.assertEqual(signal["liquidity"], {"amount_x": "0",
                                               "amount_y": "8000000"})

    def test_skip_idle_at_or_above_max(self):
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            signal, info = self._plan(self._report(idle=20.0))
        self.assertIsNone(signal)
        self.assertIn("idle_max_usd", info["reason"])

    def test_skip_idle_below_min(self):
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            signal, info = self._plan(self._report(idle=4.99))
        self.assertIsNone(signal)
        self.assertIn("min_add_usd", info["reason"])

    def test_idle_math_nets_queued_commits(self):
        # Wallet shows 28 USDC but a pending open already claims 20.
        with open(os.path.join(self.signals_dir, "sheldon-1-2.json"), "w") as fh:
            json.dump({"signal_id": "sheldon-1-2", "action": "open",
                       "position_usd": 20.0,
                       "created_at": "2026-10-01T00:00:00+08:00"}, fh)
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            signal, info = self._plan(self._report(idle=28.0))
        self.assertIsNotNone(signal)
        self.assertAlmostEqual(signal["position_usd"], 8.0)
        self.assertAlmostEqual(info["queued_committed_usdc"], 20.0)


class TestCommittedScan(SweepTestBase):
    def test_open_prep_and_sweep_all_count(self):
        with open(os.path.join(self.signals_dir, "sheldon-1-1.json"), "w") as fh:
            json.dump({"action": "open", "position_usd": 21.0,
                       "created_at": "2026-10-01T00:00:00+08:00"}, fh)
        with open(os.path.join(self.signals_dir, "sheldon-prep-2-1.json"), "w") as fh:
            json.dump({"action": "swap", "input_mint": USDC_MINT,
                       "amount": "7000000",
                       "created_at": "2026-10-01T00:00:00+08:00"}, fh)
        processed = os.path.join(os.path.dirname(self.signals_dir), "processed")
        os.makedirs(processed, exist_ok=True)
        with open(os.path.join(processed, "sheldon-sweep-3.json"), "w") as fh:
            json.dump({"action": "add_liquidity", "position_usd": 6.0,
                       "created_at": "2026-10-01T00:00:00+08:00"}, fh)
        total, n = scan_committed_usdc(self.signals_dir, now=NOW)
        self.assertAlmostEqual(total, 34.0)
        self.assertEqual(n, 3)

    def test_sell_swaps_and_aged_signals_do_not_count(self):
        with open(os.path.join(self.signals_dir, "sheldon-prep-sell.json"), "w") as fh:
            json.dump({"action": "swap", "input_mint": "MINTX",
                       "amount": "999000000",
                       "created_at": "2026-10-01T00:00:00+08:00"}, fh)
        old_iso = time.strftime("%Y-%m-%dT%H:%M:%S+08:00",
                                time.localtime(NOW - 172800))
        with open(os.path.join(self.signals_dir, "sheldon-9-9.json"), "w") as fh:
            json.dump({"action": "open", "position_usd": 50.0,
                       "created_at": old_iso}, fh)
        total, n = scan_committed_usdc(self.signals_dir, now=NOW)
        self.assertAlmostEqual(total, 0.0)
        self.assertEqual(n, 0)


class TestCandidateSelection(SweepTestBase):
    def test_y_side_must_be_usdc(self):
        tracked = self._tracked()
        tracked["_pool"]["token_y_address"] = "NOTUSDC"
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            best, skipped = select_add_candidate([tracked], [], POLICY)
        self.assertIsNone(best)
        self.assertIn("not USDC", skipped[0]["reason"])

    def test_headroom_caps_target(self):
        tracked = self._tracked(value=97.0)  # headroom 3 < min_add 5
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            best, skipped = select_add_candidate([tracked], [], POLICY)
        self.assertIsNone(best)
        self.assertIn("headroom", skipped[0]["reason"])

    def test_y_side_room_guard(self):
        # Range [-2156,-2108): active at -2120 leaves only ~25%+ above;
        # active at -2112 leaves ~8% -> reject.
        ok, reason = _y_side_room_ok(-2156, -2108, -2112, 25.0)
        self.assertFalse(ok)
        ok, _ = _y_side_room_ok(-2156, -2108, -2127, 25.0)  # 41% above
        self.assertTrue(ok)
        ok, reason = _y_side_room_ok(-2156, -2108, 0, 25.0)
        self.assertFalse(ok)
        self.assertIn("unknown", reason)

    def test_unknown_bounds_rejected(self):
        tracked = self._tracked()
        tracked["lower_bound"] = None
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            best, skipped = select_add_candidate([tracked], [], POLICY)
        self.assertIsNone(best)
        self.assertIn("bounds unknown", skipped[0]["reason"])

    def test_bin_step_rail_enforced_when_pool_present(self):
        tracked = self._tracked()
        tracked["_pool"]["bin_step"] = 1  # disallowed
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            best, skipped = select_add_candidate([tracked], [], POLICY)
        self.assertIsNone(best)
        self.assertIn("bin_step", skipped[0]["reason"])

    def test_highest_score_wins_floor_below_open(self):
        a = self._tracked(POOL_A, POS_A, value=40.0)   # floor score
        b = self._tracked(POOL_B, POS_B, value=40.0)   # floor score
        scores = [{"pool_address": POOL_B, "score": 88.0}]
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            best, _ = select_add_candidate([a, b], scores, POLICY)
        self.assertEqual(best["pool_address"], POOL_B)  # live score wins

    def test_floor_score_loses_to_open_candidates(self):
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            best, _ = select_add_candidate([self._tracked()], [], POLICY)
        self.assertLess(best["score"], 70.0)
        self.assertAlmostEqual(best["score"], 69.99)


class TestGuards(SweepTestBase):
    def test_stale_wallet_scan_skips(self):
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            signal, info = self._plan(self._report(idle=8.0), mtime=NOW - 3600)
        self.assertIsNone(signal)
        self.assertIn("stale", info["reason"])

    def test_cooldown_blocks(self):
        state = {"last_add": {"signal_id": "sheldon-sweep-1",
                              "reserved_at": NOW - 2 * 3600}}
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            signal, info = self._plan(self._report(idle=8.0), state=state)
        self.assertIsNone(signal)
        self.assertIn("cooldown", info["reason"])

    def test_day_cap_blocks(self):
        state = {"day_counts": {time.strftime("%Y-%m-%d", time.gmtime(NOW)): 6}}
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            signal, info = self._plan(self._report(idle=8.0), state=state)
        self.assertIsNone(signal)
        self.assertIn("day cap", info["reason"])

    def test_disabled_policy_skips(self):
        policy = dict(POLICY, add_enabled=False)
        with patch.object(idle_sweep, "get_policy", return_value=policy):
            signal, info = self._plan(self._report(idle=8.0))
        self.assertIsNone(signal)
        self.assertIn("enabled", info["reason"])

    def test_no_candidates_skips(self):
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            signal, info = self._plan(self._report(idle=8.0, tracked=[]))
        self.assertIsNone(signal)
        self.assertIn("no qualifying", info["reason"])


class TestReserveConfirm(SweepTestBase):
    def _signal(self):
        return {
            "signal_id": "sheldon-sweep-123",
            "action": "add_liquidity",
            "dex": "meteora",
            "pool_address": POOL_A,
            "position_id": POS_A,
            "side": "quote_only",
            "bin_range": {"lower": -2156, "upper": -2108},
            "liquidity": {"amount_x": "0", "amount_y": "8000000"},
            "position_usd": 8.0,
            "score": 69.99,
            "max_slippage_bps": 100,
            "reason": "IDLE SWEEP test",
            "created_at": "2026-10-01T00:00:00+08:00",
        }

    def test_execute_reserves_then_writes(self):
        ok, err = execute_sweep(self._signal(), self.signals_dir, {},
                                state_dir=self.state_dir, now=NOW)
        self.assertTrue(ok, err)
        state = _load(os.path.join(self.state_dir, "add_state.json"))
        self.assertEqual(state["pending_reservation"]["signal_id"],
                         "sheldon-sweep-123")
        self.assertAlmostEqual(state["pending_reservation"]["amount_usd"], 8.0)
        self.assertTrue(os.path.exists(os.path.join(
            self.signals_dir, "sheldon-sweep-123.json")))

    def test_reservation_counts_against_next_cycle(self):
        execute_sweep(self._signal(), self.signals_dir, {},
                      state_dir=self.state_dir, now=NOW)
        state = json.load(open(
            os.path.join(self.state_dir, "add_state.json")))
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            signal, info = self._plan(self._report(idle=8.0), state=state)
        # 8 reserved, wallet still shows 8 -> net 0 -> below min_add.
        self.assertIsNone(signal)
        self.assertIn("min_add_usd", info["reason"])

    def test_release_when_processed(self):
        execute_sweep(self._signal(), self.signals_dir, {},
                      state_dir=self.state_dir, now=NOW)
        # George processed it; wallet rescan now shows the drop.
        processed = os.path.join(os.path.dirname(self.signals_dir), "processed")
        os.makedirs(processed, exist_ok=True)
        os.rename(os.path.join(self.signals_dir, "sheldon-sweep-123.json"),
                  os.path.join(processed, "sheldon-sweep-123.json"))
        state = _load(os.path.join(self.state_dir, "add_state.json"))
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            signal, info = self._plan(self._report(idle=0.5), state=state)
        # Released reservation becomes last_add; cooldown counts from the
        # emit time, so an immediate retry is blocked (anti-loop rail).
        self.assertIsNone(signal)
        self.assertIn("cooldown", info["reason"])
        saved = _load(os.path.join(self.state_dir, "add_state.json"))
        self.assertIsNone(saved["pending_reservation"])
        self.assertEqual(saved["last_add"]["final_state"], "processed")

    def test_release_when_failed_verify(self):
        execute_sweep(self._signal(), self.signals_dir, {},
                      state_dir=self.state_dir, now=NOW)
        failed = os.path.join(os.path.dirname(self.signals_dir), "failed_verify")
        os.makedirs(failed, exist_ok=True)
        os.rename(os.path.join(self.signals_dir, "sheldon-sweep-123.json"),
                  os.path.join(failed, "sheldon-sweep-123.json"))
        state = _load(os.path.join(self.state_dir, "add_state.json"))
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            # Capital returned to the wallet, but the cooldown counts from
            # the failed emit: no immediate retry loop on a failing add.
            signal, info = self._plan(self._report(idle=8.0), state=state)
        self.assertIsNone(signal)
        self.assertIn("cooldown", info["reason"])
        saved = _load(os.path.join(self.state_dir, "add_state.json"))
        self.assertIsNone(saved["pending_reservation"])
        self.assertEqual(saved["last_add"]["final_state"], "failed_verify")

    def test_retry_allowed_after_cooldown_despite_failure(self):
        execute_sweep(self._signal(), self.signals_dir, {},
                      state_dir=self.state_dir, now=NOW)
        failed = os.path.join(os.path.dirname(self.signals_dir), "failed_verify")
        os.makedirs(failed, exist_ok=True)
        os.rename(os.path.join(self.signals_dir, "sheldon-sweep-123.json"),
                  os.path.join(failed, "sheldon-sweep-123.json"))
        # First plan persists the release; reload and age past cooldown.
        state = _load(os.path.join(self.state_dir, "add_state.json"))
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            self._plan(self._report(idle=8.0), state=state)
        state = _load(os.path.join(self.state_dir, "add_state.json"))
        self.assertIsNone(state["pending_reservation"])
        state["last_add"]["reserved_at"] = NOW - 7 * 3600  # cooldown elapsed
        # Age the failed_verify file past the 24h commit window AND prove
        # the wallet kept the capital (rescan shows 8): the failed tx never
        # landed. Only now is a retry both allowed and safe.
        failed_file = os.path.join(failed, "sheldon-sweep-123.json")
        sig = _load(failed_file)
        aged = NOW - 172800
        sig["created_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S+08:00", time.gmtime(aged + 8 * 3600))
        with open(failed_file, "w") as fh:
            json.dump(sig, fh)
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            signal, info = self._plan(self._report(idle=8.0), state=state)
        self.assertIsNotNone(signal, info)
        self.assertAlmostEqual(signal["position_usd"], 8.0)

    def test_pending_reservation_blocks_then_ages_out(self):
        execute_sweep(self._signal(), self.signals_dir, {},
                      state_dir=self.state_dir, now=NOW)
        state = _load(os.path.join(self.state_dir, "add_state.json"))
        # Still queued, but older than the max age -> released for safety.
        # Cooldown (6h) also elapsed: net idle 0 -> min_add skip proves the
        # hold itself is gone.
        old_state = dict(state)
        old_state["pending_reservation"] = dict(
            state["pending_reservation"], reserved_at=NOW - 7 * 3600)
        old_state["last_add"] = dict(old_state["pending_reservation"])
        with patch.object(idle_sweep, "get_policy", return_value=POLICY):
            signal, info = self._plan(self._report(idle=8.0), state=old_state)
        self.assertIsNone(signal)
        self.assertIn("min_add_usd", info["reason"])
        saved = _load(os.path.join(self.state_dir, "add_state.json"))
        self.assertIsNone(saved["pending_reservation"])

    def test_write_failure_after_reservation_is_surfaced(self):
        # Make the SIGNAL write fail (parent is a file, not a dir) while
        # state persistence still works: reservation must survive.
        blocker = os.path.join(self._tmp.name, "not_a_dir")
        with open(blocker, "w") as fh:
            fh.write("x")
        bad_signals = os.path.join(blocker, "pending")
        ok, err = execute_sweep(self._signal(), bad_signals, {},
                                state_dir=self.state_dir, now=NOW)
        self.assertFalse(ok)
        self.assertIn("signal write failed", err)
        saved = _load(os.path.join(self.state_dir, "add_state.json"))
        self.assertEqual(saved["pending_reservation"]["signal_id"],
                         "sheldon-sweep-123")

    def test_state_save_failure_aborts_before_signal(self):
        # Unwritable state dir -> tmp write fails -> no signal either.
        with open(os.path.join(self.state_dir, "keep.txt"), "w") as fh:
            fh.write("x")
        os.chmod(self.state_dir, 0o555)
        try:
            ok, err = execute_sweep(self._signal(), self.signals_dir, {},
                                    state_dir=self.state_dir, now=NOW)
            self.assertFalse(ok)
            self.assertIn("could not persist", err)
            self.assertFalse(os.path.exists(os.path.join(
                self.signals_dir, "sheldon-sweep-123.json")))
        finally:
            os.chmod(self.state_dir, 0o755)


if __name__ == "__main__":
    unittest.main()
