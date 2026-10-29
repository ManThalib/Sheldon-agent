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
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lp_scoring
import pool_features
from capital import RESERVED_MINTS, SOL_MINT, USDC_MINT, summarize_wallet
from readiness import (
    MAX_PREP_SWAPS_PER_MINT_PER_HOUR,
    PREP_CONFIRM_GRACE_SECONDS,
    build_readiness,
    gate_prep_swaps,
    load_raw_wallet,
    plan_funding,
    sol_overhead_lamports,
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
from run_cycle import _release_unemitted_rotations, _settle_rotations


class RotationSettlementTests(unittest.TestCase):
    """Item 3: a rejected/failed rotation close must fail the entry and
    release the candidate reservation instead of wedging it for 7 days."""

    DAY = "2026-10-07"
    NOW = 1791400000.0  # 2026-10-07-ish epoch; only relative math matters

    def _rotations(self, status="awaiting_close",
                   sid="sheldon-rot-1-1"):
        return {"SRC": {"candidate_pool": "CAND", "cycle_day": self.DAY,
                        "status": status, "close_signal_id": sid}}

    def _reserved(self, rotations):
        return {e.get("candidate_pool") for e in rotations.values()
                if isinstance(e, dict)
                and e.get("status") in ("proposed", "awaiting_close")
                and e.get("candidate_pool")}

    def test_rejected_close_fails_entry_and_releases_reservation(self):
        rotations = self._rotations()
        review = _settle_rotations(
            rotations, {"SRC": {"pool_address": "SRC"}},
            {"sheldon-rot-1-1": {"status": "rejected",
                                 "reason": "simulation failed",
                                 "confirmed_at": self.NOW - 10}},
            now_ts=self.NOW)
        entry = rotations["SRC"]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("rejected", entry["reason"])
        self.assertIn("simulation failed", entry["reason"])
        self.assertTrue(entry.get("failed_at"))
        self.assertEqual([r["pool_address"] for r in review], ["SRC"])
        self.assertEqual(review[0]["reason"], entry["reason"])
        self.assertNotIn("CAND", self._reserved(rotations))

    def test_failed_and_failed_verify_also_fail_entry(self):
        for status in ("failed", "failed_verify"):
            rotations = self._rotations(sid="sheldon-rot-2-1")
            review = _settle_rotations(
                rotations, {"SRC": {"pool_address": "SRC"}},
                {"sheldon-rot-2-1": {"status": status, "reason": "rpc down",
                                     "confirmed_at": self.NOW}},
                now_ts=self.NOW)
            self.assertEqual(rotations["SRC"]["status"], "failed", status)
            self.assertEqual(len(review), 1)

    def test_executed_close_keeps_awaiting_close(self):
        rotations = self._rotations(sid="sheldon-rot-3-1")
        review = _settle_rotations(
            rotations, {"SRC": {"pool_address": "SRC"}},
            {"sheldon-rot-3-1": {"status": "executed",
                                 "confirmed_at": self.NOW}},
            now_ts=self.NOW)
        self.assertEqual(rotations["SRC"]["status"], "awaiting_close")
        self.assertEqual(review, [])
        self.assertIn("CAND", self._reserved(rotations))

    def test_settlement_wins_when_source_position_gone(self):
        # George's close landed (source left the scan): settle, even if an
        # older journal line said rejected. Phase 2: the settle must be
        # backed by a current position scan and a wallet scan newer than
        # the close confirmation.
        rotations = self._rotations(sid="sheldon-rot-4-1")
        review = _settle_rotations(
            rotations, {},
            {"sheldon-rot-4-1": {"status": "rejected",
                                 "confirmed_at": self.NOW}},
            now_ts=self.NOW + 60,
            position_mtime=self.NOW + 60,
            wallet_mtime=self.NOW + 60)
        self.assertEqual(rotations["SRC"]["status"], "settled")
        self.assertEqual(review, [])

    def test_stale_pending_entry_expires(self):
        rotations = self._rotations(sid="sheldon-rot-5-1")
        _settle_rotations(rotations, {"SRC": {"pool_address": "SRC"}}, {},
                          now_ts=self.NOW + 8 * 24 * 3600)
        self.assertEqual(rotations["SRC"]["status"], "expired")

    def test_unemitted_close_signal_releases_reservation(self):
        rotations = {"SRC": {"candidate_pool": "CAND", "cycle_day": self.DAY,
                             "status": "awaiting_close"},
                     "OLD": {"candidate_pool": "OLD_CAND",
                             "cycle_day": "2026-10-01",
                             "status": "awaiting_close",
                             "close_signal_id": "sheldon-old-1"}}
        review = _release_unemitted_rotations(rotations, cycle_day=self.DAY)
        self.assertEqual(rotations["SRC"]["status"], "failed")
        self.assertEqual([r["pool_address"] for r in review], ["SRC"])
        # An entry from an earlier cycle with a recorded signal is untouched.
        self.assertEqual(rotations["OLD"]["status"], "awaiting_close")

    def test_unemitted_release_ignores_recorded_signal(self):
        rotations = {"SRC": {"candidate_pool": "CAND", "cycle_day": self.DAY,
                             "status": "awaiting_close",
                             "close_signal_id": "sheldon-rot-9-1"}}
        self.assertEqual(
            _release_unemitted_rotations(rotations, cycle_day=self.DAY), [])
        self.assertEqual(rotations["SRC"]["status"], "awaiting_close")

    def test_unknown_close_signal_id_is_not_failed(self):
        rotations = {"SRC": {"candidate_pool": "CAND", "cycle_day": self.DAY,
                             "status": "awaiting_close"}}
        review = _settle_rotations(
            rotations, {"SRC": {"pool_address": "SRC"}},
            {"other-signal": {"status": "rejected", "confirmed_at": self.NOW}},
            now_ts=self.NOW)
        self.assertEqual(rotations["SRC"]["status"], "awaiting_close")
        self.assertEqual(review, [])


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

    def test_meteora_bin_step_125_is_kept_after_owner_ruling(self):
        # Owner ruling 2026-10-19: any Meteora bin_step >= 4 is allowed.
        # This test previously asserted 125 was dropped by the whitelist.
        kept, _ = _filter_open_candidates(
            [self._candidate("P1", dex="meteora",
                             pool={"active_bin_id": -5333, "bin_step": 125})],
            {},
        )
        self.assertEqual(len(kept), 1)

    def test_meteora_bin_step_below_owner_minimum_is_dropped(self):
        for step in (1, 2, 3):
            kept, skipped = _filter_open_candidates(
                [self._candidate("P1", dex="meteora",
                                 pool={"active_bin_id": -5333,
                                       "bin_step": step})],
                {},
            )
            self.assertEqual(kept, [], f"bin_step {step} must be rejected")
            self.assertIn("below owner minimum", skipped[0]["reason"])

    def test_meteora_bin_step_above_protocol_max_is_dropped(self):
        kept, skipped = _filter_open_candidates(
            [self._candidate("P1", dex="meteora",
                             pool={"active_bin_id": -5333, "bin_step": 401})],
            {},
        )
        self.assertEqual(kept, [])
        self.assertIn("protocol maximum", skipped[0]["reason"])

    def test_meteora_bin_step_400_is_kept(self):
        kept, _ = _filter_open_candidates(
            [self._candidate("P1", dex="meteora",
                             pool={"active_bin_id": -5333, "bin_step": 400})],
            {},
        )
        self.assertEqual(len(kept), 1)

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
                                        "bin_step": 3,
                                        "token_x_decimals": 9,
                                        "token_y_decimals": 6,
                                        "token_x_price_usd": 120.0,
                                        "token_y_price_usd": 1.0})
        # Candidate must pass the filter first; bin_step 3 is below the
        # owner minimum and is dropped.
        kept, skipped = _filter_open_candidates([verdict], {})
        self.assertEqual(kept, [])
        self.assertIn("below owner minimum", skipped[0]["reason"])


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
        # Volatile dust is NOT deployable: it must be swapped first.
        self.assertEqual(summary["deployable_usdc"], 15.0)
        self.assertEqual(summary["liquidation_required_usd"], 4.0)
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
        wallet = {"idle_usdc": 200.0}
        plan = capital_plan(wallet)
        self.assertTrue(plan["open_eligible"])
        self.assertEqual(plan["suggested_position_usdc"], 100.0)

    def test_open_not_eligible_low_idle(self):
        wallet = {"idle_usdc": 10.0, "deployable_usdc": 200.0}
        self.assertFalse(capital_plan(wallet)["open_eligible"])

    def test_open_not_eligible_low_deployable(self):
        wallet = {"idle_usdc": 10.0}
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

    def test_raydium_tick_spacing_raw_ticks(self):
        # Orca/Raydium half-width is raw 1bp ticks: vol 2% * WIDTH_FACTOR 0.5
        # = +/-1% target = 100 ticks. (The old spacing-unit formula returned
        # spacing counts that were then applied as raw ticks — 4x narrow on
        # a ts=4 pool.)
        pool = {"dex": "raydium", "volatility": 2.0, "bin_step": 0, "tick_spacing": 1}
        self.assertEqual(adaptive_half_width(pool), 100)

    def test_orca_incident_replay_floor_widens_narrow_estimate(self):
        # 2026-10-14 Czfq3xZZ replay: Orca's old 2-sample day estimate read
        # 0.137% for SOL-USDC (ts=4); the tick-count floor opened ±10 raw
        # ticks = ±0.1% and price sat 5 ticks from the edge in 45 minutes.
        # The price-fraction floor (±1%) now yields 100 raw ticks.
        pool = {"dex": "orca", "volatility": 0.137, "bin_step": 0, "tick_spacing": 4}
        self.assertEqual(adaptive_half_width(pool), 100)

    def test_orca_healthy_volatility_raw_ticks(self):
        # Corrected day estimate (~6.13% from the 3-sample window) on the
        # same pool: ±3.02% target -> 302 raw ticks.
        pool = {"dex": "orca", "volatility": 6.13, "bin_step": 0, "tick_spacing": 4}
        self.assertEqual(adaptive_half_width(pool), 302)

    def test_fraction_floor_price_parity_across_dex(self):
        # Same ±1% floor expressed in each DEX's own units.
        orca = {"dex": "orca", "volatility": 0.0, "bin_step": 0, "tick_spacing": 1}
        meteora = {"dex": "meteora", "volatility": 0.0, "bin_step": 10, "tick_spacing": 0}
        self.assertEqual(adaptive_half_width(orca), 100)    # 100 x 1bp ticks
        self.assertEqual(adaptive_half_width(meteora), 10)  # 10 x 10bp bins

    def test_zero_volatility_uses_fraction_floor(self):
        # Zero/unknown volatility no longer opens at the 10-bin structural
        # floor: bs=4 -> ±1% = 25 bins.
        pool = {"dex": "meteora", "volatility": 0.0, "bin_step": 4, "tick_spacing": 4}
        self.assertEqual(adaptive_half_width(pool), 25)

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
        wallet = {"idle_usdc": 200.0}
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
        self.assertEqual(set(sp), {"source", "version", "contract_version",
                                   "gate", "min_open_score"})
        self.assertIn(sp["source"], ("missy", "local"))
        self.assertGreaterEqual(sp["version"], 1)
        self.assertEqual(sp["contract_version"], 1)
        self.assertEqual(sp["gate"], "safety")

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
            # bin_step 10: an allowed step. Disallowed bin steps are
            # covered by test_meteora_bin_step_125_is_dropped above.
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
        # 20.0 of volatile dust is not deployable; only the 100 USDC is.
        self.assertEqual(report["wallet"]["deployable_usdc"], 100.0)
        # 75% of 100 = 75, idle > 20 => eligible.
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
                               dust_mints=dust_mints,
                               sol_reserve_lamports=sol_overhead_lamports(
                                   report["strategies"][0].get("dex")
                                   if report["strategies"] else None))
        allowed, blocked = gate_prep_swaps(
            funding["prep_swaps"], signals_dir, funding["wallet_mtime"]
        )
        funding["prep_swaps_allowed"] = allowed
        funding["prep_swaps_blocked"] = blocked
        report["funding"] = funding
        # Phase 1: mirror main() and attach the structured readiness object
        # the open gate requires (no open without a passing readiness object).
        pos_path = lp_scoring.newest_file(self.pos_dir, "position_scan")
        try:
            position_mtime = os.path.getmtime(pos_path) if pos_path else 0.0
        except OSError:
            position_mtime = 0.0
        funding_entries = {
            e.get("pool_address"): e
            for e in (funding.get("funded") or [])
            + (funding.get("unfunded") or [])
            if e.get("pool_address")
        }
        report["readiness"] = {
            s.get("pool_address"): build_readiness(
                s,
                assets=raw["assets"],
                wallet_mtime=raw["mtime"],
                position_mtime=position_mtime,
                now=time.time(),
                funding_entry=funding_entries.get(s.get("pool_address")),
                close_settled=True,
            )
            for s in report["strategies"]
        }
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
                {"mint": SOL_MINT, "symbol": "SOL", "amount_raw": "600000000",
                 "amount_ui": 0.6, "decimals": 9, "price_usd": 117.0,
                 "total_value_usd": 70.2, "is_native_sol": True},
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
        prep_for = []
        for path in created:
            with open(path, "r", encoding="utf-8") as fh:
                signal = json.load(fh)
            actions.append(signal["action"])
            if signal["action"] == "swap":
                prep_for.append(signal.get("prep_for_pool"))

        self.assertIn("swap_to_usdc", actions)
        self.assertIn("open", actions)
        # Funded from existing balances: no prep swap FOR THIS POOL. A
        # pool-agnostic surplus sell (prep_for_pool is None) is the separate
        # "dead capital" rebalance and is allowed — the SOL above the
        # $100-sized open's need still crosses SELL_SURPLUS_MIN_USD ($2).
        self.assertNotIn("p2", prep_for)
        self.assertEqual(actions.count("swap_to_usdc"), 1)

    def test_dust_sweep_skips_recent_prep_buy(self):
        """Anti-oscillation for the dust path: a mint bought as capital prep
        within the oscillation window must not be dust-swept back to USDC
        (2026-10-08: WBTC bought at 19:44:13 was dust-swept at 19:44:31)."""
        from readiness import prep_ledger

        state_dir = os.path.join(self.root, "state")
        ledger = {"entries": [{
            "key": f"{USDC_MINT}|WBTC|buy|p2",
            "input_mint": USDC_MINT,
            "output_mint": "WBTC",
            "direction": "buy",
            "prep_for_pool": "p2",
            "signal_id": "sheldon-prep-test-1",
            "emitted_at": time.time() - 60.0,
            "status": "executed",
            "confirmed_at": time.time() - 30.0,
            "in_amount": "51000000",
            "out_amount": "61796",
        }]}
        prep_ledger.save_ledger(ledger, state_dir=state_dir)

        report = {
            "wallet": {
                "wallet_id": "main",
                "dust_assets": [
                    {"mint": "WBTC", "symbol": "", "decimals": 8,
                     "amount_raw": "61753", "amount_ui": 0.00061753,
                     "price_usd": 82506.98, "value_usd": 50.95},
                    {"mint": "OTHERDUST", "symbol": "", "decimals": 9,
                     "amount_raw": "1000000000", "amount_ui": 1.0,
                     "price_usd": 2.0, "value_usd": 2.0},
                ],
            },
            "verdicts": [],
            "open_skipped": [],
            "strategies": [],
            "funding": {"funded": [], "unfunded": [],
                        "prep_swaps_allowed": [], "prep_swaps_blocked": []},
        }
        signals_dir = os.path.join(self.root, "signals")
        created, _review = write_signals(
            report, signals_dir, self.pos_dir, state_dir=state_dir,
        )

        dust_signals = []
        for path in created:
            with open(path, "r", encoding="utf-8") as fh:
                sig = json.load(fh)
            if sig.get("action") == "swap_to_usdc":
                dust_signals.append(sig["mint"])
        # The recent prep buy is protected; unrelated dust still sweeps.
        self.assertNotIn("WBTC", dust_signals)
        self.assertIn("OTHERDUST", dust_signals)

    def test_dust_sweep_resumes_after_oscillation_window(self):
        """Once the window passes, the same mint is sweepable again."""
        from readiness import prep_ledger

        state_dir = os.path.join(self.root, "state")
        ledger = {"entries": [{
            "key": f"{USDC_MINT}|WBTC|buy|p2",
            "input_mint": USDC_MINT,
            "output_mint": "WBTC",
            "direction": "buy",
            "prep_for_pool": "p2",
            "signal_id": "sheldon-prep-test-2",
            "emitted_at": time.time() - 7200.0,
            "status": "executed",
            "confirmed_at": time.time() - 7100.0,
            "in_amount": "51000000",
            "out_amount": "61796",
        }]}
        prep_ledger.save_ledger(ledger, state_dir=state_dir)

        report = {
            "wallet": {
                "wallet_id": "main",
                "dust_assets": [
                    {"mint": "WBTC", "symbol": "", "decimals": 8,
                     "amount_raw": "61753", "amount_ui": 0.00061753,
                     "price_usd": 82506.98, "value_usd": 50.95},
                ],
            },
            "verdicts": [],
            "open_skipped": [],
            "strategies": [],
            "funding": {"funded": [], "unfunded": [],
                        "prep_swaps_allowed": [], "prep_swaps_blocked": []},
        }
        signals_dir = os.path.join(self.root, "signals")
        created, _review = write_signals(
            report, signals_dir, self.pos_dir, state_dir=state_dir,
        )

        dust_signals = []
        for path in created:
            with open(path, "r", encoding="utf-8") as fh:
                sig = json.load(fh)
            if sig.get("action") == "swap_to_usdc":
                dust_signals.append(sig["mint"])
        self.assertIn("WBTC", dust_signals)

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


class CloseReemitGateTests(unittest.TestCase):
    """Bug #6 (live 2026-10-10): Sheldon re-emitted a close for a position
    George had already closed. The journal-based re-emit gate suppresses
    duplicate close/claim signals and surfaces a review item instead."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.journal_dir = os.path.join(self.root, "journal")
        os.makedirs(self.journal_dir, exist_ok=True)
        self.signals_dir = os.path.join(self.root, "signals")
        self.pos_dir = os.path.join(self.root, "positions")
        os.makedirs(self.pos_dir, exist_ok=True)
        with open(os.path.join(self.pos_dir, "position_scan-x.json"), "w") as fh:
            json.dump({"positions": []}, fh)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_journal(self, records):
        path = os.path.join(self.journal_dir, "2026-10-10.jsonl")
        with open(path, "w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

    @staticmethod
    def _close_record(signal_id, position_id, pool, age_seconds=60.0):
        ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        return {"timestamp_utc": ts.isoformat(), "signal_id": signal_id,
                "action": "close", "decision": "executed",
                "pool_address": pool, "position_id": position_id}

    @staticmethod
    def _close_verdict(position_id, pool):
        # Position-rubric evidence: close-family verdicts without it are
        # demoted as open-rubric artifacts (see write_signals).
        return {"action": "CLOSE", "position": position_id,
                "pool": "POOL", "pool_address": pool, "dex": "meteora",
                "score": 10.0, "lower_bound": 0, "upper_bound": 10,
                "evidence": {"range_status": 0.0, "fee_capture": 26.32,
                             "staleness": 0.0, "depeg_exposure": 0.0,
                             "il_risk": 31.57},
                "reason": "test"}

    def _report(self, verdicts):
        return {"wallet": {}, "verdicts": verdicts, "open_skipped": [],
                "strategies": [], "funding": {"funded": [], "unfunded": [],
                                              "prep_swaps_allowed": [],
                                              "prep_swaps_blocked": []}}

    def test_recently_executed_closes_parses_journal(self):
        from readiness import prep_ledger
        self._write_journal([
            self._close_record("sig-1", "POS1", "POOLA"),
            self._close_record("sig-2", "POS2", "POOLB", age_seconds=7 * 3600),
            {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
             "signal_id": "sig-3", "action": "close", "decision": "failed",
             "pool_address": "POOLC", "position_id": "POS3"},
        ])
        recent = prep_ledger.recently_executed_closes(journal_dir=self.journal_dir)
        self.assertIn("POS1", recent["position_ids"])
        self.assertNotIn("POS2", recent["position_ids"])  # outside window
        self.assertNotIn("POS3", recent["position_ids"])  # not executed
        self.assertIn("POOLA", recent["pool_addrs"])
        self.assertNotIn("POOLC", recent["pool_addrs"])

    def test_close_reemit_suppressed_by_recent_journal_close(self):
        self._write_journal([self._close_record("sig-live-1", "POSX", "POOLA")])
        report = self._report([self._close_verdict("POSX", "POOLA")])
        created, review = write_signals(
            report, self.signals_dir, self.pos_dir, journal_dir=self.journal_dir)
        actions = []
        for path in created:
            with open(path) as fh:
                actions.append(json.load(fh)["action"])
        self.assertNotIn("close", actions)
        self.assertTrue(any("suppressed" in r.get("reason", "") for r in review))

    def test_unrelated_close_not_suppressed(self):
        self._write_journal([self._close_record("sig-live-1", "POSX", "POOLA")])
        report = self._report([self._close_verdict("POSY", "POOLB")])
        created, review = write_signals(
            report, self.signals_dir, self.pos_dir, journal_dir=self.journal_dir)
        actions = []
        for path in created:
            with open(path) as fh:
                actions.append(json.load(fh)["action"])
        self.assertIn("close", actions)
        self.assertFalse(any("suppressed" in r.get("reason", "") for r in review))

    def test_claim_suppressed_for_closed_position(self):
        self._write_journal([self._close_record("sig-live-1", "POSX", "POOLA")])
        verdict = self._close_verdict("POSX", "POOLA")
        verdict["action"] = "COLLECT_FEES"
        report = self._report([verdict])
        created, review = write_signals(
            report, self.signals_dir, self.pos_dir, journal_dir=self.journal_dir)
        actions = []
        for path in created:
            with open(path) as fh:
                actions.append(json.load(fh)["action"])
        self.assertNotIn("claim_fees", actions)

    def test_resolve_entry_marks_manual(self):
        from run_cycle.close_settlement import resolve_entry, load_close_state
        state_dir = os.path.join(self.root, "state")
        os.makedirs(state_dir, exist_ok=True)
        from run_cycle.close_settlement import save_close_state, CLOSE_STATE_FILENAME
        save_close_state(state_dir, {"sig-stuck": {
            "status": "CLOSE_UNKNOWN_TIMEOUT", "pool_address": "POOLA"}})
        rc = resolve_entry(state_dir, "sig-stuck", note="verified closed on-chain")
        self.assertEqual(rc, 0)
        entry = load_close_state(state_dir)["sig-stuck"]
        self.assertEqual(entry["status"], "CLOSE_RESOLVED")
        self.assertTrue(entry["manual"])
        self.assertIn("on-chain", entry["note"])
        # already-settled is a no-op success
        rc2 = resolve_entry(state_dir, "sig-stuck")
        self.assertEqual(rc2, 0)
        # unknown id -> exit 1
        rc3 = resolve_entry(state_dir, "sig-never-existed")
        self.assertEqual(rc3, 1)


class PoolFeaturesV1Tests(unittest.TestCase):
    """v1 gate/ranker integration through run_cycle's live entry points."""

    GATE = {"version": 1, "enabled": True, "require_range_ready": True}
    RANKER = dict(pool_features.DEFAULT_RANKER, enabled=True)

    def _pool(self, addr, *, score=90.0, eligible=True, range_ready=True,
              apr=100.0, in_range=0.5, vol=2.0, ref=1.0, reject=None):
        return {
            "pool_features_version": 1,
            "name": "SOL-USDC", "pool_address": addr, "dex": "orca",
            "score": score, "eligible": eligible,
            "pair_class": "stable_bluechip",
            "token_x_symbol": "SOL", "token_y_symbol": "USDC",
            "token_x_decimals": 9, "token_y_decimals": 6,
            "token_x_price_usd": 100.0, "token_y_price_usd": 1.0,
            "tvl": 200_000.0, "volume_window": 2_000_000.0,
            "volatility": vol, "current_tick_index": 100,
            "bin_step": None, "tick_spacing": 4,
            "rejected_reason": ", ".join(reject or []),
            "safety": {"eligible": eligible,
                       "reject_codes": list(reject or []),
                       "thresholds_used": {"policy_version": 2}},
            "data_quality": {"complete": range_ready,
                             "range_ready": range_ready,
                             "missing": [] if range_ready else ["range_ready"]},
            "fee_capture": {"realized_fee_apr": apr},
            "expected_in_range_frac": in_range,
            "expected_in_range": {
                "horizon_days": 3.0, "reference_half_width_pct": ref,
                "reference_half_width_units": 100, "width_unit": "tick",
                "vol_daily_pct": vol,
            },
            "il_estimate_pct": 0.1,
            "slippage_estimate_bps": {"100": 0.0, "1000": 0.0},
        }

    def _candidate(self, pool, score=None):
        return {"action": "OPEN_CANDIDATE", "pool": pool["name"],
                "pool_address": pool["pool_address"], "dex": pool["dex"],
                "pair_class": pool["pair_class"],
                "score": pool["score"] if score is None else score,
                "_pool": pool}

    def test_open_filter_keeps_low_score_safety_survivor(self):
        good = self._pool("GOOD", score=40.0)
        bad = self._pool("BAD", score=99.0, eligible=False,
                         reject=["low_tvl"])
        kept, skipped = _filter_open_candidates(
            [self._candidate(good), self._candidate(bad)], {},
            policy_windows={"open_window_utc": "00:00-23:59",
                            "blackout_dates": []},
            pool_gate=self.GATE,
        )
        self.assertEqual([v["pool_address"] for v in kept], ["GOOD"])
        self.assertEqual(skipped[0]["reject_codes"], ["low_tvl"])

    def test_build_strategies_ranks_by_net_not_score(self):
        whale = self._pool("WHALE", score=99.0, apr=20.0, vol=6.0)
        fee_rich = self._pool("FEE", score=40.0, apr=400.0, vol=1.0)
        with patch.object(pool_features, "ranker_policy",
                          return_value=self.RANKER):
            strategies = build_strategies(
                [self._candidate(whale), self._candidate(fee_rich)],
                {"idle_usdc": 500.0, "deployable_usdc": 500.0})
        self.assertEqual([s["pool_address"] for s in strategies],
                         ["FEE", "WHALE"])
        self.assertGreater(strategies[0]["net_daily_pct"],
                           strategies[1]["net_daily_pct"])

    def test_ranker_flag_off_keeps_score_order(self):
        whale = self._pool("WHALE", score=99.0, apr=20.0)
        fee_rich = self._pool("FEE", score=40.0, apr=400.0)
        with patch.object(pool_features, "ranker_policy",
                          return_value=dict(self.RANKER, enabled=False)):
            strategies = build_strategies(
                [self._candidate(fee_rich), self._candidate(whale)],
                {"idle_usdc": 500.0, "deployable_usdc": 500.0})
        self.assertEqual([s["pool_address"] for s in strategies],
                         ["WHALE", "FEE"])
