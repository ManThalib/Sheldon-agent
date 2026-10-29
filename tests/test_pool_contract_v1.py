#!/usr/bin/env python3
"""Missy pool_features v1 consumer tests (gate, ranker, rails, fixture).

Every test here is deterministic and stdlib-only.  The authoritative
fixture lives in the Missy repo (read-only); tests skip if it is absent.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pool_features
from run_cycle import _filter_open_candidates, write_signals
from strategy.candidate_filtering import _rank_candidates, build_strategies
from strategy.sheldon_selector import select_strategy

MISSY_FIXTURE = ("/Users/manthalib/Missy-agent/pool_screener/tests/fixtures/"
                 "pool_scan_2026-10-29.json")

GATE_ON = {"version": 1, "enabled": True, "require_range_ready": True,
           "v0_fallback": "legacy"}
GATE_ON_NO_RANGE = dict(GATE_ON, require_range_ready=False)
RANKER_ON = dict(pool_features.DEFAULT_RANKER, enabled=True)


def _v1_pool(addr="P1", *, score=90.0, eligible=True, reject_codes=None,
             range_ready=True, apr=100.0, in_range=0.5, vol=2.0,
             ref_width=1.0, slippage=None, il=0.1, dex="orca",
             bin_step=None, tvl=100_000.0, volume=1_000_000.0):
    return {
        "pool_features_version": 1,
        "name": "SOL-USDC",
        "pool_address": addr,
        "dex": dex,
        "score": score,
        "eligible": eligible,
        "pair_class": "stable_bluechip",
        "token_x_symbol": "SOL", "token_y_symbol": "USDC",
        "token_x_price_usd": 100.0, "token_y_price_usd": 1.0,
        "token_x_decimals": 9, "token_y_decimals": 6,
        "tvl": tvl, "volume_window": volume,
        "volatility": vol,
        "bin_step": bin_step,
        "tick_spacing": 4 if dex != "meteora" else bin_step,
        "current_tick_index": 100,
        "active_bin_id": 100 if dex == "meteora" else None,
        "rejected_reason": (", ".join(reject_codes) if reject_codes else ""),
        "safety": {
            "eligible": eligible,
            "reject_codes": list(reject_codes or []),
            "metrics": {},
            "thresholds_used": {"min_tvl_usd": 10_000.0, "policy_version": 2},
        },
        "data_quality": {
            "complete": range_ready,
            "missing": [] if range_ready else ["range_ready"],
            "range_ready": range_ready,
            "prices_known": True,
            "missing_optional": [],
        },
        "fee_capture": {
            "realized_fee_apr": apr,
            "volume_window": volume,
            "tvl": tvl,
            "fee_tvl_ratio": apr / 365.0,
        },
        "expected_in_range_frac": in_range,
        "expected_in_range": {
            "horizon_days": 3.0,
            "reference_half_width_pct": ref_width,
            "reference_half_width_units": 100,
            "width_unit": "tick",
            "width_source": "orca_tick_spacing_4",
            "vol_daily_pct": vol,
            "sigma_daily_pct": vol / 4.0,
            "model": "bm-barrier-v1",
            "curve": {},
        },
        "il_estimate_pct": il,
        "slippage_estimate_bps": (
            {"100": 0.0, "1000": 0.0} if slippage is None else slippage
        ),
        "slippage_model": {"depth_proxy_usd": 1_000_000.0},
        "lp_lock": None,
        "pool_age_days": None,
        "tvl_trend_24h_pct": None,
        "owner_concentration_pct": None,
    }


def _candidate(pool, score=None):
    return {
        "action": "OPEN_CANDIDATE",
        "pool": pool.get("name"),
        "pool_address": pool.get("pool_address"),
        "dex": pool.get("dex"),
        "pair_class": pool.get("pair_class"),
        "score": pool.get("score") if score is None else score,
        "_pool": pool,
    }


class GateTests(unittest.TestCase):
    def test_safety_only_score_40_is_kept_but_score_99_ineligible_is_rejected(self):
        good = _v1_pool("GOOD", score=40.0, apr=120.0, in_range=0.8, vol=1.0)
        bad = _v1_pool("BAD", score=99.0, eligible=False,
                       reject_codes=["low_tvl", "high_vol"])

        with patch.object(pool_features, "gate_policy", return_value=GATE_ON), \
                patch.object(pool_features, "ranker_policy",
                             return_value=dict(RANKER_ON, enabled=False)):
            self.assertTrue(pool_features.gate_v1(good)["ok"])
            import lp_scoring
            kept_good = lp_scoring.score_pool_missy(good)
            kept_bad = lp_scoring.score_pool_missy(bad)
        self.assertEqual(kept_good["verdict"], "OPEN_CANDIDATE")
        self.assertEqual(kept_good["score"], 40.0)   # audit only
        self.assertEqual(kept_bad["verdict"], "IGNORE")
        self.assertEqual(kept_bad["score"], 99.0)
        self.assertIn("low_tvl", kept_bad["reason"])
        self.assertIn("high_vol", kept_bad["reason"])

    def test_filter_keeps_low_score_and_reports_reject_codes(self):
        good = _v1_pool("GOOD", score=40.0, range_ready=True)
        bad = _v1_pool("BAD", score=99.0, eligible=False,
                       reject_codes=["blacklisted"])
        with patch.object(pool_features, "gate_policy", return_value=GATE_ON):
            kept, skipped = _filter_open_candidates(
                [_candidate(good), _candidate(bad)], {},
                policy_windows={"open_window_utc": "00:00-23:59",
                                "blackout_dates": []},
                pool_gate=GATE_ON,
            )
        self.assertEqual([v["pool_address"] for v in kept], ["GOOD"])
        self.assertEqual(skipped[0]["reject_codes"], ["blacklisted"])
        self.assertIn("blacklisted", skipped[0]["reason"])

    def test_range_ready_is_the_deployability_gate(self):
        pool = _v1_pool("NO_TICK", range_ready=False)
        kept, skipped = _filter_open_candidates(
            [_candidate(pool)], {},
            policy_windows={"open_window_utc": "00:00-23:59",
                            "blackout_dates": []},
            pool_gate=GATE_ON,
        )
        self.assertEqual(kept, [])
        self.assertIn("no live tick/bin", skipped[0]["reason"])

        kept, skipped = _filter_open_candidates(
            [_candidate(pool)], {},
            policy_windows={"open_window_utc": "00:00-23:59",
                            "blackout_dates": []},
            pool_gate=GATE_ON_NO_RANGE,
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(skipped, [])

    def test_v0_regression_score_gate_and_legacy_backstop(self):
        v0_low = {"name": "SOL-USDC", "pool_address": "V0LOW", "dex": "orca",
                  "token_x_symbol": "SOL", "token_y_symbol": "USDC",
                  "score": 40.0, "tvl": 500_000.0, "volume_window": 2_000_000.0,
                  "current_tick_index": 100}
        v0_ok = dict(v0_low, pool_address="V0OK", score=95.0)
        kept, skipped = _filter_open_candidates(
            [_candidate(v0_low), _candidate(v0_ok)], {},
            policy_windows={"open_window_utc": "00:00-23:59",
                            "blackout_dates": []},
            pool_gate=GATE_ON,
        )
        self.assertEqual([v["pool_address"] for v in kept], ["V0OK"])
        self.assertIn("min_open_score", skipped[0]["reason"])

        # Legacy TVL backstop still applies when the old scan lacks eligible.
        thin = dict(v0_ok, pool_address="THIN", tvl=10.0, volume_window=1.0)
        thin.pop("eligible", None)
        kept, skipped = _filter_open_candidates(
            [_candidate(thin)], {},
            policy_windows={"open_window_utc": "00:00-23:59",
                            "blackout_dates": []},
            pool_gate=GATE_ON,
        )
        self.assertEqual(kept, [])
        self.assertIn("legacy policy", skipped[0]["reason"])

    def test_dual_model_disagreement_has_no_effect_on_v1_gate(self):
        pool = _v1_pool("DUAL", score=40.0)
        cand = _candidate(pool)
        cand.update({"new_fc_score": 12.0, "new_fc_verdict": "IGNORE"})
        kept, skipped = _filter_open_candidates(
            [cand], {},
            policy_windows={"open_window_utc": "00:00-23:59",
                            "blackout_dates": []},
            pool_gate=GATE_ON,
        )
        self.assertEqual([v["pool_address"] for v in kept], ["DUAL"])
        self.assertEqual(skipped, [])

    def test_score_value_has_no_effect_on_kept_set(self):
        kept_sets = []
        for score in (0.0, 100.0):
            pool = _v1_pool("SAME", score=score)
            kept, skipped = _filter_open_candidates(
                [_candidate(pool, score=score)], {},
                policy_windows={"open_window_utc": "00:00-23:59",
                                "blackout_dates": []},
                pool_gate=GATE_ON,
            )
            kept_sets.append({v["pool_address"] for v in kept})
            self.assertEqual(skipped, [])
        self.assertEqual(kept_sets[0], kept_sets[1])


class RankerTests(unittest.TestCase):
    def test_formula(self):
        pool = _v1_pool(apr=365.0, in_range=0.5, vol=4.0, ref_width=1.0,
                        slippage={"100": 10.0, "1000": 100.0}, il=0.1)
        m = pool_features.compute_net_yield(
            pool, RANKER_ON, deployed_width_pct=1.0)
        inputs = m["inputs"]
        self.assertAlmostEqual(inputs["concentration"], 1.0)
        self.assertAlmostEqual(inputs["gross_daily_pct"], 0.5)
        self.assertAlmostEqual(inputs["variance_drag_pct"], 1 * 16 / 800.0)
        self.assertAlmostEqual(inputs["slippage_drag_pct"], (2 * 10 / 100) / 3.0)
        self.assertAlmostEqual(
            m["net_daily_pct"],
            0.5 - 16 / 800.0 - (2 * 10 / 100) / 3.0)

    def test_monotonicity(self):
        base = _v1_pool(apr=100.0, in_range=0.5, vol=2.0, ref_width=1.0)
        c = RANKER_ON
        n = lambda p, w=1.0: pool_features.compute_net_yield(p, c, w)["net_daily_pct"]

        self.assertGreater(n(dict(base, fee_capture={"realized_fee_apr": 200.0})),
                           n(base))
        self.assertLess(n(dict(base, expected_in_range={
            "vol_daily_pct": 4.0, "reference_half_width_pct": 1.0,
            "horizon_days": 3.0})), n(base))
        self.assertGreater(n(dict(base, expected_in_range_frac=0.9)), n(base))
        self.assertLess(n(base, 2.0), n(base, 1.0))
        wider = pool_features.compute_net_yield(base, c, 2.0)
        self.assertEqual(wider["inputs"]["concentration"], 2.0)

    def test_ranker_orders_by_net_and_ties_break_on_address(self):
        rich = _candidate(_v1_pool("B", apr=300.0))
        poor = _candidate(_v1_pool("A", apr=30.0))
        tied_a = _candidate(_v1_pool("A", apr=100.0))
        tied_b = _candidate(_v1_pool("B", apr=100.0))
        with patch.object(pool_features, "ranker_policy", return_value=RANKER_ON):
            ranked, metrics, cfg = _rank_candidates([poor, rich])
            self.assertEqual([v["pool_address"] for v in ranked], ["B", "A"])
            tied, _, _ = _rank_candidates([tied_b, tied_a])
            self.assertEqual([v["pool_address"] for v in tied], ["A", "B"])

    def test_score_monkeypatch_does_not_change_kept_set_or_ranking(self):
        pools = [_v1_pool("A", score=0.0, apr=90.0),
                 _v1_pool("B", score=100.0, apr=300.0)]
        with patch.object(pool_features, "ranker_policy", return_value=RANKER_ON):
            c0 = [_candidate(p, score=0.0) for p in pools]
            c100 = [_candidate(p, score=100.0) for p in pools]
            r0, _, _ = _rank_candidates(c0)
            r100, _, _ = _rank_candidates(c100)
        self.assertEqual([v["pool_address"] for v in r0],
                         [v["pool_address"] for v in r100])
        self.assertEqual([v["pool_address"] for v in r0], ["B", "A"])

    def test_wider_deployed_range_demotes(self):
        narrow = _candidate(_v1_pool("N", apr=100.0, vol=3.0, ref_width=1.0))
        wide = _candidate(_v1_pool("W", apr=100.0, vol=3.0, ref_width=1.0))
        cfg = dict(RANKER_ON, deployed_half_width_pct=3.0)
        with patch.object(pool_features, "ranker_policy", return_value=RANKER_ON):
            ranked_narrow, _, _ = _rank_candidates([narrow])
            ranked_wide, _, _ = _rank_candidates([wide])
        m_n = pool_features.compute_net_yield(narrow["_pool"], cfg, 1.0)
        m_w = pool_features.compute_net_yield(wide["_pool"], cfg, 3.0)
        self.assertGreater(m_w["inputs"]["concentration"],
                           m_n["inputs"]["concentration"])
        self.assertLess(m_w["net_daily_pct"], m_n["net_daily_pct"])

    def test_il_guard_is_a_cap_not_a_subtractive_term(self):
        loan = _v1_pool("LOWIL", il=0.1)
        heavy = _v1_pool("HIGHIL", il=50.0)
        cfg = dict(RANKER_ON, max_il_pct=5.0, il_policy="skip")
        low = pool_features.compute_net_yield(loan, cfg, 1.0)
        high = pool_features.compute_net_yield(heavy, cfg, 1.0)
        self.assertEqual(low["status"], "ranked")
        self.assertEqual(high["status"], "il_skip")
        self.assertFalse(high["rankable"])
        # Guard does not alter the net of a surviving pool.
        self.assertAlmostEqual(low["net_daily_pct"],
                               pool_features.compute_net_yield(
                                   loan, dict(cfg, max_il_pct=1000.0), 1.0
                               )["net_daily_pct"])

    def test_null_inputs_rank_last_and_do_not_crash(self):
        unknown = _v1_pool("UNK", in_range=None,
                           slippage={"100": None, "1000": None})
        unknown["lp_lock"] = None
        known = _v1_pool("KNOWN", apr=50.0)
        with patch.object(pool_features, "ranker_policy", return_value=RANKER_ON):
            ranked, metrics, _ = _rank_candidates(
                [_candidate(unknown), _candidate(known)])
        self.assertEqual([v["pool_address"] for v in ranked], ["KNOWN", "UNK"])
        self.assertIsNone(metrics["UNK"]["net_daily_pct"])
        self.assertFalse(metrics["UNK"]["rankable"])

    def test_unknown_rank_skip_drops_null_records(self):
        unknown = _v1_pool("UNK", in_range=None)
        known = _v1_pool("KNOWN", apr=50.0)
        cfg = dict(RANKER_ON, unknown_rank="skip")
        with patch.object(pool_features, "ranker_policy", return_value=cfg):
            ranked, _, _ = _rank_candidates(
                [_candidate(unknown), _candidate(known)])
        self.assertEqual([v["pool_address"] for v in ranked], ["KNOWN"])

    def test_strategy_selection_uses_net_threshold(self):
        cfg = {"min_tvl_usd": 0.0, "trend_threshold": 8,
               "vol_threshold_pct": 10.0, "high_net_daily_pct": 0.10}
        pool = {"tvl": 1_000_000.0}
        self.assertEqual(
            select_strategy(pool, 0.2, 50.0, 0, cfg, metric="net_daily_pct"),
            "BidAsk")
        self.assertEqual(
            select_strategy(pool, 0.05, 50.0, 0, cfg, metric="net_daily_pct"),
            "Spot")


class RailsTests(unittest.TestCase):
    def test_blackout_blocks_every_v1_open(self):
        pool = _v1_pool("P1")
        now = time.strftime("%Y-%m-%d")
        kept, skipped = _filter_open_candidates(
            [_candidate(pool)], {},
            policy_windows={"open_window_utc": "00:00-23:59",
                            "blackout_dates": [now]},
            pool_gate=GATE_ON,
        )
        self.assertEqual(kept, [])
        self.assertIn("blackout", skipped[0]["reason"])

    def test_dedup_already_holding(self):
        kept, skipped = _filter_open_candidates(
            [_candidate(_v1_pool("P1"))], {"P1": {"status": "active"}},
            policy_windows={"open_window_utc": "00:00-23:59",
                            "blackout_dates": []},
            pool_gate=GATE_ON,
        )
        self.assertEqual(kept, [])
        self.assertIn("dedup", skipped[0]["reason"])

    def test_rotation_reservation(self):
        kept, skipped = _filter_open_candidates(
            [_candidate(_v1_pool("P1"))], {},
            policy_windows={"open_window_utc": "00:00-23:59",
                            "blackout_dates": []},
            pool_gate=GATE_ON, reserved_pools={"P1"},
        )
        self.assertEqual(kept, [])
        self.assertIn("reserved", skipped[0]["reason"])

    def test_reopen_cooldown(self):
        kept, skipped = _filter_open_candidates(
            [_candidate(_v1_pool("P1"))], {},
            policy_windows={"open_window_utc": "00:00-23:59",
                            "blackout_dates": []},
            pool_gate=GATE_ON,
            cooldown_pools={"P1": time.time() + 3600},
        )
        self.assertEqual(kept, [])
        self.assertIn("cooldown", skipped[0]["reason"])

    def test_bin_step_rail_still_applies_to_v1_meteora(self):
        pool = _v1_pool("M1", dex="meteora", bin_step=1)
        kept, skipped = _filter_open_candidates(
            [_candidate(pool)], {},
            policy_windows={"open_window_utc": "00:00-23:59",
                            "blackout_dates": []},
            pool_gate=GATE_ON,
        )
        self.assertEqual(kept, [])
        self.assertIn("bin_step", skipped[0]["reason"])

    def test_min_position_size_still_cuts_v1(self):
        pool = _v1_pool("P1")
        with patch.object(pool_features, "ranker_policy",
                          return_value=dict(RANKER_ON, enabled=False)):
            self.assertEqual(build_strategies([_candidate(pool)],
                                              {"idle_usdc": 5.0,
                                               "deployable_usdc": 5.0}), [])

    def test_readiness_still_blocks_v1_open_signal(self):
        pool = _v1_pool("P1")
        cand = _candidate(pool)
        strategy = {"pool_address": "P1", "suggested_usdc": 50.0,
                    "center": 100, "half_width": 10,
                    "bin_range": {"lower": 90, "upper": 110},
                    "score": pool["score"]}
        report = {
            "verdicts": [cand],
            "strategies": [strategy],
            "wallet": {},
            "open_skipped": [],
            "funding": {"funded": [], "unfunded": [],
                        "prep_swaps_allowed": [], "prep_swaps_blocked": [],
                        "notes": []},
            "readiness": {},
        }
        root = tempfile.mkdtemp()
        try:
            signals_dir = os.path.join(root, "signals")
            pos_dir = os.path.join(root, "positions")
            os.makedirs(pos_dir)
            with open(os.path.join(pos_dir, "position_scan-x.json"), "w") as fh:
                json.dump({"positions": []}, fh)
            created, review = write_signals(
                report, signals_dir, pos_dir,
                min_open_score=90.0, supported_dexes={"orca"},
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(created, [])
        self.assertTrue(any("readiness gate" in r.get("reason", "")
                            for r in review))

    def test_determinism_same_scan_same_strategy_order(self):
        pools = [_v1_pool("A", apr=150.0), _v1_pool("B", apr=300.0),
                 _v1_pool("C", apr=50.0)]
        with patch.object(pool_features, "ranker_policy", return_value=RANKER_ON):
            one = build_strategies([_candidate(p) for p in pools],
                                   {"idle_usdc": 500.0})
            two = build_strategies([_candidate(p) for p in pools],
                                   {"idle_usdc": 500.0})
        self.assertEqual([s["pool_address"] for s in one],
                         [s["pool_address"] for s in two])
        self.assertEqual([s["net_daily_pct"] for s in one],
                         [s["net_daily_pct"] for s in two])


@unittest.skipUnless(os.path.exists(MISSY_FIXTURE),
                     "Missy v1 fixture not available")
class MissyFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(MISSY_FIXTURE, "r", encoding="utf-8") as fh:
            cls.pools = json.load(fh)

    def test_fixture_is_v1_and_gate_counts(self):
        self.assertEqual(len(self.pools), 67)
        self.assertTrue(all(p.get("pool_features_version") == 1
                            for p in self.pools))
        eligible = [p for p in self.pools
                    if (p.get("safety") or {}).get("eligible")]
        range_ready = [p for p in self.pools
                       if (p.get("data_quality") or {}).get("range_ready")]
        self.assertEqual(len(eligible), 26)
        self.assertEqual(len(range_ready), 7)

        gated = [p for p in eligible if pool_features.gate_v1(p)["ok"]]
        self.assertEqual(len(gated), 6)
        self.assertEqual(len(gated),
                         len([p for p in range_ready
                              if (p.get("safety") or {}).get("eligible")]))
        relaxed = [p for p in eligible
                   if pool_features.gate_v1(
                       p, require_range_ready=False)["ok"]]
        self.assertEqual(len(relaxed), 26)

    def test_fixture_reject_codes_are_surfaced(self):
        rejected = [p for p in self.pools
                    if not (p.get("safety") or {}).get("eligible")]
        self.assertTrue(rejected)
        for pool in rejected[:10]:
            gate = pool_features.gate_v1(pool)
            self.assertFalse(gate["ok"])
            self.assertTrue(gate["reject_codes"])
            for code in gate["reject_codes"]:
                self.assertIn(code, gate["reason"])

    def test_top10_old_score_vs_net_yield(self):
        cfg = dict(RANKER_ON, deployed_half_width_pct=None)
        by_score = sorted(self.pools,
                          key=lambda p: p.get("score") or 0.0,
                          reverse=True)[:10]
        measured = []
        for pool in self.pools:
            m = pool_features.compute_net_yield(pool, cfg)
            if m["rankable"]:
                measured.append((pool, m["net_daily_pct"]))
        by_net = [p for p, _ in sorted(
            measured, key=lambda x: (-x[1], x[0].get("pool_address") or ""))][:10]
        self.assertEqual(len(by_net), 10)
        # The two orderings must not be identical: that is the bug being fixed.
        self.assertNotEqual([p["pool_address"] for p in by_score],
                            [p["pool_address"] for p in by_net])

    def test_fixture_ranker_is_deterministic(self):
        cfg = dict(RANKER_ON, deployed_half_width_pct=None)
        first = [(p["pool_address"],
                  pool_features.compute_net_yield(p, cfg)["net_daily_pct"])
                 for p in self.pools]
        second = [(p["pool_address"],
                   pool_features.compute_net_yield(p, cfg)["net_daily_pct"])
                  for p in self.pools]
        self.assertEqual(first, second)

    def test_phase_b_shadow_format_and_counts(self):
        report = pool_features.shadow_report(
            self.pools, gate_cfg=GATE_ON, ranker_cfg=RANKER_ON)
        self.assertEqual(report["contract_version"], 1)
        self.assertEqual(report["gate"]["v1_records"], 67)
        self.assertEqual(report["gate"]["safety_eligible"], 26)
        self.assertEqual(report["gate"]["safety_ready"], 6)
        self.assertEqual(len(report["gate"]["kept"]), 6)
        # Every skipped record carries a reason/codes attribution.
        for item in report["gate"]["skipped"]:
            self.assertTrue(item["reason"])
        self.assertTrue(report["gate"]["flips_in"])
        self.assertTrue(report["ranker"]["top_by_old_score"])
        self.assertEqual(len(report["ranker"]["inversions"]), 3)
        self.assertEqual(report, pool_features.shadow_report(
            self.pools, gate_cfg=GATE_ON, ranker_cfg=RANKER_ON))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class AdaptiveRetagRemovalTests(unittest.TestCase):
    def test_weak_v1_universe_is_not_retagged(self):
        import lp_scoring

        root = tempfile.mkdtemp()
        try:
            pools_dir = os.path.join(root, "pools")
            pos_dir = os.path.join(root, "positions")
            os.makedirs(pools_dir)
            os.makedirs(pos_dir)
            pools = [_v1_pool(f"P{i}", score=50.0) for i in range(25)]
            scan = os.path.join(pools_dir, "pool_scan-x.json")
            with open(scan, "w") as fh:
                json.dump(pools, fh)
            now = time.time()
            os.utime(scan, (now, now))
            pos = os.path.join(pos_dir, "position_scan-x.json")
            with open(pos, "w") as fh:
                json.dump({"positions": []}, fh)
            os.utime(pos, (now, now))

            with patch.object(pool_features, "gate_policy",
                              return_value=GATE_ON):
                report = lp_scoring.run_cycle(pools_dir, pos_dir)

            # No adaptive re-tag: every v1 verdict is the static safety verdict.
            self.assertNotIn("dynamic_thresholds", report)
            self.assertEqual(len(report["pool_scores"]), 25)
            for s in report["pool_scores"]:
                self.assertEqual(s["verdict"], "OPEN_CANDIDATE")
                self.assertEqual(s["dynamic"]["thresholds"], "safety")
        finally:
            shutil.rmtree(root, ignore_errors=True)
