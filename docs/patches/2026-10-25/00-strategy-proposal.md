# Range & Rotation Policy Replacement — PROPOSAL (pending owner ruling)

- **Date:** 2026-10-25
- **Author:** Miraa (strategy session, design only — nothing in this document has been applied)
- **Status:** 🟡 **PROPOSED — awaiting owner ruling.** No code/config changes made. Owner rules item-by-item before anything lands.
- **Scope:** replace the volatility-adaptive range width (`strategy/range/_7d_volatility_based.py`) and the rotation/recenter posture around it, per recommendation #5 from the 2026-10-25 review ("open only at max achievable width").

---

## 1. Diagnosis (verified evidence, not re-derived)

The current volatility-based range is not mis-tuned — it solves the wrong problem. Meteora caps one tx at **70 bins** (`max_meteora_range_width`, George SAFETY_RAILS §5), so the maximum price half-width is arithmetic:

| bin_step | Max half-width at 70 bins |
|---|---|
| 4bps | ±1.4% |
| 10bps | ±3.5% |

1. **Width is chosen by pool selection, not by the vol model.** The scorer ranks on `fee_tvl_ratio`, which rewards tight liquidity — so the 4bps pool (5rCf1DM8, 0.21) outranks the 10bps pool (BGm1tav, 0.17) exactly where the cap bites hardest. Selection systematically favors the pools that die fastest.
2. **Lifetime tracks width, not volatility.** Observed closes: ±1.1% → ~6h; ±1.2–1.35% → 1.3–6.8h (one 26h outlier); ±1.7% → 6.8h; ±2.7% → 39h. 5 of 7 closes were fully out of range (range_status = 0.0).
3. **Rotation cannot pay for itself at ~$103 capital.** Fees observed ~$0.004–0.01/position/day; each rotation costs ~4 txs (close, swap, prep swap, open) plus bin-array rent out/in. Every narrow-range rotation buys hours of in-range life and a high probability of repeating.

## 2. Guiding principles

1. **Width is a structural constraint, not an adaptive target.** On Meteora the answer to "how wide" is always "as wide as the tx allows"; adaptation only decides *downward*, and every downward step shortens life.
2. **Motion is net-negative at this capital size.** Rotations/recenters cost certain tx fees + rent to chase uncertain fee accrual. Default posture = do nothing.
3. **Out-of-range is a state, not a loss.** DLMM pays only for in-range liquidity, but sitting out costs pennies/day while acting costs more, immediately, with re-entry risk.

---

## 3. Proposed policy (levers, exact values)

### L1 — Range width, in PRICE terms
- Floor per side: **±3.5% (350bps)** (`range.min_half_fraction`: 0.01 → **0.035**).
- Meteora positions open at the **structural maximum: 70 bins** (no vol-adaptive narrowing). `width_factor` (0.5) loses meaning for the Meteora branch — it stays relevant only for Orca/Raydium branches.
- Eligibility coupling: a Meteora pool is width-eligible iff `35 × bin_step ≥ 350bps`, i.e. **bin_step ≥ 10bps**. This is the arithmetic backbone of recommendation #5.
- Sanity: SOL daily vol ~2–3% vs a ±3.5% band → expected life of days, not hours (observed proxy: the ±2.7% position lived 39h).

### L2 — Pool choice reacts to the bin_step/width coupling
- `pool_eligibility.bin_step_rule.min`: 4 → **10**. ⠂ Supersedes the 2026-10-19 owner ruling ("any bin_step ≥ 4 allowed"); flagged as an explicit owner decision (§6-D).
- Screener/scorer: `fee_tvl_ratio` ranking must be **width-aware** (or gated by width eligibility *before* rank). A raw fee/TVL rank that prefers a 4bps pool over an eligible 10bps pool is the structural trap that produced the observed close pattern. Lands in Missy's pool scoring and/or Sheldon's `candidate_filtering` pre-filter.
- `scoring.min_open_score` (90) unchanged — no compensation for a smaller eligible pool set by loosening the score floor.

### L3 — Min-hold vs close triggers
- `min_hold_hours`: **12, unchanged** (still the floor before any mutation).
- **Range exit alone is never a close trigger.** An out-of-range position is held: no close, no recenter, zero txs, fees paused.
- Automated close only on **structural drift**: price sustained beyond ~2× the band (≥70 bins from range center on a 10bps pool) for ≥6h continuously, or owner command, or existing rail violations (daily loss / drawdown / readiness fail-closed).
- Reopen after any close still obeys `reopen_cooldown_seconds` (2700).

### L4 — When to accept sitting out-of-range
- Default: **accept it.** OOR is an owned state. Cost of waiting ≈ foregone fees (~$0.004–0.01/day); cost of acting ≈ 4 txs + rent + re-entry risk. Patience wins at every horizon we have data for.
- Owner visibility: the daily digest must list OOR positions with drift and dwell time (bins from range, hours OOR) so a manual override is one command away. Kill switch remains the hard stop.

### L5 — Recenter & rotation at this capital
- **Rotation: off.** `max_rotations_per_day`: 2 → **0**. Rotation becomes operator-emitted only (`rotate_margin` becomes moot; keep for reference).
- **Recenter: dwell-based, not drift-based.** Replace the momentum-drift trigger (`drift_frac_of_half_width` 0.6 / `min_drift_bins` 8 + trend confirmation) with a dwell trigger: fire only when the position has been **continuously OOR for ≥24h** (and age ≥ `min_hold_hours`), re-centered at full 70 bins around the then-active bin. Keep `recenter_policy.enabled: true`, `cooldown_hours: 6`, `amount_haircut_pct: 2.0`; `max_recenters_per_pool_per_day`: 2 → **1**.
- Rationale: instant-drift recenter is churn with extra steps at this size. A drift that has persisted a full day is more likely structural; a transient one has usually already returned (and earned fees while we did nothing).

---

## 4. Where the changes land (when approved — not now)

| File | Change |
|---|---|
| `agents/Sheldon-agent/sheldon_policy.json` | `range.min_half_fraction` → 0.035; `pool_eligibility.bin_step_rule.min` → 10; `max_rotations_per_day` → 0; `recenter_policy` trigger semantics → dwell-based, per-pool/day cap 1 |
| `agents/Sheldon-agent/strategy/range/_7d_volatility_based.py` | Meteora branch of `adaptive_half_width` → structural max (70 bins); price-floor eligibility check (`35 × bin_step ≥ floor`); fail closed as today |
| `agents/Sheldon-agent/strategy/candidate_filtering.py` | Width-eligibility pre-filter before score rank; ban opens on pools that cannot reach the price floor |
| Missy pool scoring (separate repo) | Width-aware fee/TVL ranking or equivalent gate so 4bps pools cannot outrank eligible 10bps pools |
| George recenter trigger (Meteora agent) | Dwell-based trigger per L5; close trigger per L3 |
| George `SAFETY_RAILS.md` | No rail changes required — `max_meteora_range_width = 70` is the constraint this design respects, not a problem |

`execution_limits.json` needs no change: 70-bin cap, mutation cooldown 6h, exposure/loss rails all remain as enforced.

---

## 5. Expected effects

- **Lifetimes:** hours → days (width is the dominant observed variable: 6h at ±1.1% vs 39h at ±2.7%; ±3.5% should sit at or beyond the long end).
- **Churn:** rotations ~2/day → ~0; recenters rare (≤1/pool/day, only on persistent OOR).
- **Fees:** lower per-dollar density on wide ranges, but uninterrupted accrual on drift-free days replaces the close/swap/open tax. Net expected positive at this capital.
- **Opens:** eligible pool set shrinks (4bps pools excluded). Sitting in cash when nothing qualifies is the intended behavior, not a failure.
- **Watch item:** Curve/BidAsk strategy configs (`strategy_config.curve_half_width_bins` 20 → ±2% at 10bps) sit below the new floor. Recommend the floor applies to *total range width* regardless of strategy shape; Curve may still concentrate liquidity inside a wider range. Flagged in §6.

## 6. Open decisions for the owner

- **A. Width floor:** adopt ±3.5% (= the 70-bin cap at 10bps, per recommendation #5)? Alternatives: ±3% (admits 9bps pools), or a price cap on very high bin_step pools (e.g., refuse ±8.75% on 25bps+ pools). **My rec: ±3.5%.**
- **B. OOR posture:** pure patience (hold indefinitely, digest visibility) vs patience + dwell-recenter at 24h. **My rec: patience + 24h dwell-recenter.** (This is the fork that was put to the owner in-session with no reply; drafted here as the recommended option.)
- **C. Rotation:** fully off vs a capped 1/day. **My rec: off.**
- **D. bin_step_rule.min 4 → 10:** this supersedes the 2026-10-19 ruling. It is arithmetic-equivalent to the price floor (a 4bps pool can never reach ±3.5%), but is a formal policy change needing your signature. **My rec: adopt.**
- **E. Strategy shapes:** does the ±3.5% floor bind Curve/BidAsk total width too, or only the Spot default? **My rec: bind total width; liquidity shape may concentrate inside it.**

Nothing here is applied. Rule on A–E (or say "all as recommended") and the implementation becomes a patch list, not a design.
