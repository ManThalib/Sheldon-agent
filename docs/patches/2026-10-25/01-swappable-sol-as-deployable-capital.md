# Patch 01 — Count swappable idle SOL as deployable capital

**Status:** DRAFT for review (owner-requested 2026-10-25). Not applied.

## Symptom (live evidence)

2026-10-25 07:35 UTC report: wallet `total_usd=103.5593` = `idle_usdc 19.69`
+ `idle_sol_usd 83.87` (0.826 SOL above the 0.06 reserve). The capital plan saw
only `deployable_usdc=19.69` → `suggested_position_usdc=14.77` → below the
$20 floor → `open_eligible=false`, `strategies=[]` for ~17 consecutive hours
(post the 2026-10-24 15:36 close+swap). 81% of the wallet was invisible to
the open path. Root cause: the sizing layer treats every non-stable asset as
unswappable dust, but idle SOL is this pipeline's own X-side asset, and
`readiness/funding.py::plan_funding` already converts it (sell SOL → USDC)
when an open needs the stable side.

## Root cause

- `strategy/position_sizing.py::open_eligible` / `suggested_position_usd`
  consume `wallet["deployable_usdc"]`, which `capital.py::summarize_wallet`
  computes stablecoin-only by design (regression test 2026-10-18: WBTC must
  not count). SOL was lumped into that exclusion.

## Change

Sizing reads a new `deployable_capital_usd(wallet)` = stablecoin PLUS
swappable idle SOL (above the calibrated `sol.reserve_lamports` reserve,
haircut `sol.swappable_haircut` for swap friction). Dust / `liquidation_required_usd`
assets stay excluded — their test keeps pinning the old behavior.

### 1. `sheldon_policy.json` — one new key under the existing `sol` object

```diff
   "sol": {
     "_comment": "Phase 1 (2026-10-09): real rent/fee/reserve model. ...",
     "reserve_lamports": 60000000,
     "auto_top_up_enabled": true,
     "auto_top_up_source_mint": "USDC",
     "max_top_up_usd": 10,
     "top_up_buffer_pct": 20,
     "min_post_top_up_lamports": 20000000,
+    "_swappable_comment": "Owner ruling 2026-10-25: idle SOL above reserve_lamports is deployable capital (it is the pipeline's X-side asset; plan_funding converts it when an open needs the stable side).",
+    "swappable_haircut": 0.98
   },
```

No new reserve key: reuse the existing calibrated `sol.reserve_lamports`
(60,000,000 = George's worst-case open SOL overhead).

### 2. `strategy/sheldon_policy.py` — export the reserve + haircut

`_load_sheldon_policy` already passes the `sol` block through verbatim
(`policy["sol"] = dict(data.get("sol") or {})`), so only module constants
are needed:

```diff
 MIN_POSITION_USD: float = _POLICY["min_position_usd"]
 DEFAULT_MAX_POSITION_USD: float = _POLICY["default_max_position_usd"]
 MAX_POSITION_OPEN_PER_CYCLE: int = _POLICY["max_opens_per_cycle"]
+# Owner ruling 2026-10-25: swappable idle SOL. Reserve is the SAME value
+# readiness already passes to sol_overhead_lamports() (60M calibrated worst
+# case), so sizing and funding can never disagree about the reserve.
+SOL_RESERVE_LAMPORTS: int = int((_POLICY.get("sol") or {}).get("reserve_lamports", 60_000_000))
+SWAPPABLE_SOL_HAIRCUT: float = float((_POLICY.get("sol") or {}).get("swappable_haircut", 0.98))
```

### 3. `strategy/position_sizing.py` — new helpers, `open_eligible` uses them

```diff
-from strategy.sheldon_policy import MIN_POSITION_USD, DEFAULT_MAX_POSITION_USD
+from strategy.sheldon_policy import (MIN_POSITION_USD, DEFAULT_MAX_POSITION_USD,
+                                     SOL_RESERVE_LAMPORTS, SWAPPABLE_SOL_HAIRCUT)
+
+
+def swappable_sol_usd(wallet: Dict[str, Any],
+                      sol_reserve_lamports: int = SOL_RESERVE_LAMPORTS,
+                      haircut: float = SWAPPABLE_SOL_HAIRCUT) -> float:
+    """Idle SOL usable as deployable capital, net of the reserve and haircut.
+
+    Owner ruling 2026-10-25: a wallet that holds mostly SOL (after SOL-heavy
+    closes or capital-prep buys) must not read as dead to the planner. SOL is
+    not unswappable dust: it is the pipeline's X-side asset and plan_funding
+    sells it when an open needs the stable side. Reserve and haircut keep it
+    conservative; the result is 0.0 whenever the balances are unknown.
+    """
+    raw = float(wallet.get("idle_sol_raw_lamports") or 0.0)
+    usd = float(wallet.get("idle_sol_usd") or 0.0)
+    if raw <= 0.0 or usd <= 0.0:
+        return 0.0
+    spendable_raw = max(0.0, raw - float(sol_reserve_lamports))
+    return (usd * spendable_raw / raw) * haircut
+
+
+def deployable_capital_usd(wallet: Dict[str, Any]) -> float:
+    """Deployable capital: stablecoin plus swappable idle SOL.
+
+    Dust / liquidation_required assets remain excluded (pinned by
+    tests/test_capital_stable_only.py)."""
+    idle = float(wallet.get("idle_stable_usd")
+                 if wallet.get("idle_stable_usd") is not None
+                 else wallet.get("idle_usdc") or 0.0)
+    return idle + swappable_sol_usd(wallet)
```

```diff
 def open_eligible(wallet: Dict[str, Any]) -> bool:
-    """Open gate: spendable stablecoin must cover the minimum, and the
-    suggested position must also clear the minimum.
-
-    Both inputs are stablecoin-only. Volatile assets (``dust_assets`` /
-    ``liquidation_required_usd``) are excluded: they must be sold first, and
-    until then they cannot fund an open. ``idle_stable_usd`` falls back to
-    ``idle_usdc`` so hand-built wallet dicts keep working.
-    """
-    deployable = float(wallet.get("deployable_usdc") or 0.0)
+    """Open gate: spendable stablecoin must cover the minimum, and the
+    suggested position must also clear the minimum.
+
+    Stablecoin covers the Y side directly; capital sizing additionally counts
+    swappable idle SOL above the reserve (owner ruling 2026-10-25). Volatile
+    *dust* (``dust_assets`` / ``liquidation_required_usd``) stays excluded:
+    it must be sold first, and until then it cannot fund an open.
+    ``idle_stable_usd`` falls back to ``idle_usdc`` so hand-built wallet
+    dicts keep working.
+    """
+    deployable = deployable_capital_usd(wallet)
     idle = float(wallet.get("idle_stable_usd")
                  if wallet.get("idle_stable_usd") is not None
                  else wallet.get("idle_usdc") or 0.0)
```

### 4. `strategy/capital_plan.py` — report section reads the same helper

```diff
-from strategy.position_sizing import suggested_position_usd, open_eligible
+from strategy.position_sizing import (suggested_position_usd, open_eligible,
+                                       deployable_capital_usd)
...
-    deployable = float(wallet.get("deployable_usdc") or 0.0)
+    deployable = deployable_capital_usd(wallet)
```

This also feeds `run_cycle.py`'s capital-split budget (line ~780:
`budget = report["capital_plan"]["deployable_usdc"]`), so the split now sizes
on total capital. With the live wallet (~$19.69 USDC + ~$76.4 swappable SOL)
the split funds 4 opens at ~$24 each — matching the owner's standing
≥4-positions preference with no rail change.

### 5. `strategy/candidate_filtering.py::build_strategies` — same helper

```diff
 from strategy.position_sizing import open_eligible, suggested_position_usd
+from strategy.position_sizing import deployable_capital_usd
...
-    deployable = float(wallet.get("deployable_usdc") or 0.0)
+    deployable = deployable_capital_usd(wallet)
```

## Tests

`tests/test_capital_stable_only.py` stays green unchanged: dust (WBTC/JUP)
still never counts, and no test there pins `suggested_position_usd` on a
SOL-holding wallet. Add a new class pinning the new behavior:

```python
class SwappableSolCountsOwnerRuling(unittest.TestCase):
    """Owner ruling 2026-10-25: swappable idle SOL is deployable capital."""

    def _wallet(self, idle_usdc, sol_usd, sol_raw):
        return {"idle_usdc": idle_usdc, "idle_stable_usd": idle_usdc,
                "idle_sol_usd": sol_usd, "idle_sol_raw_lamports": sol_raw,
                "dust_total_usd": 0.0, "deployable_usdc": idle_usdc}

    def test_sol_above_reserve_counts_with_haircut(self):
        w = self._wallet(19.69, 82.35, 765_931_542)   # 60M reserve kept
        # 19.69 + 82.35 * 0.98 = 100.39
        self.assertAlmostEqual(deployable_capital_usd(w), 100.39, places=1)
        self.assertTrue(open_eligible(w))

    def test_sol_within_reserve_is_not_deployable(self):
        w = self._wallet(19.69, 4.0, 40_000_000)      # below 60M reserve
        self.assertAlmostEqual(deployable_capital_usd(w), 19.69, places=6)
        self.assertFalse(open_eligible(w))            # 75% of 19.69 < $20

    def test_live_deadlock_wallet_is_eligible(self):
        # The exact 2026-10-25 07:35 shape: 19.69 USDC + 83.87 SOL.
        w = self._wallet(19.69, 83.87, 825_931_542)
        self.assertGreater(deployable_capital_usd(w), 95.0)
        self.assertTrue(open_eligible(w))
```

## Verification (after apply)

1. `python3 -m unittest tests.test_capital_stable_only tests.test_capital -v`
2. Watch the next hourly report: `capital_plan.deployable_usdc` ≈
   idle_usdc + swappable SOL, `suggested_position_usdc` ≈ 75% of it, and
   `open_eligible` true on the live wallet shape.
3. Report `wallet.sol_balance` fields remain untouched (Missy raw scan).

## Blast radius / rollback

Sizing layer only; no signal-schema, no George-executor, no ledger changes.
Rollback = revert the 5 file edits. If the haircut proves wrong in live
funding, tune `sol.swappable_haircut` in the manifest (no code change).
