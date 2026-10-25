# Patch 03 — Sequence capital prep after the sizing gate

**Status:** DRAFT for review (owner-requested 2026-10-25). Not applied.

## Symptom (live evidence)

2026-10-24, wallet ~$94, three open legs attempted:

- 08:45 prep BUY SOL lands → opens BGm1tav + 3msVd34R5K ($26.82 each). Good.
- 11:35 / 13:35 prep BUY SOL for a third open on 5rCf1DM8 — both execute.
- ~13:37 prep SELL "SOL surplus 0.088153 (~$8.93) back to USDC after funding
  opens" — the surplus of the duplicated buys, sold back one leg earlier.
- 15:35 both positions close (out of range); 15:36 prep BUY SOL fires AGAIN
  for 5rCf1DM8.
- Since then: zero opens. The candidate the preps were funding never opened.

Six swap legs (3 buys, 1 surplus sell, plus the failed/blocked attempts and
the Jupiter 429 series) paid gas, slippage and price risk for an open the
planner refused at the sizing gate (`suggested $14.77 < $20 floor`), leaving
$19.69 USDC stranded against $83.87 unusable SOL.

## Root cause

`run_cycle.py` line 646 calls `plan_funding(report["strategies"], ...)` on the
strategy list **before** the readiness gate (line 706) and **before** the
capital split (line ~776). `write_signals` then writes every allowed prep
(section 0b) with no check that the prep's target survived gates that run
later. The open loop checks `pool_addr not in strategies` and defers; the prep
loop has no such check.

## Change

Gate prep emission on the final strategy list and the per-strategy size —
the same two facts that decide whether an open signal can be built. Preps are
unchanged for candidates that pass; a prep for a candidate that cannot open
this cycle (below the $20 floor, no center/range, below score, unsupported
dex) becomes a review item instead of a chain transaction.

### `run_cycle/signals.py` — replace the 0b loop

```diff
+    # Sizing-gate filter for prep targets. plan_funding() runs on the
+    # pre-split strategy list, so an allowed prep can otherwise pay swap
+    # costs for an open the split/readiness/sizing gates then refuse
+    # (live 2026-10-24: 3 BUYs + 1 surplus sell for an open whose suggested
+    # size never cleared the $20 floor). A prep is only worth a transaction
+    # when its target pool has an OPEN_CANDIDATE that passes every open
+    # gate this cycle; the open itself may still wait a cycle for the prep
+    # to land and the wallet to rescan — that ordering is correct.
+    from run_cycle.gates import MIN_POSITION_USD as _MIN_POS_USD
+    sized_pools = set()
+    for v in report.get("verdicts", []):
+        if v.get("action") != "OPEN_CANDIDATE":
+            continue
+        pool_addr = v.get("pool_address")
+        dex = v.get("dex") or "unknown"
+        strategy = strategies.get(pool_addr) or {}
+        try:
+            score_ok = v.get("score") is not None and float(v["score"]) >= min_open_score
+        except (TypeError, ValueError):
+            score_ok = False
+        if not (pool_addr in strategies and dex in supported_dexes
+                and score_ok
+                and strategy.get("center") is not None
+                and strategy.get("bin_range")
+                and float(strategy.get("suggested_usdc") or 0.0) >= _MIN_POS_USD):
+            continue
+        sized_pools.add(pool_addr)
+
     # 0b. Allowed prep swaps execute before any position signals.
     for spec in allowed_preps:
+        if spec.get("for_pool") not in sized_pools:
+            review.append({
+                "pool_address": spec.get("for_pool"),
+                "dex": "jupiter",
+                "score": (strategies.get(spec.get("for_pool")) or {}).get("score"),
+                "evidence": None,
+                "reason": ("prep deferred: target pool has no OPEN_CANDIDATE "
+                           "passing the sizing gates this cycle (prep runs "
+                           "after the sizing gate, not before)"),
+            })
+            continue
         signal = build_prep_swap_signal(spec, idx, base)
         path = os.path.join(signals_dir, f"{signal['signal_id']}.json")
         with open(path, "w", encoding="utf-8") as fh:
             json.dump(signal, fh, indent=2)
             fh.write("\n")
         created.append(path)
         if state_dir:
             prep_ledger.record_emitted(spec, signal["signal_id"], state_dir=state_dir)
         idx += 1
```

Interaction with patch 01: with swappable SOL counted, `suggested_usdc`
becomes real capital-based sizing, so the gate does not block preps for
properly sized candidates — it only stops preps for candidates that could
never open this cycle.

## Tests — new file `tests/test_prep_sequencing.py`

```python
class PrepFollowsSizingGate(unittest.TestCase):
    def _report(self, suggested, score=96.0, in_strategies=True):
        strategies = [{
            "pool_address": "POOL1", "score": score,
            "suggested_usdc": suggested, "center": -2300,
            "bin_range": {"lower": -2320, "upper": -2280},
        }] if in_strategies else []
        return {
            "wallet": {}, "verdicts": [{
                "action": "OPEN_CANDIDATE", "pool_address": "POOL1",
                "dex": "meteora", "score": score,
                "evidence": {"fee_yield": 25.0}}],
            "strategies": strategies,
            "funding": {"prep_swaps_allowed": [{
                "direction": "buy", "for_pool": "POOL1", "symbol": "SOL",
                "input_mint": SOL_MINT, "output_mint": SOL_MINT,
                "amount": "1000", "need_ui": 0.12, "have_ui": 0.0}],
                "prep_swaps_blocked": []},
        }

    def test_prep_emitted_when_candidate_passes_sizing(self):
        created, review = write_signals(
            self._report(24.0), signals_dir=tmp, positions_dir=tmp,
            min_open_score=90.0, supported_dexes={"meteora"})
        self.assertEqual(len(created), 1)

    def test_prep_deferred_when_size_below_floor(self):
        # The live 2026-10-24 shape: suggested $14.77 < $20 floor.
        created, review = write_signals(
            self._report(14.77), signals_dir=tmp, positions_dir=tmp,
            min_open_score=90.0, supported_dexes={"meteora"})
        self.assertEqual(created, [])
        self.assertIn("prep deferred", review[0]["reason"])

    def test_prep_deferred_when_pool_dropped_from_strategies(self):
        # Capital split / readiness dropped it after plan_funding ran.
        created, review = write_signals(
            self._report(24.0, in_strategies=False), ...)
        self.assertEqual(created, [])
```

## Verification (after apply)

1. `python3 -m unittest tests.test_prep_sequencing tests.test_prep_sol_source tests.test_prep_ledger -v`
2. Watch `signals/pending/` across two live cycles: no prep JSON for a pool
   absent from the same cycle's `report["strategies"]`; `signals_review`
   gains "prep deferred" lines when a prep would have wasted a leg.
3. Ledger swap legs per day should drop to ≤ 2 per actual open.

## Blast radius / rollback

Emission-side only. Deferring a prep defers the open by one cycle at worst
(the prep re-emits on the next scan once the candidate passes the gate). No
George-executor change. Rollback = revert the hunk.
