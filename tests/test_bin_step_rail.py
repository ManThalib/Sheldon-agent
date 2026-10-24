"""Owner ruling 2026-10-19: Meteora bin_step >= 4 (and <= 400).

Covers the shared predicate, the two Sheldon call sites that used to carry
their own copies, and the idle-capital gate that previously compared nothing.
"""

import sys
import unittest
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy.bin_step_rail import (  # noqa: E402
    METEORA_MAX_BIN_STEP,
    METEORA_MIN_BIN_STEP,
    allowed_bin_step_set,
    meteora_bin_step_allowed,
    normalize_rule,
)


class PredicateTests(unittest.TestCase):
    def test_owner_minimum_is_inclusive(self):
        for step in (4, 10, 20, 25, 50, 100, 400):
            ok, reason = meteora_bin_step_allowed(step)
            self.assertTrue(ok, f"{step} must be allowed: {reason}")

    def test_steps_below_owner_minimum_are_rejected(self):
        for step in (1, 2, 3):
            ok, reason = meteora_bin_step_allowed(step)
            self.assertFalse(ok, f"{step} must be rejected")
            self.assertIn("below owner minimum", reason)

    def test_protocol_maximum_is_inclusive_and_401_rejected(self):
        self.assertTrue(meteora_bin_step_allowed(METEORA_MAX_BIN_STEP)[0])
        ok, reason = meteora_bin_step_allowed(401)
        self.assertFalse(ok)
        self.assertIn("protocol maximum", reason)

    def test_large_values_are_rejected_not_accepted(self):
        # The idle-capital gate used to return allowed for bin_step=999.
        for step in (401, 999, 10_000):
            self.assertFalse(meteora_bin_step_allowed(step)[0], step)

    def test_missing_zero_and_malformed_fail_closed(self):
        for value in (None, "", 0, "junk", [], {}, False, "0"):
            ok, _ = meteora_bin_step_allowed(value)
            self.assertFalse(ok, f"{value!r} must fail closed")

    def test_numeric_strings_are_accepted(self):
        self.assertTrue(meteora_bin_step_allowed("7")[0])

    def test_derived_set_cannot_disagree_with_predicate(self):
        steps = allowed_bin_step_set()
        self.assertEqual(min(steps), METEORA_MIN_BIN_STEP)
        self.assertEqual(max(steps), METEORA_MAX_BIN_STEP)
        for step in range(1, 402):
            self.assertEqual(step in steps,
                             meteora_bin_step_allowed(step)[0], step)


class RuleBoundTests(unittest.TestCase):
    def test_config_cannot_readmit_low_steps(self):
        rule = normalize_rule({"min": 1, "max": 400})
        self.assertEqual(rule["min"], 4)
        self.assertFalse(meteora_bin_step_allowed(2, rule)[0])

    def test_config_cannot_exceed_protocol_max(self):
        rule = normalize_rule({"min": 4, "max": 5000})
        self.assertEqual(rule["max"], 400)

    def test_legacy_list_is_treated_as_envelope(self):
        rule = normalize_rule([10, 20, 25, 50, 100])
        self.assertEqual(rule, {"min": 10, "max": 100})

    def test_malformed_rule_falls_back_to_owner_rule(self):
        self.assertEqual(normalize_rule({"min": "x"}), {"min": 4, "max": 400})
        self.assertEqual(normalize_rule(None), {"min": 4, "max": 400})


class CallSiteTests(unittest.TestCase):
    def test_gates_uses_shared_predicate(self):
        from run_cycle.gates import _meteora_bin_step_allowed

        self.assertTrue(_meteora_bin_step_allowed({"bin_step": 4}, "meteora")[0])
        self.assertTrue(_meteora_bin_step_allowed({"bin_step": 125}, "meteora")[0])
        self.assertFalse(_meteora_bin_step_allowed({"bin_step": 3}, "meteora")[0])
        self.assertFalse(_meteora_bin_step_allowed({"bin_step": 401}, "meteora")[0])
        # Non-Meteora pools are not judged by the bin-step rail.
        self.assertTrue(_meteora_bin_step_allowed({"bin_step": 0}, "orca")[0])

    def test_range_module_uses_shared_predicate(self):
        from strategy.range import _7d_volatility_based as r

        self.assertTrue(r.meteora_bin_step_allowed({"bin_step": 4}, "meteora")[0])
        self.assertFalse(r.meteora_bin_step_allowed({"bin_step": 2}, "meteora")[0])

    def test_idle_capital_gate_actually_compares(self):
        from idle_capital_sweep import pool_eligibility as pe

        # Regression: this returned (True, "") for any value, including 999.
        ok, reason = pe.meteora_bin_step_allowed({"bin_step": 999}, "meteora")
        self.assertFalse(ok)
        self.assertIn("protocol maximum", reason)
        self.assertFalse(pe.meteora_bin_step_allowed({"bin_step": 2}, "meteora")[0])
        self.assertTrue(pe.meteora_bin_step_allowed({"bin_step": 4}, "meteora")[0])
        self.assertFalse(pe.meteora_bin_step_allowed({"bin_step": None}, "meteora")[0])
        # Non-Meteora: bin_step is not applicable metadata.
        self.assertTrue(pe.meteora_bin_step_allowed({"bin_step": 0}, "raydium")[0])


class PolicySourceTests(unittest.TestCase):
    def test_policy_exposes_rule_and_has_no_dead_top_level_list(self):
        import json
        from strategy.sheldon_policy import METEORA_BIN_STEP_RULE

        data = json.loads(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sheldon_policy.json")).read())
        self.assertNotIn("allowed_bin_steps", data,
                         "dead top-level list must stay removed")
        self.assertIn("bin_step_rule", data["pool_eligibility"])
        self.assertEqual(METEORA_BIN_STEP_RULE, {"min": 4, "max": 400})


if __name__ == "__main__":
    unittest.main(verbosity=2)
