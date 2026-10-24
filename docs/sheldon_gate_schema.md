# Sheldon Gate: Missy Results → Sheldon → George

## Table of Contents

- [1. Data Flow Pipeline](#1-data-flow-pipeline)
- [2. Validation & Freshness Gate](#2-validation--freshness-gate)
- [3. Dynamic Calibration Layer](#3-dynamic-calibration-layer)
- [4. Scoring Engine (lp_scoring.py)]{4. scoring-engine-lp_scoring-py}
- [5. Out-of-Range Grace (range_state.py)}
- [6. Open Candidate Filtering (run_cycle.py)}
- [7. Funding & Readiness (readiness.py)}
- [8. Signal Writing (write_signals → George-schema)}
- [9. Close Emission Guards (close_settlement.py)}
- [10. Rotation & Settlement (Phase 2)}
- [Schema Summary (Machine-Readable)}]
- [What's Good](#what-s-good)
- [What's Bad (Pain Points / Risks)}]
- [What Can Be Improved](#what-can-be-improved)
- [Key Constants & Thresholds Overview}



## Overview

The **Sheldon gate** is the deterministic pipeline that processes Missy's scan data and converts it into George-schema signal files for execution. This schema captures the end-to-end flow from raw Missy scan outputs through policy enforcement, scoring, verdict mapping, and signal generation.

---

## Data Flow Pipeline

### 1. Data Input Layer

**Inputs ( newest Missy scans ):**

| Source | Directory | File Pattern | Key Fields |
|---|---|---|---|
| Pool Screens | `/data/missy-data/pool_screens/` | `pool_scan-*.json` | `tvl`, `volume_window`, `realized_fee_apr`, `volatility`, `token_x_price_usd`, `token_y_price_usd`, `pair_class`, `eligible` |
| Position Scans | `/data/missy-data/position_scans/` | `position_scan-*.json` | `positions[]` with `position_id`, `pool_address`, `lower_price`, `upper_price`, `current_price`, `fees_usd`, `fees_owed_raw`, `days_open`, `in_range`, `value_usd` |
| Wallet Scans | `/data/missy-data/wallet_screens/` | `wallet_screen-*.json` | `idle_usdc`, `dust_assets`, `total_usd`, `deployable_usdc`, `reserved_total_usdc` |

**Freshness Checks (default max_age_seconds = 3900 = 65 min):**
- Pool/position scans must be fresh ≤ 3900s old
- Wallet scans checked separately in readiness (default 180s = 3 min for open signals)

---

### 2. Validation & Freshness Gate

```text
run_cycle() entry point:
  ├── Load newest pool scan → is_fresh() check → fail if stale
  ├── Load newest position scan → is_fresh() check → filter_live_positions() (drop status="closed")
  └─ Load wallet scan → load_wallet_scan() → summarize_wallet()
       └─ If missing/malformed → zeroed summary with error → OPEN signals skipped
```

**Fail-closed behavior:** If any required data is missing or stale, the cycle returns exit code 2 and no signals are written.

---

### 3. Dynamic Calibration Layer (optional, configured via `dynamic` in `profiles.json`)

If enabled, builds context from historical scan archive:

| Layer | Description |
|---|---|
| **Rolling Percentile Normalisation** | `build_norms()`: percentile reference windows for `fee_yield` and `turnover` from up to 60 prior scans |
| **Regime Detection** | `detect_regime()`: classifies market as `neutral`/`low_vol`/`high_vol`/`fee_boom`/`cracked_peg` based on median volatility/APR |
| **Regime Weight Adjustment** | Multiplicatively shifts base profile weights per regime, renormalized to 100 |
| **Adaptive Pool Thresholds** | Derives OPEN/WATCH from quantiles (90th/70th) of scored universe; floored at policy min, ceiled at 90/80 |
| **Expected-PnL Position Verdicts** | Compares HOLD vs CLOSE vs REBALANCE over 1-day horizon; overrides static verdict when margin ≥ 0.01 USD |

**If dynamic is disabled:** All percentile blending, regime shifts, and adaptive thresholds are disabled; static `profiles.json` behavior prevails.

---

### 4. Scoring Engine (lp_scoring.py)

#### Pool Scoring (`score_pool()`)

**Components (weighted sum, clamped 0-100):**

| Component | Weight (stable_stable) | Description |
|---|---|---|
| fee_yield | 35.0 | Realized Fee APR / apr_cap_pct (sqrt curve) |
| turnover | 15.0 | Window volume / TVL / turnover_max |
| depth | 15.0 | Log scale TVL gate: $50k-$5M |
| volatility_fit | 10.0 | Triangular curve, peak at class vol_peak_pct |
| depeg_safety | 25.0 | Worst stable side distance from $1 (max 30% reduction) |

**Blending:** Absolute scores blended with percentile ranks (70% percentile, 30% absolute) via `_maybe_blend()`.

**Verdict Mapping (`pool_verdict()`):**
- Score ≥ open threshold → `OPEN_CANDIDATE`
- Score ≥ watch threshold → `WATCH`
- Else → `IGNORE`

**Missy Gate:** If `eligible` is False → verdict capped at `IGNORE` regardless of score.

#### Position Scoring (`score_position()` / `score_position_missy()`)

**Components (weighted sum, clamped 0-100):**

| Component | Description |
|---|---|
| range_status | In-range centering reward: 1.0 at center, 0.0 at boundary |
| fee_capture | APR-normalized efficiency: daily fee yield / pool daily yield |
| staleness | Range efficiency: `in_range_days / total_days` |
| depeg_exposure | Peg fraction penalty: max 30% score reduction |
| il_risk | Net yield coverage: (fees - IL) / value |

**Open/Close Separation Logic (critical):**

```text
After computing total score and collecting unknown_parts:
  1. If PNL decisive and suggests CLOSE/REBALANCE but unknown_parts → REVIEW
  2. elif total < close(40) and unknown_parts → REVIEW
  3. elif total < close(40) → CLOSE
  4. elif total < review(60) → REVIEW
  5. else → HOLD
- Append unknown parts to reason if verdict != REVIEW and unknown_parts exist
- Policy CLOSE (off-universe pair) is exempt and always CLOSE
```

**Data Quality:** `data_quality.unknown_components` list tracks which components ran on missing data.

---

### 5. Out-of-Range Grace (range_state.py)

**Policy:** A position that leaves its range must survive **TWO** consecutive automation runs before Sheldon emits a close.

**Mechanism:**
- State persisted in `state/out_of_range.json`
- Run 1: CLOSE/REBALANCE downgraded to HOLD with reason "out-of-range grace: 1/2 consecutive out-of-range runs"
- Run 2: CLOSE/REBALANCE passes through
- Returning in range resets the counter

---

### 6. Open Candidate Filtering (run_cycle.py `_filter_open_candidates()`)

**Filter Sequence:**

| Step | Check |
|---|---|
| a. Dedup | One position per pool (enforced earlier by `pools_by_addr`) |
| b. Center != 0 | Skip if active_bin/tick center is 0 |
| c. Trading window | `_in_trading_window()`: Asia/Shanghai (UTC+8); blackout dates |
| d. Policy gates (from `sheldon_policy.json`): | - `min_pool_liquidity_usd: 250k` <br> - `min_24h_volume_usd: 1M` <br> - `allowed_bin_steps: [4,10,20,25,50,100]` (Meteora only, fail-closed) <br> - `min_fee_tvl_ratio: 0.05` <br> - `max_volatility_pct: 50` <br> - `max_turnover_ratio: 50` |
| e. Meteora bin-step check | `meteora_bin_step_allowed()` — fail-closed if bin step not in allowed list |
| f. Top N by score | Sort descending, take top `max_opens_per_cycle=3` |
| g. Reserved pools | Pools marked as rotation targets blocked |

**Sizing (in `build_strategies()`):**
- `position_usd = min(deployable_usdc * 0.75, DEFAULT_MAX_POSITION_USD=100)`
- `>= MIN_POSITION_USD (20) required`
- 50/50 USD split into `amount_x`/`amount_y` via prices/decimals
- `max_slippage_bps=100` on the intent

---

### 7. Funding & Readiness (readiness.py)

**`plan_funding()`:** For each strategy, diff 50/50 token needs against raw wallet balances.

**`gate_prep_swaps()`:** Blocks prep swaps when:
- Wallet scan older than `MAX_WALLET_SCAN_AGE_SECONDS=180`
- Younger than last prep swap + `PREP_CONFIRM_GRACE_SECONDS=60`
- Above `MAX_PREP_SWAPS_PER_MINT_PER_HOUR=3`

**`build_readiness()` (Phase 1):** Every strategy gets one additive readiness object. All checks mandatory in one cycle; stale/missing scan blocks open (fail-closed).

**Mandatory readiness checks:**
- `require_fresh_wallet_scan`: wallet scan freshness
- `require_fresh_position_scan`: position scan freshness
- `require_both_tokens`: both token sides funded
- `require_sol_readiness`: SOL reserve adequate
- `require_close_settled`: no pending close for same pool
- `max_wallet_scan_age_seconds`: 900s (15 min)
- `max_position_scan_age_seconds`: 900s (15 min)

**Readiness outcome:**
- `ready=True` → strategy proceeds to signal writing
- `ready=False` → strategy blocked; reason logged in `open_skipped`

---

### 8. Signal Writing (`write_signals()` → George-schema)

**Output Order:**

| Signal Type | Action | When Emitted |
|---|---|---|
| **OPEN** | `open` | For each ready strategy (top 3 by score) |
| **DUST** | `swap_to_usdc` | Non-USDC dust assets |
| **PREP SWAP** | `swap` | Funded shortfall strategies |
| **CLOSE/REBALANCE** | `close` | Verdicts of CLOSE/REBALANCE (after grace) |
| **COLLECT_FEES** | `claim_fees` | Verdicts with `collect_fees=True` |
| **IDLE SWEEP** | `add_liquidity` | Leftover idle capital into best tracked position |

**George-schema Signal Format:**

```json
{
  "signal_id": "sheldon-{timestamp}",
  "action": "open"|"close"|"claim_fees"|"swap"|"add_liquidity",
  "dex": "meteora"|"raydium"|"orca",
  "pool_address": "...",
  "position_id": "...",
  "position_usd": 90.0,
  "bin_range": {"lower": ..., "upper": ...} | "tick_range": {...},
  "side": "quote_only" | "base_only" | ...,
  "max_slippage_bps": 100,
  "reason": "human readable",
  "created_at": "2026-10-23T04:58:12+08:00",
  "decision": "pending" | "executed" | ...
}
```

**Wake Hint:** `_wake_george()` emits: `WAKE_GEORGE: N signal(s) -> filenames` to stderr.

---

### 9. Close Emission Guards (close_settlement.py)

**Two-phase close emission:**

| Phase | Check |
|---|---|
| **Staleness** | Scan older than `max_scan_age_seconds=900` → suppress close |
| **Pool Settlement** | If pool has close-settlement event newer than position scan → suppress |
| **In-Flight** | If close for same position_id already emitted → block (idempotency) |
| **Reopen Cooldown** | `reopen_cooldown_seconds=2700` (45 min) blocks same-position re-opens |

**COLLECT_FEES** never suppressed (claims must always flow).

---

### 10. Rotation & Settlement (Phase 2)

**Two-cycle workflow:**
- **Cycle N:** ROTATE verdict → close signal for source position; candidate reserved; re-enters open flow only after close settles
- **Cycle N+1:** Close settlement gate blocks opens until George's journal confirms the close; reservation settles; candidate re-enters normal open flow

**Rotation Detection:** For each position with verdict in {REVIEW, CLOSE, REBALANCE}, check if rotating to a better candidate pool improves expected PnL (margin ≥ rotate_margin=5.0 USD).

---

## Full End-to-End Flow Diagram

```text
Missy Scans (pool + position + wallet)
          │
          ▼
Freshness Validation (max-age checks)
          │
          ▼
Dynamic Calibration (optional: percentiles, regime, adaptive thresholds)
          │
          ▼
Scoring Engine:
  ├─ Pool Scoring (5+ components, percentile blending)
  └─ Position Scoring (5 components, open/close separation)
          │
          ▼
Verdict Mapping (pool: IGNORE/WATCH/OPEN_CANDIDATE; position: HOLD/REVIEW/CLOSE/REBALANCE/COLLECT_FEES)
          │
          ▼
Out-of-Range Grace (2-run safety)
          │
          ▼
Open Candidate Filtering (dedup, windows, policy gates, top-3)
          │
          ▼
Funding & Readiness (plan_funding + build_readiness, Phase 1 gates)
          │
          ▼
Signal Writing (George-schema: open → dust → prep → close/claim → sweep)
          │
          ▼
Wake Hint: WAKE_GEORGE: N signal(s) -> filenames
```

---

## Schema Summary (Machine-Readable)

```json
{
  "data_sources": {
    "pool_scan": "path/to/pool_scan-*.json [tvl, volume_window, realized_fee_apr, volatility, pair_class, eligible]",
    "position_scan": "path/to/position_scan-*.json [positions[] with full position data]",
    "wallet_scan": "path/to/wallet_screen-*.json [idle_usdc, dust_assets, deployable_usdc]"
  },
  "freshness": {
    "pool_position_max_age_seconds": 3900,
    "wallet_max_age_seconds": 180 (for opens) / 900 (for readiness)
  },
  "scoring": {
    "pool_components": ["fee_yield", "turnover", "depth", "volatility_fit", "depeg_safety"],
    "position_components": ["range_status", "fee_capture", "staleness", "depeg_exposure", "il_risk"],
    "open_threshold": 70 (policy) / adaptive from quantiles,
    "watch_threshold": 55 (policy) / adaptive from quantiles,
    "position_close": 40,
    "position_review": 60
  },
  "gates": {
    "policy": "sheldon_policy.json [min_open_score, min_pool_liquidity_usd, min_24h_volume_usd, allowed_bin_steps, ...]",
    "readiness": "Phase 1 mandatory checks [fresh wallet, fresh position, both tokens, SOL, close settled]",
    "out_of_range_grace": "2 consecutive runs before CLOSE emits",
    "close_emission": "Staleness + pool settlement + in-flight + reopen cooldown guards"
  },
  "signals": {
    "output_format": "George-schema: signal_id, action, dex, pool_address, position_id, position_usd, bin_range/tick_range, side, max_slippage_bps, reason, created_at",
    "order": ["open", "swap_to_usdc", "prep swaps", "close/claim", "add_liquidity"],
    "wake_hint": "WAKE_GEORGE: N signal(s) -> filenames"
  },
  "rotation": {
    "two_cycle_workflow": "Cycle N: ROTATE close + reserve target; Cycle N+1: settle close, re-enter open flow",
    "rotate_margin": 5.0 USD
  }
}
```

---

## What's Good

### Strengths

1. **Deterministic & Reproducible** — All scoring is deterministic given the same inputs; no randomness or RPC dependencies.

2. **Robust Data Quality Tracking** — `unknown_components` list explicitly tracks which inputs were missing, and verdicts are capped at REVIEW (never CLOSE) when data is absent. This fail-closed behavior prevents bad signals from leaking.

3. **Two-Safety-Out-of-Range Grace** — The 2-run requirement before CLOSE emits prevents premature closures from transient price moves; positions get a chance to return to range.

4. **Dynamic Calibration** — Rolling percentiles and regime detection adapt thresholds to current market conditions rather than static cut-offs alone.

5. **Comprehensive Policy Gates** — Policy-driven checks (bin-step, liquidity, volume, TVL ratio) ensure only tradeable pools are selected; fail-closed on malformed data (bin_step validation).

6. **Open/Close Separation** — Explicit separation: CLOSE requires known data for every component; missing data caps at REVIEW. This prevents CLOSE signals on positions with unverifiable data.

7. **Expected-PnL Override** — When PNL is decisive (margin ≥ 0.01 USD), it can override score-based verdicts, adding PnL-awareness to the purely scoring-based system.

8. **Comprehensive Guardrail Suite** — Close emission guards (staleness, settlement, in-flight, cooldown) prevent the ghost-signal problem that occurred 2026-10-18 (10 of 12 signals were ghosts).

9. **Rotation Two-Cycle Workflow** — Clear N+1 settlement pattern prevents capital double-counting; reservations track pending rotates until close settles.

10. **SOL Reserve Model** — Phase 1 SOL reserve (60M lamports) ensures rent/gas never starves opens; auto-top-up from USDC buffer provides safety net.

---

## What's Bad (Pain Points / Risks)

### Weaknesses

1. **Cold-Start Dynamic Calibration** — With too few historical scans (below min_samples=20 for thresholds), adaptive thresholds fall back to static behavior. New deployments will always use static thresholds until sufficient scan history accumulates.

2. **Adaptive Thresholds Need ≥20 Pools** — If the universe is small (few stable/stable-cap pairs), adaptive thresholds cannot be computed and static thresholds are used, which may not be optimal for the actual universe size.

3. **Position Scoring Requires Complete Data** — Any missing component (days_open, fees, price bounds, volatility) reduces verdict capability:
   - CLOSE requires all components known
   - Otherwise REVIEW
   - This can block CLOSE on positions with genuinely complete but unconventional data.

4. **Out-of-Range Grace Adds Latency** — A position truly out-of-range takes 2 full cycles (potentially ~130 min with 65 min scan freshness + cycle time) before CLOSE can emit. This delays capital reallocation.

5. **Signal Writing is Sequential & Cascading** — Failures in early phases cascade:
   - If open signal write fails → idle sweep may have insufficient idle capital
   - If prep swap gating blocks → funding notes accumulate but no action
   - The sequential nature means one failure can block the entire cycle's output.

6. **Wallet Scan Freshness is Binary** — Wallet scans older than 180s (3 min) block open signals, but there's no graduated penalty; a scan at 179s is treated the same as one at 10s. This can be overly strict.

7. **No Real-Time Price Data** — All prices come from Missy's pre-computed scans; no oracle or RPC fallback. If scans are stale, prices are stale, which affects IL estimates, depeg safety, and PnL calculations.

8. **Bin-Step Filter is Meteora-Only** — `allowed_bin_steps: [4,10,20,25,50,100]` is Meteora-specific; Raydium/Orca tick spacing is not validated in the same way, creating an uneven playing field.

9. **Rotation Detection Depends on Score Delta** — Rotation requires `cand_score >= pos_score + rotate_margin (5.0)`. If the score distribution is compressed, rotation opportunities may be missed.

10. **Dynamic Regime Detection Needs History** — `history_scans: 30` for regime; cold starts will always classify as `neutral` regime, missing regime-specific weight adjustments.

---

## What Can Be Improved

### Recommended Improvements

1. **Graduated Wallet Freshness Penalties** — Instead of binary 180s/900s check, implement a sliding scale where older wallets get progressively softer blocks (e.g., reduce max position size proportionally to age).

2. **Expand Bin-Step Validation** — Add Raydium/Orca tick-spacing validation to the policy gates, or make the bin-step check DEX-aware rather than Meteora-only fail-closed.

3. **Out-of-Range Grace: Side-Aware Counters** — The current grace counter is side-agnostic; make it side-aware so switching sides resets the counter (as noted in range_state.py comments).

4. **Dynamic Thresholds Floor/Ceil Calibration** — The adaptive thresholds floor at min_open_score=70 (or 90 from policy) and ceil at 90/80. These values could be made configurable per-deploy rather than hardcoded.

5. **PnL Horizon Configurability** — The expected-PnL horizon is fixed at 1 day. Make this configurable per profile or per-position for different strategies.

6. **Component Weight Calibration via Tuner** — The `tuner.py` already exists for weight perturbations; consider making weight calibration automatic based on rolling performance metrics rather than manual profile updates.

7. **Additional Data Quality Safeguards** — Add explicit warnings when `unknown_components` count exceeds a threshold, rather than silently capping at REVIEW. This gives operators visibility into data gaps.

8. **Rotation Candidate Pre-screening** — Before marking a candidate as `RESERVED_ROTATION_TARGET`, verify the candidate also passes all policy gates (liquidity, volume, bin-step) so the reservation doesn't block valid opens.

9. **Idle Sweep Headroom Computation** — The idle sweep's headroom calculation (`max_position - tracked_value`) could be made more precise by tracking per-position range utilization more granularly.

10. **Close Settlement State Persistence** — The close settlement state (`state/close_state.json`) could include a TTL/expiry for `CLOSE_EMITTED` entries so stale close states don't permanently block opens.

11. **Profile Versioning & Migration** — Add version tracking to `profiles.json` so weight changes can be rolled back or A/B tested across cycles.

12. **Missy Score Trust Boundary** — The `score_policy` field tracks Missy vs local source, but there's no audit trail of when Missy's classification changed. Add versioned scoring provenance for compliance.

13. **Per-Pool Cooldown After Rotation** — After a rotation, add a per-pool cooldown (similar to reopen cooldown) to prevent rapid rotate-close-resequence cycles that could wedge capital.

14. **Signal Review Dashboard** — The `signals_review` in the JSON report is already a good start; consider adding visual dashboards (markdown daily logs) that aggregate review reasons over time to identify systemic data quality or policy issues.

15. **Multi-DEX Range Normalization** — The adaptive range algorithm currently treats Meteora bin-step and Raydium/Orca tick-spacing differently. Consider normalizing range widths across DEXes for fair comparison in rotation decisions.

---

## Key Constants & Thresholds Overview

| Parameter | Value | Source | Notes |
|---|---|---|---|
| `min_open_score` | 70 (policy) / 90 (scoring) | `sheldon_policy.json` / `profiles.json` | Policy takes precedence in some flows |
| `min_pool_liquidity_usd` | 250k | `sheldon_policy.json` | Enforced in open candidate filtering |
| `min_24h_volume_usd` | 1M | `sheldon_policy.json` | Enforced in open candidate filtering |
| `allowed_bin_steps` | [4,10,20,25,50,100] | `sheldon_policy.json` | Meteora only, fail-closed |
| `min_fee_tvl_ratio` | 0.05 | `sheldon_policy.json` | |
| `max_volatility_pct` | 50 | `sheldon_policy.json` | |
| `max_turnover_ratio` | 50 | `sheldon_policy.json` | |
| `position_close` | 40 | `profiles.json` | Position verdict threshold |
| `position_review` | 60 | `profiles.json` | Position verdict threshold |
| `max_opens_per_cycle` | 3 | `sheldon_policy.json` | Max open positions per cycle |
| `reopen_cooldown_seconds` | 2700 | `sheldon_policy.json` | 45 min between same-position opens |
| `max_wallet_scan_age_seconds` | 180 (opens) / 900 (readiness) | `sheldon_policy.json` | |
| `out_of_range_allowed_runs` | 2 | `range_state.py` | 2 consecutive runs before CLOSE |
| `rotate_margin` | 5.0 USD | `sheldon_policy.json` | Rotation improvement threshold |
| `close_emission.max_scan_age_seconds` | 900 | `sheldon_policy.json` | Close staleness guard |
| `close_emission.require_scan_newer_than_last_close` | true | `sheldon_policy.json` | |
| `SOL reserve_lamports` | 60M | `sheldon_policy.json` | Never spent on opens |
| `default_max_position_usd` | 100 | `sheldon_policy.json` | Capped position size |
| `min_position_usd` | 20 | `sheldon_policy.json` | Minimum position size |
| `add_policy.idle_max_usd` | 20 | `sheldon_policy.json` | Max idle for sweep |
| `add_policy.min_add_usd` | 5 | `sheldon_policy.json` | Min idle for sweep |
| `add_policy.y_side_room_pct` | 25% | `sheldon_policy.json` | Y-side headroom for sweep |
| `SOL_RESERVE_LAMPORTS` | 20M | `capital.py` | Never spent (different from policy 60M) |
| `BUY_BUFFER_PCT` | 2.0% | `readiness.py` | SOL buffer for purchases |
| `PREP_CONFIRM_GRACE_SECONDS` | 60 | `readiness.py` | Prep swap grace period |
| `MAX_PREP_SWAPS_PER_MINT_PER_HOUR` | 3 | `readiness.py` | Hourly cap |
| `MAX_WALLET_SCAN_AGE_SECONDS` | 180 | `readiness.py` | Wallet freshness for prep swaps |
| `SOL_RESERVE_LAMPORTS` (readiness) | 20M | `readiness.py` | SOL reserve for open readiness |

---
```