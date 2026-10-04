#!/usr/bin/env python3
"""Tests for Sheldon run_cycle, capital, and strategy modules."""

import json
import math
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lp_scoring
from capital import RESERVED_MINTS, SOL_MINT, USDC_MINT, summarize_wallet
from readiness import (
    MAX_PREP_SWAPS_PER_MINT_PER_HOUR,
    PREP_CONFIRM_GRACE_SECONDS,
    gate_prep_swaps,
    load_raw_wallet,
    plan_funding,
)
from run_cycle import write_signals
from strategy import (
    DEFAULT_MAX_POSITION_USD,
    MAX_POSITION_OPEN_PER_CYCLE,
    MIN_POSITION_USD,
    adaptive_half_width,
    build_strategies,
    capital_plan,
)
from run_cycle import _build_dust_swap_signal, _build_open_signal
from run_cycle import _filter_open_candidates, _load_active_positions


class OpenCandidateFilterTests(unittest.TestCase):
    """Dedup and center-guard: the ZEC/USDC re-open bug."""

    @staticmethod
    def _candidate(addr, score=90.0, dex="orca", pool=None):
        base_pool = {"current_tick_index": 26191}
        if pool is not None:
            base_pool.update(pool)
        # Policy-eligible defaults so tests exercise the rail under test.
        base_pool.setdefault("tvl", 500000.0)
        base_pool.setdefault("volume_window", 2000000.0)
        # Normalize Meteora pools: active_bin_id drives the center.
        if dex == "meteora" and "active_bin_id" not in base_pool:
            base_pool["active_bin_id"] = base_pool.get("current_tick_index", 0)
        return {
            "action": "OPEN_CANDIDATE",
            "pool_address": addr,
            "dex": dex,
            "score": score,
            "_pool": base_pool,
        }

    def test_held_pool_is_skipped(self):
        kept, skipped = _filter_open_candidates(
            [self._candidate("P1")], {"P1": {"status": "active"}}
        )
        self.assertEqual(kept, [])
        self.assertIn("dedup", skipped[0]["reason"])

    def test_inactive_holding_still_blocks(self):
        kept, skipped = _filter_open_candidates(
            [self._candidate("P1")], {"P1": {"status": "inactive"}}
        )
        self.assertEqual(kept, [])

    def test_any_entry_in_active_map_blocks(self):
        # The active map is authoritative: _load_active_positions already
        # excluded closed entries, so anything present here blocks the open.
        kept, skipped = _filter_open_candidates(
            [self._candidate("P1")], {"P1": {"status": "whatever"}}
        )
        self.assertEqual(kept, [])
        self.assertIn("dedup", skipped[0]["reason"])

    def test_loader_excludes_closed_positions(self):
        root = tempfile.mkdtemp()
        try:
            scan = os.path.join(root, "position_scan-x.json")
            with open(scan, "w") as fh:
                json.dump({"positions": [
                    {"pool_address": "P1", "status": "closed"},
                    {"pool_address": "P2", "status": "active"},
                ]}, fh)
            now = time.time()
            os.utime(scan, (now, now))
            active = _load_active_positions(root)
            self.assertNotIn("P1", active)
            self.assertIn("P2", active)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_duplicate_pool_collapses(self):
        kept, skipped = _filter_open_candidates(
            [self._candidate("P1", score=95.0), self._candidate("P1", score=80.0)],
            {},
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["score"], 95.0)
        self.assertIn("duplicate", skipped[0]["reason"])

    def test_unknown_center_is_dropped(self):
        kept, skipped = _filter_open_candidates(
            [self._candidate("P1", pool={"active_bin_id": None, "current_tick_index": None})],
            {},
        )
        self.assertEqual(kept, [])
        self.assertIn("unknown", skipped[0]["reason"])

    def test_meteora_center_from_active_bin(self):
        kept, _ = _filter_open_candidates(
            [self._candidate("P1", dex="meteora",
                             pool={"active_bin_id": -5333, "bin_step": 10})],
            {},
        )
        self.assertEqual(len(kept), 1)

    def test_meteora_zero_bin_is_dropped(self):
        kept, skipped = _filter_open_candidates(
            [self._candidate("P1", dex="meteora", pool={"active_bin_id": 0})],
            {},
        )
        self.assertEqual(kept, [])

    def test_missy_eligible_false_is_skipped(self):
        candidate = self._candidate(
            "P1", score=90.0,
            pool={"eligible": False, "rejected_reason": "tvl below policy"},
        )
        kept, skipped = _filter_open_candidates([candidate], {})
        self.assertEqual(kept, [])
        self.assertIn("Missy: tvl below policy", skipped[0]["reason"])

    def test_missy_eligible_true_skips_legacy_tvl_volume(self):
        candidate = self._candidate(
            "P1", score=90.0,
            pool={"eligible": True, "tvl": 10.0, "volume_window": 1.0},
        )
        kept, skipped = _filter_open_candidates([candidate], {})
        self.assertEqual(len(kept), 1)
        self.assertEqual(skipped, [])

    def test_legacy_low_tvl_gate_when_eligible_missing(self):
        kept, skipped = _filter_open_candidates(
            [self._candidate("P1", score=90.0,
                             pool={"tvl": 10.0, "volume_window": 100_000.0})],
            {},
        )
        self.assertEqual(kept, [])
        self.assertIn("legacy policy", skipped[0]["reason"])

    def test_meteora_bin_step_4_is_dropped(self):
        # Incident pool 5rCf1DM8...: bin_step 4 violates George's
        # allowed_bin_steps rail (minimum 10).
        kept, skipped = _filter_open_candidates(
            [self._candidate("P1", dex="meteora",
                             pool={"active_bin_id": -5333, "bin_step": 4})],
            {},
        )
        self.assertEqual(kept, [])
        self.assertIn("bin_step 4", skipped[0]["reason"])

    def test_meteora_bin_step_10_is_kept(self):
        kept, _ = _filter_open_candidates(
            [self._candidate("P1", dex="meteora",
                             pool={"active_bin_id": -5333, "bin_step": 10})],
            {},
        )
        self.assertEqual(len(kept), 1)

    def test_meteora_bin_step_100_is_kept(self):
        kept, _ = _filter_open_candidates(
            [self._candidate("P1", dex="meteora",
                             pool={"active_bin_id": -5333, "bin_step": 100})],
            {},
        )
        self.assertEqual(len(kept), 1)

    def test_meteora_missing_bin_step_fails_closed(self):
        kept, skipped = _filter_open_candidates(
            [self._candidate("P1", dex="meteora",
                             pool={"active_bin_id": -5333})],
            {},
        )
        self.assertEqual(kept, [])
        self.assertIn("bin_step", skipped[0]["reason"])

    def test_meteora_malformed_bin_step_fails_closed(self):
        kept, _ = _filter_open_candidates(
            [self._candidate("P1", dex="meteora",
                             pool={"active_bin_id": -5333, "bin_step": "junk"})],
            {},
        )
        self.assertEqual(kept, [])

    def test_non_meteora_pool_ignores_bin_step_rail(self):
        # Orca/Raydium have tick_spacing, not bin steps: rail must not fire.
        kept, _ = _filter_open_candidates(
            [self._candidate("P1", dex="orca",
                             pool={"current_tick_index": 26191, "tick_spacing": 64})],
            {},
        )
        self.assertEqual(len(kept), 1)

    def test_build_open_signal_refuses_disallowed_bin_step(self):
        # Last-resort guard: even if a filtered candidate slipped through,
        # the builder must never emit an open for a rail-violating pool.
        strategy = {"suggested_usdc": 50.0, "center": -100, "half_width": 10,
                    "bin_range": {"lower": -110, "upper": -90}}
        verdict = self._candidate("P1", dex="meteora",
                                  pool={"pool_address": "P1",
                                        "active_bin_id": -100,
                                        "bin_step": 4,
                                        "token_x_decimals": 9,
                                        "token_y_decimals": 6,
                                        "token_x_price_usd": 120.0,
                                        "token_y_price_usd": 1.0})
        # Candidate must pass the filter first; bin_step 4 is dropped.
        kept, skipped = _filter_open_candidates([verdict], {})
        self.assertEqual(kept, [])
        self.assertIn("bin_step 4", skipped[0]["reason"])


class WalletCapitalTests(unittest.TestCase):
    def test_summarize_wallet_idle_usdc_and_dust(self):
        data = {
            "wallet": "W1",
            "total_usd": 200.0,
            "assets": [
                {"mint": USDC_MINT, "amount_raw": "15000000", "amount_ui": 15.0,
                 "decimals": 6, "price_usd": 1.0, "total_value_usd": 15.0,
                 "is_native_sol": False},
                {"mint": "DUSTMINT", "symbol": "DUST", "amount_raw": "2000000000",
                 "amount_ui": 2.0, "decimals": 9, "price_usd": 2.0,
                 "total_value_usd": 4.0, "is_native_sol": False},
                {"mint": SOL_MINT, "symbol": "SOL", "amount_raw": "10000000",
                 "amount_ui": 0.01, "decimals": 9, "price_usd": 120.0,
                 "total_value_usd": 1.2, "is_native_sol": True},
                {"mint": "SMALL", "symbol": "SML", "amount_raw": "1",
                 "amount_ui": 1e-9, "decimals": 9, "price_usd": 1.0,
                 "total_value_usd": 0.5, "is_native_sol": False},
            ],
        }
        summary = summarize_wallet(data)
        self.assertEqual(summary["idle_usdc"], 15.0)
        self.assertEqual(summary["dust_total_usdc"], 4.0)
        self.assertEqual(summary["reserved_total_usdc"], 1.2)
        self.assertEqual(summary["deployable_usdc"], 19.0)
        self.assertEqual(len(summary["dust_assets"]), 1)
        self.assertEqual(summary["dust_assets"][0]["mint"], "DUSTMINT")

    def test_custom_reserved_mint_never_dust(self):
        reserved = "RESVMINT"
        data = {
            "wallet": "W1",
            "total_usd": 100.0,
            "assets": [
                {"mint": reserved, "symbol": "RSV", "amount_raw": "1000000000",
                 "amount_ui": 1.0, "decimals": 9, "price_usd": 10.0,
                 "total_value_usd": 10.0, "is_native_sol": False},
            ],
        }
        summary = summarize_wallet(data)
        self.assertEqual(summary["dust_total_usdc"], 10.0)
        self.assertEqual(summary["reserved_total_usdc"], 0.0)

    def test_usdc_not_dust(self):
        data = {
            "wallet": "W1",
            "total_usd": 20.0,
            "assets": [
                {"mint": USDC_MINT, "amount_raw": "20000000", "amount_ui": 20.0,
                 "decimals": 6, "price_usd": 1.0, "total_value_usd": 20.0,
                 "is_native_sol": False},
            ],
        }
        summary = summarize_wallet(data)
        self.assertEqual(summary["dust_assets"], [])
        self.assertEqual(summary["idle_usdc"], 20.0)


class CapitalPlanTests(unittest.TestCase):
    def test_open_eligible_idle_and_suggested_both_clear(self):
        wallet = {"idle_usdc": 100.0, "deployable_usdc": 200.0}
        plan = capital_plan(wallet)
        self.assertTrue(plan["open_eligible"])
        self.assertEqual(plan["suggested_position_usdc"], 100.0)

    def test_open_not_eligible_low_idle(self):
        wallet = {"idle_usdc": 10.0, "deployable_usdc": 200.0}
        self.assertFalse(capital_plan(wallet)["open_eligible"])

    def test_open_not_eligible_low_deployable(self):
        wallet = {"idle_usdc": 100.0, "deployable_usdc": 10.0}
        plan = capital_plan(wallet)
        self.assertFalse(plan["open_eligible"])
        # suggested_position_usd should be min(deployable*0.75, 100)
        self.assertEqual(plan["suggested_position_usdc"], 7.5)

    def test_suggested_capped_at_max(self):
        wallet = {"idle_usdc": 1000.0, "deployable_usdc": 1000.0}
        plan = capital_plan(wallet)
        self.assertEqual(plan["suggested_position_usdc"], DEFAULT_MAX_POSITION_USD)


class AdaptiveRangeTests(unittest.TestCase):
    def test_meteora_low_volatility(self):
        pool = {"dex": "meteora", "volatility": 1.77, "bin_step": 4, "tick_spacing": 4}
        half = adaptive_half_width(pool)
        self.assertGreaterEqual(half, 10)
        # With small vol and bin_step 4, half-width should be modest.
        self.assertLessEqual(half, 1000)

    def test_meteora_one_bps(self):
        pool = {"dex": "meteora", "volatility": 1.87, "bin_step": 1, "tick_spacing": 1}
        half = adaptive_half_width(pool)
        self.assertGreaterEqual(half, 10)

    def test_raydium_tick_spacing(self):
        pool = {"dex": "raydium", "volatility": 2.0, "bin_step": 0, "tick_spacing": 1}
        half = adaptive_half_width(pool)
        self.assertGreaterEqual(half, 10)

    def test_zero_volatility_defaults_to_min(self):
        pool = {"dex": "meteora", "volatility": 0.0, "bin_step": 4, "tick_spacing": 4}
        self.assertEqual(adaptive_half_width(pool), 10)

    def test_extreme_volatility_clamped(self):
        # Non-Meteora DEXes keep the generic 1000 cap.
        pool = {"dex": "raydium", "volatility": 100.0, "bin_step": 0, "tick_spacing": 1}
        self.assertEqual(adaptive_half_width(pool), 1000)

    def test_meteora_extreme_volatility_clamped_to_rail(self):
        # Meteora half-width must fit inside George's 70-bin inclusive rail.
        pool = {"dex": "meteora", "volatility": 100.0, "bin_step": 1, "tick_spacing": 1}
        self.assertEqual(adaptive_half_width(pool), 34)


class StrategyTests(unittest.TestCase):
    def _pool(self, score: float, volatility: float, bin_step: int,
              active_bin_id: int = 0, dex: str = "meteora"):
        return {
            "pool_address": f"p{score}",
            "name": "SOL-USDC",
            "dex": dex,
            "volatility": volatility,
            "bin_step": bin_step,
            "tick_spacing": bin_step,
            "active_bin_id": active_bin_id,
            "current_tick_index": active_bin_id,
            "token_x_decimals": 9,
            "token_y_decimals": 6,
            "token_x_price_usd": 120.0,
            "token_y_price_usd": 1.0,
        }

    def _candidate(self, score: float, pool: dict) -> dict:
        return {
            "action": "OPEN_CANDIDATE",
            "pool": pool["name"],
            "pool_address": pool["pool_address"],
            "dex": pool["dex"],
            "pair_class": "stable_bluechip",
            "score": score,
            "evidence": {},
            "reason": "test",
            "_pool": pool,
        }

    def test_build_strategies_caps_at_three(self):
        wallet = {"idle_usdc": 100.0, "deployable_usdc": 100.0}
        candidates = [self._candidate(float(i), self._pool(float(i), 1.0, 4, active_bin_id=-100 - i)) for i in range(5)]
        strategies = build_strategies(candidates, wallet)
        self.assertLessEqual(len(strategies), MAX_POSITION_OPEN_PER_CYCLE)

    def test_build_strategies_returns_empty_when_not_eligible(self):
        wallet = {"idle_usdc": 5.0, "deployable_usdc": 5.0}
        candidates = [self._candidate(90.0, self._pool(90.0, 1.0, 4))]
        self.assertEqual(build_strategies(candidates, wallet), [])

    def test_strategy_sizing_is_75_percent(self):
        wallet = {"idle_usdc": 100.0, "deployable_usdc": 200.0}
        candidate = self._candidate(90.0, self._pool(90.0, 1.0, 4))
        strategies = build_strategies([candidate], wallet)
        # 75% of 200 = 150, capped at DEFAULT_MAX_POSITION_USD (100).
        self.assertEqual(strategies[0]["suggested_usdc"], 100.0)

    def test_strategy_range_centered(self):
        wallet = {"idle_usdc": 100.0, "deployable_usdc": 100.0}
        candidate = self._candidate(90.0, self._pool(90.0, 1.0, 4, active_bin_id=-100))
        strategies = build_strategies([candidate], wallet)
        s = strategies[0]
        half = s["half_width"]
        self.assertEqual(s["center"], -100)
        self.assertEqual(s["bin_range"]["lower"], -100 - half)
        self.assertEqual(s["bin_range"]["upper"], -100 + half)


class SignalTests(unittest.TestCase):
    def _strategy(self):
        return {
            "suggested_usdc": 50.0,
            "center": -5299,
            "half_width": 23,
            "bin_range": {"lower": -5322, "upper": -5276},
        }

    def _verdict(self):
        return {
            "action": "OPEN_CANDIDATE",
            "pool": "SOL-USDC",
            "pool_address": "p1",
            "dex": "meteora",
            "score": 90.0,
            "evidence": {},
            "_pool": {
                "pool_address": "p1",
                "bin_step": 10,
                "token_x_decimals": 9,
                "token_y_decimals": 6,
                "token_x_price_usd": 120.0,
                "token_y_price_usd": 1.0,
            },
        }

    def test_build_open_signal(self):
        signal = _build_open_signal(self._strategy(), self._verdict(), 1, 12345)
        self.assertEqual(signal["action"], "open")
        self.assertEqual(signal["dex"], "meteora")
        self.assertEqual(signal["bin_range"]["lower"], -5322)
        self.assertEqual(signal["bin_range"]["upper"], -5276)
        self.assertIn("amount_x", signal["liquidity"])
        self.assertIn("amount_y", signal["liquidity"])
        # 50/50 split of $50 => $25 each side.
        self.assertEqual(signal["liquidity"]["amount_x"], str(int(25 / 120 * 1e9)))
        self.assertEqual(signal["liquidity"]["amount_y"], str(int(25 / 1 * 1e6)))

    def test_open_signal_carries_score_policy(self):
        # Audit trail: every open signal records which scoring policy made it.
        signal = _build_open_signal(self._strategy(), self._verdict(), 1, 12345)
        sp = signal["score_policy"]
        self.assertIn(sp["source"], ("missy", "local"))
        self.assertIsInstance(sp["version"], int)
        self.assertEqual(float(sp["min_open_score"]), 70.0)

    def test_scoring_policy_identity_loads(self):
        from strategy import scoring_policy
        sp = scoring_policy()
        self.assertEqual(set(sp), {"source", "version", "min_open_score"})
        self.assertIn(sp["source"], ("missy", "local"))
        self.assertGreaterEqual(sp["version"], 1)

    def test_build_dust_swap_signal(self):
        asset = {
            "mint": "DUST",
            "symbol": "DUST",
            "decimals": 9,
            "amount_raw": "1000000000",
            "amount_ui": 1.0,
            "value_usd": 5.0,
        }
        signal = _build_dust_swap_signal(asset, 1, 12345)
        self.assertEqual(signal["action"], "swap_to_usdc")
        self.assertEqual(signal["mint"], "DUST")
        self.assertEqual(signal["value_usd"], 5.0)


class ReadinessGateTests(unittest.TestCase):
    """Prep swap gating: stale scans, confirmation grace, hourly cap."""

    def setUp(self):
        self.root = tempfile.mkdtemp()  # George-style signals root
        self.pending = os.path.join(self.root, "pending")
        self.processed = os.path.join(self.root, "processed")
        os.makedirs(self.pending)
        os.makedirs(self.processed)
        self.tz = timezone(timedelta(hours=8))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_prep(self, subdir, signal_id, created_epoch,
                    input_mint=USDC_MINT, output_mint=SOL_MINT):
        sig = {
            "signal_id": signal_id, "action": "swap",
            "input_mint": input_mint, "output_mint": output_mint,
            "amount": "1000000",
            "created_at": datetime.fromtimestamp(created_epoch, self.tz).strftime(
                "%Y-%m-%dT%H:%M:%S+08:00"),
        }
        with open(os.path.join(subdir, f"{signal_id}.json"), "w") as fh:
            json.dump(sig, fh)

    def _spec(self, direction="buy"):
        if direction == "buy":
            return {"direction": "buy", "for_pool": "p1", "input_mint": USDC_MINT,
                    "output_mint": SOL_MINT, "amount_raw": "1000000",
                    "usd": 1.0, "need_ui": 0.1, "have_ui": 0.0, "symbol": "SOL"}
        return {"direction": "sell", "for_pool": None, "input_mint": SOL_MINT,
                "output_mint": USDC_MINT, "amount_raw": "1000000",
                "usd": 1.0, "need_ui": 0.0, "have_ui": 0.1, "symbol": "SOL"}

    def test_history_found_in_pending_and_processed_subdirs(self):
        self._write_prep(self.pending, "sheldon-prep-1-1", 1000)
        self._write_prep(self.processed, "sheldon-prep-2-1", 2000)
        history = __import__("readiness")._prep_swap_history(self.pending)
        # signals_dir given as the pending dir must still find processed/.
        self.assertEqual(len(history), 2)

    def test_stale_scan_blocks_all(self):
        now = time.time()
        allowed, blocked = gate_prep_swaps([self._spec()], self.pending,
                                           wallet_mtime=now - 500, now=now)
        self.assertEqual(allowed, [])
        self.assertIn("stale", blocked[0]["reason"])

    def test_missing_mtime_blocks_all(self):
        allowed, blocked = gate_prep_swaps([self._spec()], self.pending,
                                           wallet_mtime=0.0, now=time.time())
        self.assertEqual(allowed, [])

    def test_fresh_scan_no_history_allows(self):
        now = time.time()
        allowed, blocked = gate_prep_swaps([self._spec()], self.pending,
                                           wallet_mtime=now - 30, now=now)
        self.assertEqual(len(allowed), 1)
        self.assertEqual(blocked, [])

    def test_rescan_too_soon_after_swap_blocks(self):
        now = time.time()
        last = now - 30  # swap 30s ago; grace is 60s
        self._write_prep(self.processed, "sheldon-prep-10-1", last)
        allowed, blocked = gate_prep_swaps(
            [self._spec()], self.pending,
            wallet_mtime=last + 10, now=now)  # rescan only 10s after swap
        self.assertEqual(allowed, [])
        self.assertIn("grace", blocked[0]["reason"])

    def test_rescan_after_grace_allows(self):
        now = time.time()
        last = now - PREP_CONFIRM_GRACE_SECONDS - 30
        self._write_prep(self.processed, "sheldon-prep-11-1", last)
        allowed, blocked = gate_prep_swaps(
            [self._spec()], self.pending,
            wallet_mtime=now - 5, now=now)
        self.assertEqual(len(allowed), 1)

    def test_hourly_cap_blocks(self):
        now = time.time()
        for i in range(MAX_PREP_SWAPS_PER_MINT_PER_HOUR):
            self._write_prep(self.processed, f"sheldon-prep-20-{i}",
                             now - 1800 - i)
        allowed, blocked = gate_prep_swaps(
            [self._spec()], self.pending,
            wallet_mtime=now - 5, now=now)
        self.assertEqual(allowed, [])
        self.assertIn("loop guard", blocked[0]["reason"])


class RunCycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.pools_dir = os.path.join(self.root, "pools")
        self.pos_dir = os.path.join(self.root, "positions")
        self.wallet_dir = os.path.join(self.root, "wallet_screens")
        for d in (self.pools_dir, self.pos_dir, self.wallet_dir):
            os.makedirs(d)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _touch(self, path):
        now = time.time()
        os.utime(path, (now, now))

    def _make_pool(self):
        return {
            "name": "SOL-USDC", "pool_address": "p2", "dex": "meteora",
            "token_x_symbol": "SOL", "token_y_symbol": "USDC",
            "token_x_address": SOL_MINT, "token_y_address": USDC_MINT,
            "tvl": 2_000_000.0, "volume_window": 20_000_000.0,
            "realized_fee_apr": 150.0, "volatility": 8.0,
            "token_x_price_usd": 117.0, "token_y_price_usd": 1.0,
            "token_x_decimals": 9, "token_y_decimals": 6,
            # bin_step 10: an allowed step. The 4-step incident pool shape
            # is covered by test_meteora_bin_step_4_is_dropped above.
            "bin_step": 10, "tick_spacing": 4,
            "active_bin_id": -5299,
        }

    def test_wallet_and_strategies_in_report(self):
        pool = self._make_pool()
        with open(os.path.join(self.pools_dir, "pool_scan-x.json"), "w") as fh:
            json.dump([pool], fh)
        self._touch(os.path.join(self.pools_dir, "pool_scan-x.json"))

        with open(os.path.join(self.pos_dir, "position_scan-x.json"), "w") as fh:
            json.dump({"positions": []}, fh)
        self._touch(os.path.join(self.pos_dir, "position_scan-x.json"))

        wallet_scan = {
            "wallet": "W1",
            "total_usd": 200.0,
            "assets": [
                {"mint": USDC_MINT, "amount_raw": "100000000", "amount_ui": 100.0,
                 "decimals": 6, "price_usd": 1.0, "total_value_usd": 100.0,
                 "is_native_sol": False},
                {"mint": "DUST", "symbol": "DUST", "amount_raw": "1000000000",
                 "amount_ui": 1.0, "decimals": 9, "price_usd": 20.0,
                 "total_value_usd": 20.0, "is_native_sol": False},
            ],
        }
        with open(os.path.join(self.wallet_dir, "wallet_screen-x.json"), "w") as fh:
            json.dump(wallet_scan, fh)
        self._touch(os.path.join(self.wallet_dir, "wallet_screen-x.json"))

        report = lp_scoring.run_cycle(
            self.pools_dir, self.pos_dir, self.wallet_dir, max_age_seconds=3900.0
        )

        self.assertEqual(report["failures"], [])
        self.assertIn("wallet", report)
        self.assertEqual(report["wallet"]["idle_usdc"], 100.0)
        self.assertEqual(report["wallet"]["dust_total_usdc"], 20.0)
        self.assertEqual(report["wallet"]["deployable_usdc"], 120.0)
        # 75% of deployable = 90, idle > 20 => eligible.

    def _write_signals_with_funding(self, wallet_scan, signals_dir, report=None):
        """Mirror main(): run the funding plan + gates before write_signals."""
        if report is None:
            report = lp_scoring.run_cycle(
                self.pools_dir, self.pos_dir, self.wallet_dir, max_age_seconds=3900.0
            )
        report["capital_plan"] = capital_plan(report["wallet"])
        open_candidates = [v for v in report["verdicts"]
                           if v.get("action") == "OPEN_CANDIDATE"]
        report["strategies"] = build_strategies(open_candidates, report["wallet"])
        pool_meta = {v.get("pool_address"): (v.get("_pool") or {})
                     for v in open_candidates}
        for s in report["strategies"]:
            s.setdefault("_pool", pool_meta.get(s.get("pool_address")) or {})
        raw = load_raw_wallet(self.wallet_dir)
        dust_mints = {a["mint"] for a in (report["wallet"].get("dust_assets") or [])}
        funding = plan_funding(report["strategies"], raw["assets"],
                               wallet_path=raw["path"], wallet_mtime=raw["mtime"],
                               dust_mints=dust_mints)
        allowed, blocked = gate_prep_swaps(
            funding["prep_swaps"], signals_dir, funding["wallet_mtime"]
        )
        funding["prep_swaps_allowed"] = allowed
        funding["prep_swaps_blocked"] = blocked
        report["funding"] = funding
        return write_signals(report, signals_dir, self.pos_dir)

    def test_write_signals_creates_dust_and_open(self):
        pool = self._make_pool()
        with open(os.path.join(self.pools_dir, "pool_scan-x.json"), "w") as fh:
            json.dump([pool], fh)
        self._touch(os.path.join(self.pools_dir, "pool_scan-x.json"))

        with open(os.path.join(self.pos_dir, "position_scan-x.json"), "w") as fh:
            json.dump({"positions": []}, fh)
        self._touch(os.path.join(self.pos_dir, "position_scan-x.json"))

        wallet_scan = {
            "wallet": "W1",
            "total_usd": 200.0,
            "assets": [
                {"mint": USDC_MINT, "amount_raw": "100000000", "amount_ui": 100.0,
                 "decimals": 6, "price_usd": 1.0, "total_value_usd": 100.0,
                 "is_native_sol": False},
                {"mint": SOL_MINT, "symbol": "SOL", "amount_raw": "404615384",
                 "amount_ui": 0.404615384, "decimals": 9, "price_usd": 117.0,
                 "total_value_usd": 47.34, "is_native_sol": True},
                {"mint": "DUST", "symbol": "DUST", "amount_raw": "1000000000",
                 "amount_ui": 1.0, "decimals": 9, "price_usd": 20.0,
                 "total_value_usd": 20.0, "is_native_sol": False},
            ],
        }
        with open(os.path.join(self.wallet_dir, "wallet_screen-x.json"), "w") as fh:
            json.dump(wallet_scan, fh)
        self._touch(os.path.join(self.wallet_dir, "wallet_screen-x.json"))

        signals_dir = os.path.join(self.root, "signals")
        created, review = self._write_signals_with_funding(None, signals_dir)

        actions = []
        for path in created:
            with open(path, "r", encoding="utf-8") as fh:
                signal = json.load(fh)
            actions.append(signal["action"])

        self.assertIn("swap_to_usdc", actions)
        self.assertIn("open", actions)
        self.assertNotIn("swap", actions)  # funded: no prep swap needed
        self.assertEqual(actions.count("swap_to_usdc"), 1)

    def test_write_signals_defers_open_and_emits_prep_swap(self):
        pool = self._make_pool()
        with open(os.path.join(self.pools_dir, "pool_scan-x.json"), "w") as fh:
            json.dump([pool], fh)
        self._touch(os.path.join(self.pools_dir, "pool_scan-x.json"))

        with open(os.path.join(self.pos_dir, "position_scan-x.json"), "w") as fh:
            json.dump({"positions": []}, fh)
        self._touch(os.path.join(self.pos_dir, "position_scan-x.json"))

        # USDC only: no SOL -> strategy unfunded -> prep buy swap, no open.
        wallet_scan = {
            "wallet": "W1",
            "total_usd": 200.0,
            "assets": [
                {"mint": USDC_MINT, "amount_raw": "100000000", "amount_ui": 100.0,
                 "decimals": 6, "price_usd": 1.0, "total_value_usd": 100.0,
                 "is_native_sol": False},
            ],
        }
        with open(os.path.join(self.wallet_dir, "wallet_screen-x.json"), "w") as fh:
            json.dump(wallet_scan, fh)
        self._touch(os.path.join(self.wallet_dir, "wallet_screen-x.json"))

        signals_dir = os.path.join(self.root, "signals")
        created, review = self._write_signals_with_funding(None, signals_dir)

        by_action = {}
        for path in created:
            with open(path, "r", encoding="utf-8") as fh:
                signal = json.load(fh)
            by_action.setdefault(signal["action"], []).append(signal)

        self.assertNotIn("open", by_action)
        preps = by_action.get("swap", [])
        self.assertEqual(len(preps), 1)
        prep = preps[0]
        self.assertEqual(prep["input_mint"], USDC_MINT)
        self.assertEqual(prep["output_mint"], SOL_MINT)
        self.assertEqual(prep["direction"], "buy")
        # Position = 75% of deployable (100 USDC) = $75; half = $37.50.
        # delta = 37.5 / 117 SOL; +2% buffer, in USDC base units.
        delta_raw = int(37.5 / 117 * 1e9)
        expected_cost = math.ceil((delta_raw / 1e9) * 117 * 1.02 * 1e6)
        self.assertEqual(int(prep["amount"]), expected_cost)
        self.assertGreater(int(prep["amount"]), 0)
        # The deferred open is explained in review, not silently dropped.
        self.assertTrue(any("deferred" in r["reason"] for r in review))

    def test_write_signals_without_funding_plan_refuses_open(self):
        pool = self._make_pool()
        with open(os.path.join(self.pools_dir, "pool_scan-x.json"), "w") as fh:
            json.dump([pool], fh)
        self._touch(os.path.join(self.pools_dir, "pool_scan-x.json"))

        with open(os.path.join(self.pos_dir, "position_scan-x.json"), "w") as fh:
            json.dump({"positions": []}, fh)
        self._touch(os.path.join(self.pos_dir, "position_scan-x.json"))

        wallet_scan = {
            "wallet": "W1",
            "total_usd": 200.0,
            "assets": [
                {"mint": USDC_MINT, "amount_raw": "100000000", "amount_ui": 100.0,
                 "decimals": 6, "price_usd": 1.0, "total_value_usd": 100.0,
                 "is_native_sol": False},
                {"mint": SOL_MINT, "symbol": "SOL", "amount_raw": "300000000",
                 "amount_ui": 0.3, "decimals": 9, "price_usd": 117.0,
                 "total_value_usd": 35.1, "is_native_sol": True},
            ],
        }
        with open(os.path.join(self.wallet_dir, "wallet_screen-x.json"), "w") as fh:
            json.dump(wallet_scan, fh)
        self._touch(os.path.join(self.wallet_dir, "wallet_screen-x.json"))

        report = lp_scoring.run_cycle(
            self.pools_dir, self.pos_dir, self.wallet_dir, max_age_seconds=3900.0
        )
        report["capital_plan"] = capital_plan(report["wallet"])
        open_candidates = [v for v in report["verdicts"]
                           if v.get("action") == "OPEN_CANDIDATE"]
        report["strategies"] = build_strategies(open_candidates, report["wallet"])

        signals_dir = os.path.join(self.root, "signals")
        created, review = write_signals(report, signals_dir, self.pos_dir)

        actions = []
        for path in created:
            with open(path, "r", encoding="utf-8") as fh:
                signal = json.load(fh)
            actions.append(signal["action"])
        self.assertNotIn("open", actions)
        self.assertTrue(any("funding plan missing" in r["reason"] for r in review))


if __name__ == "__main__":
    unittest.main(verbosity=2)
