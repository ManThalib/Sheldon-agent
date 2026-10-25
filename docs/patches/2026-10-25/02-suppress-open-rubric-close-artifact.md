# Patch 02 — Suppress the open-rubric close artifact (with prejudice)

**Status:** DRAFT for review (owner-requested 2026-10-25). Not applied.

## Symptom (live evidence)

Every close cycle emits TWO close signals for the same position — one real
close-rubric verdict and one artifact:

```
sheldon-1792856108-2 | close | 7sBqRd5DDVvT | CLOSE score=94.17;
    evidence={"fee_yield": 24.9, "depth": 25.0, "turnover": 24.7,
              "volatility_fit": 19.5}; position_id repaired from fresh scan
    (verdict carried none)                      <-- POOL rubric on a close
sheldon-1792856108-4 | close | 7sBqRd5DDVvT | CLOSE score=57.89;
    evidence={"range_status": 0.0, "fee_capture": 26.32, "staleness": 0.0,
              "depeg_exposure": 0.0, "il_risk": 31.57}   <-- real verdict
```

At 2026-10-24 23:35:31 (+08) the **artifact was the signal that executed the
close**; the real 57.89 verdict failed dispatch one cycle later. A position
scored 94 by the rubric (HOLD territory) was closed by a verdict that carried
pool-rubric evidence and no position data. 45 close signals tracked vs 11
executed over the ledger's life — the artifact family is the majority.

## Root cause

1. `range_state.py::out_of_range_position_verdict` promotes any non-close
   verdict to CLOSE on a full-right out-of-range classification (correctly —
   that rail is deliberate). The promoted verdict keeps the ORIGINAL score
   and evidence.
2. When the position scorer falls back to the pool rubric (e.g. a thin
   `position_features` vector from Missy), that evidence is pool-shaped
   (`fee_yield`/`depth`/`turnover`/`volatility_fit`), so the promoted close
   reads `CLOSE score=94.17` with pool evidence.
3. `run_cycle/signals.py::write_signals` emits any close-family verdict whose
   `position_id` can be repaired from the fresh scan. It never checks WHICH
   rubric the evidence came from, so the artifact reaches George.

## Change

Two coordinated edits: the OOR promotion stamps its provenance, and the
signal writer refuses close-family verdicts that have neither position-rubric
evidence nor OOR provenance.

### 1. `range_state.py::out_of_range_position_verdict` — stamp provenance

```diff
     out[key] = "CLOSE"
     out["reason"] = ("full-right out-of-range: immediate close"
                      if side == "right"
                      else f"full-left out-of-range: {runs} consecutive runs; close")
     out["oor_side"] = side
     out["oor_consecutive_runs"] = runs
     out["oor_close_eligible"] = True
+    # Evidence provenance (2026-10-25): the close-signal writer must be able
+    # to prove WHY a verdict became a close. Stamp the OOR markers into the
+    # evidence so an OOR-promoted close is self-documenting even when the
+    # underlying score came from the pool rubric.
+    evidence = dict(s.get("evidence") or {})
+    evidence["oor_side"] = side
+    evidence["oor_consecutive_runs"] = runs
+    out["evidence"] = evidence
     return out
```

(The deferral branch already returns a HOLD with its own reason — untouched.)

### 2. `run_cycle/signals.py` — demote closes without position-rubric evidence

Insert immediately after the duplicate-close ghost gates and before
`lower = v.get("lower_bound")` in the close-family branch:

```diff
             if action in {"CLOSE", "REBALANCE", "ROTATE", "RECENTER"}:
                 suppress = close_global_reason or close_pool_reasons.get(pool_addr)
                 if suppress:
                     ...
                     continue
                 pending_sid = position_close_pending(close_state, pool_addr, pos_id)
                 if pending_sid:
                     ...
                     continue
+            # Open-rubric close artifact (live 2026-10-24, signal
+            # sheldon-1792856108-2: "CLOSE score=94.17" with pool-rubric
+            # evidence, whose real close-rubric verdict was 57.89 — and the
+            # artifact is the one that executed). A close decision must be
+            # evidenced by the POSITION rubric or by OOR provenance stamped
+            # by range_state. A close-family verdict carrying only pool
+            # rubric components (fee_yield/depth/turnover/volatility_fit)
+            # is demoted to review instead of reaching George.
+            if action in {"CLOSE", "REBALANCE", "ROTATE", "RECENTER"}:
+                _evidence = v.get("evidence") or {}
+                _pos_rubric = ("range_status", "fee_capture", "staleness",
+                               "depeg_exposure", "il_risk")
+                if not (any(k in _evidence for k in _pos_rubric)
+                        or _evidence.get("oor_side")):
+                    review.append({
+                        "pool_address": pool_addr,
+                        "dex": dex,
+                        "score": v.get("score"),
+                        "evidence": _evidence,
+                        "reason": ("close demoted: pool-rubric evidence on "
+                                   "close-family verdict, no position-rubric "
+                                   "components and no OOR provenance "
+                                   "(open-rubric close artifact)"),
+                    })
+                    continue
             lower = v.get("lower_bound")
```

Effect on the live cases: the artifact (`fee_yield...` only, no OOR stamp) is
demoted to a review item; the real 57.89 (`range_status` present) passes; an
OOR-promoted close passes via the new `oor_side` evidence stamp. `COLLECT_FEES`
is excluded from the check (claims carry no rubric).

## Tests — new file `tests/test_close_rubric_artifact.py`

```python
class OpenRubricCloseArtifact(unittest.TestCase):
    """A close-family verdict with pool-rubric evidence must never become a
    signal (live 2026-10-24: sheldon-1792856108-2 executed a real close)."""

    def _report(self, evidence, oor=False):
        ev = dict(evidence)
        if oor:
            ev["oor_side"] = "right"
        return {"verdicts": [{
            "action": "CLOSE", "pool_address": "POOL1", "dex": "meteora",
            "position": "POS1", "score": 94.17, "evidence": ev,
            "lower_bound": 100, "upper_bound": 200}]}

    def test_pool_rubric_close_is_demoted(self):
        created, review = write_signals(
            self._report({"fee_yield": 24.9, "depth": 25.0,
                          "turnover": 24.7, "volatility_fit": 19.5}),
            signals_dir=tmp, positions_dir=tmp)
        self.assertEqual(created, [])
        self.assertIn("open-rubric close artifact", review[0]["reason"])

    def test_position_rubric_close_passes(self):
        created, _ = write_signals(
            self._report({"range_status": 0.0, "fee_capture": 26.3,
                          "staleness": 0.0, "depeg_exposure": 0.0,
                          "il_risk": 31.5}), ...)
        self.assertEqual(len(created), 1)

    def test_oor_promoted_close_passes_via_provenance(self):
        created, _ = write_signals(
            self._report({"fee_yield": 24.9}, oor=True), ...)
        self.assertEqual(len(created), 1)
        payload = json.load(open(created[0]))
        self.assertIn("oor_side", payload["reason"])  # provenance survives
```

Also assert (regression, same file): `out_of_range_position_verdict` stamps
`oor_side`/`oor_consecutive_runs` into `evidence` on promotion.

## Verification (after apply)

1. `python3 -m unittest tests.test_close_rubric_artifact -v`
2. Next live close cycle: the processed/ directory must contain exactly ONE
   close signal per position, with either `range_status` in evidence or an
   `oor_side` marker.
3. The report's `signals_review` should begin listing demoted artifacts
   (expected: 1-2 per close cycle until the position scorer stops falling
   back to the pool rubric).

## Blast radius / rollback

Emission-side only. Close signals become strictly fewer — if a legitimate
close is wrongly demoted, it reappears next cycle once the position rubric
scores the position (the artifact family was re-emitted every cycle by
construction). Rollback = revert both hunks.
