# Sheldon LP Scoring Engine — Comprehensive Analysis

## 1. High-Level Blueprint & Core Mechanics

### Primary Objective
Sheldon is a **deterministic LP scoring engine** that evaluates DeFi liquidity pool positions on three decentralized exchanges:
- **Meteora DLMM** (DLMM bins via `active_bin_id`)
- **Raydium CLMM** (CLMM ticks via `current_tick` or `active_bin_id`)
- **Orca Whirlpool** (Whirlpool ticks via `current_tick` or `active_bin_id`)

The system scores both **pools** (opportunity: "should we open a position here?") and **positions** (health: "should we keep this position open?"), then converts verdicts into George-schema signal files for execution.

### Architectural Pattern
**Modular pipeline architecture** with clearly separated concerns:

```
┌─────────────────────────────────────────────────────────────────┐
│                     Data Input Layer                            │
│  pool_screens/ + position_scans/ + wallet_screens/              │
└─────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Validation & Freshness                       │
│  max-age checks (default 3900s), JSON parsing, malformed handling │
└─────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│                 Dynamic Calibration Layer                       │
│  │ rolling percentiles          │ regime detection              │
│  │ adaptive thresholds          │ expected-PnL verdicts         │
└─────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Scoring Engine (lp_scoring.py)             │
│  • Pool LP Opportunity Score (0-100)                           │
│  • Position Health Score (0-100)                               │
│  • data_quality tracking (unknown_components)                  │
│  • Per-verdict human-readable reason strings                   │
└─────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Verdict Mapping & Gates                      │
│  • Pool: ≥70→OPEN_CANDIDATE, ≥55→WATCH, else→IGNORE             │
│  • Position: <40→CLOSE, 40-60→REVIEW, ≥60→HOLD                │
│  • Open/close separation: missing data caps at REVIEW           │
│  • Out-of-range grace: 2 consecutive runs before CLOSE passes  │
│  • Policy gates: TVL, volume, bin-step, wallet eligibility      │
└─────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│                 Signal Generation & Output                      │
│  • _filter_open_candidates → top 3 by score → build_strategies   │
│  • plan_funding → gate_prep_swaps → write_signals                │
│  • idle_sweep → add_liquidity into best tracked position        │
│  • Output: JSON report, human summary, signal files, markdown    │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow (End-to-End)
1. **Input**: Newest `pool_scan-*.json`, `position_scan-*.json`, `wallet_screen-*.json` from configured directories
2. **Validation**: Freshness check (max-age, default 3900s), malformed data handling, config validation at load time
3. **Dynamic calibration**: `dynamic.build_context()` builds context from prior scans (rolling percentiles, regime classification, adaptive thresholds, expected-PnL config)
4. **Scoring**: Each pool gets a 0-100 score across 4-5 weighted components; each position gets a 0-100 score plus a `data_quality` record listing components scored on missing/unusable inputs
5. **Verdicts**: Scores map to verdicts. Open/close separation: a position verdict of CLOSE requires all components scored on known data — any unknown component caps the verdict at REVIEW. Policy-based CLOSE (off-universe/unknown pair) is exempt and always CLOSE. Expected-PnL can override the score-based verdict when all required inputs exist.
6. **Out-of-range grace**: `range_state.py` counts consecutive out-of-range runs per pool (`state/out_of_range.json`). Run 1 downgrades CLOSE/REBALANCE to HOLD; run 2 lets it through; back-in-range resets the counter.
7. **Open filtering + strategies**: `_filter_open_candidates` drops dedup/duplicate/center-unknown/out-of-window/policy-gated/bin-step-violating candidates; `strategy.build_strategies` ranks survivors by score (top `max_opens_per_cycle=3`) with volatility-adaptive ranges and 75%-of-deployable sizing.
8. **Funding + readiness**: `readiness.plan_funding` diffs strategy token needs against the raw wallet scan; `gate_prep_swaps` blocks rescan-races and hourly loops. Unfunded strategies yield prep `swap` signals, not opens.
9. **Signal building**: `write_signals` emits opens first, then dust `swap_to_usdc`, prep swaps, and closes/claims; `idle_sweep.plan_sweep` sweeps leftover sub-floor idle into the best tracked position.
10. **Output**: JSON report, human-readable summary, signal files, daily markdown log, and a `dynamic` block showing the active regime and thresholds.

---

## 2. Module & File Deep-Dive

### `lp_scoring.py` — Core Scoring Engine (v2)
**Purpose**: Deterministic scoring for LP positions on Meteora DLMM, Raydium CLMM, and Orca Whirlpool. Configuration externalized to `profiles.json` (validated; fail-closed).

**Key Components**:

| Section | Purpose |
|---|---|
| **Configuration** (`DEFAULT_CONFIG`, `load_config()`, `_validate_config()`) | Default weights, thresholds, constants. `profiles.json` merges over defaults via `_deep_merge()`. Invalid file → `ConfigError` exit 1. Weights must sum to 100 per profile. |
| **Universe Policy** (`STABLECOINS`, `HIGH_CAPS`, `classify_pair()`) | Hard-gated: only stablecoin/stable+high-cap pairs score above IGNORE. `classify_pair()` returns `(pair_class, sym_x, sym_y)`: `stable_stable`, `stable_bluechip`, `bluechip_bluechip`, `off_universe`, `unknown`. Cached via `@lru_cache`. |
| **Pool Scoring** (`score_pool()`, 59 components) | Computes pool LP opportunity score (0-100). Embeds original `_pool` dict; OPEN_CANDIDATE verdicts carry it for signal building. |
| **Position Scoring** (`score_position()`, `score_position_missy()`) | Computes position health score (0-100). Returns `data_quality.unknown_components` list. Open/close separation: CLOSE requires known data; missing data caps at REVIEW. |
| **Scoring Components** (detailed in Section 4) | fee_yield, turnover, depth, volatility_fit, depeg_safety (pools); range_status, fee_capture, depeg_exposure, il_risk, staleness (positions) |
| **Main Cycle** (`run_cycle()`) | Orchestrates the full cycle: load data → dynamic calibration → pool scoring → position scoring → verdict mapping → open filtering → funding → signal writing → output. |

**Key Functions**:
- `score_pool_fee_yield()`: `max_pts * clamp(apr / apr_cap, 0, 1)`
- `score_pool_turnover()`: `max_pts * clamp((volume_window / tvl) / turnover_max, 0, 1)`
- `score_pool_depth()`: `max_pts * log(tvl / depth_min) / log(depth_max / depth_min)`
- `score_pool_volatility_fit()`: Triangular curve: `vol/peak` if vol≤peak, else `(3*peak - vol)/(2*peak)`
- `score_pool_depeg_safety()`: `max_pts * min(sides)` where each side = `clamp(1 - |price - 1| / zero_dist, 0, 1)`
- `score_position_range_status()`: In-range with centering reward: `max_pts * (1.0 - edge_frac)` where edge_frac=0 at center, 1 at boundary
- `score_position_fee_capture()`: APR-normalized efficiency: `blended = 0.7 * clamp(efficiency, 0, 2) + 0.3 * (fees/expected)`; efficiency = daily fee yield / (value * APR/365)
- `score_position_il_risk()`: Net yield coverage: `max_pts * clamp(1 + (fees - IL) / value, 0, 2)`; positive net yield scores above baseline
- `score_position_depeg_exposure()`: Penalty multiplier: final score × (1 - peg_frac × 0.3), max 30% reduction
- `score_position_staleness()`: Range efficiency: `max_pts * (in_range_days / total_days)`; rewards positions that have consistently stayed in-range

**Blending with Percentiles** (`_maybe_blend()`):
```
score = max_pts * (alpha * rank + (1-alpha) * norm_abs)
```
where `rank` = percentileRank(norms[name], raw_value), `norm_abs` = abs_pts / max_pts, `alpha` = 0.7 default.

**Open/Close Separation Logic** (critical):
- After computing `total` score and collecting `unknown_parts`:
  - If PNL decisive and suggests CLOSE/REBALANCE but unknown_parts exist → **REVIEW**
  - elif total < close_threshold and unknown_parts → **REVIEW**
  - elif total < close_threshold → **CLOSE**
  - elif total < review_threshold → **REVIEW**
  - else → **HOLD**
- If unknown_parts and verdict != REVIEW → append to reason string
- **Policy CLOSE** (off-universe pair) is exempt and always CLOSE, regardless of data quality

### `dynamic.py` — Dynamic Calibration Layer
**Purpose**: Derives calibration from the Missy scan archive instead of static constants alone.

**Six Dynamic Layers**:

1. **Rolling Percentile Normalisation** (`build_norms()`, `percentile_rank()`):
   - `fee_yield` and `turnover` scored as percentile of same pair class over trailing window
   - Blended with absolute score via `blend_alpha` (0.7 default)
   - `build_norms()` returns `{pair_class: {component: sorted values}}`; classes with < min_samples fall back to absolute scoring

2. **Regime Detection** (`detect_regime()`):
   - Classifies market as `neutral`/`low_vol`/`high_vol`/`fee_boom`/`cracked_peg`
   - Based on median volatility and APR of prior scans vs current
   - `fee_boom`: peak APR > fee_boom_apr_pct (100%)
   - `cracked_peg`: stable deviation > depeg_zero_dist (0.005)
   - `high_vol`: vol_ratio > high_vol_mult (2.0)
   - `low_vol`: vol_ratio < low_vol_mult (0.5)
   - `neutral`: otherwise

3. **Regime Weight Adjustment** (`regime_weight_adjust()`):
   - Multiplicatively shifts base weights per regime, renormalized to 100
   - Example: `high_vol` shifts: fee_yield×0.8, turnover×1.05, depth×1.0, volatility_fit×1.4, depeg_safety×1.3

4. **Adaptive Pool Thresholds** (`adaptive_pool_thresholds()`):
   - OPEN/WATCH cut-offs from quantiles of scored universe
   - 90th pctile for open, 70th for watch (configurable)
   - Floored by policy min_open_score (70), ceiled at 90/80
   - Requires ≥ min_pools (20) investable pools

5. **Expected-PnL Position Verdicts** (`expected_pnl_verdict()`):
   - Compares HOLD vs CLOSE vs REBALANCE over a short horizon
   - Returns dict with per-action expected USD and chosen action
   - Overrides static verdict when margin >= min_pnl_margin_usd (0.01 default)

6. **Cross-model scoring** (`scoring/pool/score.py`, `scoring/position/score.py`, `run_cycle/gates.py`):
   - `new_fc_score` (fee-capture model) provides complementary lens alongside missy-default
   - Dual-model gate in `_filter_open_candidates` requires cross-model agreement or weighted combination
   - `cross_model` field in position/pool results notes agreement (`both open (agreement)`) or disagreement (`missy=open new_fc=ignore`)

- Returns dict with `regime`, `norms`, `blend_alpha`, `weights` (regime-shifted), `position_pnl`, `thresholds` (filled after first scoring pass)

### `range_state.py` — Out-of-Range Grace
**Policy**: A position that leaves its range must survive **TWO** consecutive automation runs before Sheldon emits a close. First run downgrades CLOSE/REBALANCE to HOLD with review reason (no close signal); second consecutive run lets it through. Returning in range resets the counter.

**Mechanism**:
- State kept in `state/out_of_range.json`
- `position_out_of_range()`: True when raw position record verifiably out of range (explicit price bounds preferred; falls back to Missy's `in_range` flag)
- `update_out_of_range_counts()`: Increments for out-of-range positions, removes for in-range; drops counters for vanished positions
- `out_of_range_position_verdict()`: Applied to each verdict — if 1 ≤ seen < allowed (2), downgrades CLOSE/REBALANCE to HOLD with reason

### `strategy/` — Position Sizing and Adaptive Ranges
**Modules**:
- `sheldon_policy.py`: Policy loading (`MIN_POSITION_USD=20`, `DEFAULT_MAX_POSITION_USD=100`, `max_opens_per_cycle=3`, windows, add_policy, etc.)
- `position_sizing.py`: `suggested_position_usd(deployable)` = `min(deployable * 0.75, DEFAULT_MAX_POSITION_USD)`; `open_eligible(wallet)` = idle >= MIN and suggested >= MIN
- `candidate_filtering.py`: `build_strategies(open_candidates, wallet)` — ranks by score, selects top N, builds volatility-adaptive ranges
- `range/_7d_volatility_based.py`: `_range_center()`, `adaptive_half_width()`, `meteora_bin_step_allowed()`

**Adaptive Range Algorithm**:
```
target_half_fraction = (volatility_pct / 100) * WIDTH_FACTOR (0.5)
Meteora: step_ratio = 1 + bin_step/10000
Raydium/Orca: step_ratio = 1.0001 ^ tick_spacing
half_width = clamp(ceil(log(1+f) / log(step_ratio)), MIN_HALF_WIDTH=10, per-DEX max)
Meteora max: George max_meteora_range_width=70 bins → half-width 34
Raydium/Orca max: MAX_HALF_WIDTH=1000
volatility <= 0 or unknown spacing → MIN_HALF_WIDTH
bin_range = [center - half_width, center + half_width]
```
where `center` is `active_bin_id` for Meteora, tick for Raydium/Orca.

### `capital.py` — Wallet Scan Loader
- `summarize_wallet(data)`: Returns `{wallet, total_usd, idle_usdc, dust_total_usdc, dust_assets, reserved_total_usdc, deployable_usdc}`
- `USDC_MINT = "EPjFWdd5AfqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"`
- `SOL_MINT = "So11111111111111111111111111111111111111112"`
- `DUST_MIN_USD = 1.0`
- `RESERVED_MINTS = {SOL_MINT}` (SOL + owner hold never spent)
- `deployable = idle_usdc + dust_total`

**No env-var override and no RPC lookup.** Unknown/missing scan means idle 0 and OPEN signals skipped.

### `readiness/` — Capital Readiness
- `wallet.py`: `load_raw_wallet()` — raw wallet scan loading
- `funding.py`: `plan_funding()` — diffs strategy token needs against raw wallet scan
- `prep_swap_gates.py`: `gate_prep_swaps()` — blocks rescan-races (PREP_CONFIRM_GRACE_SECONDS=60) and hourly loops (MAX_PREP_SWAPS_PER_MINT_PER_HOUR=3)
- `prep_swap.py`: Build George-schema swap signals
- `prep_ledger.py`: `load_and_resolve()`, `recent_buy_mints()` — oscillation window (PREP_OSCILLATION_WINDOW_SECONDS)

**Key constants**:
- `SOL_RESERVE_LAMPORTS=20M` (never spent)
- `BUY_BUFFER_PCT=2.0`
- `MIN_PREP_SWAP_USD=1.0`
- `SELL_SURPLUS_MIN_USD=2.0`
- `MAX_PREP_SWAPS_PER_MINT_PER_HOUR=3`
- `PREP_CONFIRM_GRACE_SECONDS=60`
- `MAX_WALLET_SCAN_AGE_SECONDS=180`

### `idle_sweep.py` — Idle-Capital Sweep
**When**: `add_policy.enabled` and net idle (after committed opens/preps/reservations) satisfies `min_add_usd <= idle < idle_max_usd` with fresh scan.

**Flow**:
1. Release confirmed reservations from prior cycles
2. Staleness guard: wallet scan must be < `add_policy.max_wallet_scan_age_seconds` (900s = 15min)
3. Cooldown and day cap: last add + 6h cooldown, max 6 adds/day
4. Idle math: `idle_net = idle_raw - committed - reserved`
5. If `idle_net >= idle_max (20)` → normal open path owns capital; if `idle_net < min_add (5)` → not worth tx
6. Target selection among tracked HOLD/REBALANCE positions via `select_add_candidate()`
7. Eligibility checks: Y side must be USDC, headroom `max_position - tracked_value >= add`, pool passes bin_step/TVL/volume gates, y_side_room >= 25%, per-pool cooldown + day cap
8. Emit `add_liquidity` signal with `position_usd` amount

**Reservation**: Pending add persisted atomically (`RESERVATION_MAX_AGE_SECONDS=3600`). Released when George's queues show signal done or reservation stale.

### `sheldon_policy.json` — Strategy Policy (single source)
**Key Sections**:
- `scoring`: `source: "missy"`, `min_open_score: 90.0`, `position_source: "missy"`, `position_features_version: 1`
  - *Note*: The policy sets min_open_score to 90, but `profiles.json` pool_thresholds open=70. The policy value takes precedence in some flows; the scorer uses profiles.json thresholds unless adaptive dynamic thresholds override.
- `universe`: stablecoins + high_caps list (wider than scoring's STABLECOINS/HIGH_CAPS; scoring lists win)
- `pool_eligibility`: `min_open_score: 70.0`, `allowed_bin_steps: [4,10,20,25,50,100]`
- `position_sizing`: `min_position_usd: 20`, `default_max_position_usd: 100`, `max_candidate_per_cycle: 20`
- `windows`: `open_window_utc: "00:00-23:59"`, `close_window_utc: "00:00-23:59"`, `timezone: "Asia/Shanghai"`
- `execution_intent`: `default_max_slippage_bps: 100`
- `add_policy`: `enabled: true`, `idle_max_usd: 20`, `min_add_usd: 5`, `cooldown_hours: 6`, `max_adds_per_day: 6`, `y_side_room_pct: 25`, `max_wallet_scan_age_seconds: 900`
- `readiness`: structured pre-open readiness (all checks mandatory in one cycle; stale/missing scan blocks open fail-closed)
- `sol`: reserve_lamports 60M, auto_top_up enabled
- `rotation`: Phase 2 requirements

### `profiles.json` — Scoring Configuration
**Structure**:
- `pool_profiles`: per-pair-class weights, `vol_peak_pct`, `apr_cap_pct`
- `position_profiles`: per-pair-class weights for position health
- `pool_thresholds`: `open: 70.0`, `watch: 55.0`
- `position_thresholds`: `close: 40.0`, `review: 60.0`
- `constants`: `depth_min_usd: 50000`, `depth_max_usd: 5000000`, `depeg_zero_dist: 0.005`, `stale_grace_days: 30`, `stale_zero_days: 120`, `collect_min_usd: 5`, `collect_pct_of_value: 0.01`, `turnover_max: 5.0`, `unknown_credit: 0.5`, `il_zero_pct: 10.0`, `il_conc_full_width_ticks: 2000`, `il_conc_max: 4.0`, `fee_expect_max_days: 30`
- `dynamic`: `enabled: true`, percentile, regime, thresholds, position_pnl

---

## 3. Detailed Step-by-Step Flow ("Every Step & Every Rail")

### Cycle Initialization
1. `run_cycle()` in `run_cycle.py` is invoked (CLI entry point)
2. Load config via `lp_scoring.load_config("profiles.json")` → merges `profiles.json` over `DEFAULT_CONFIG`, validates weights sum to 100, validates thresholds, validates constants
3. Load newest pool scan via `newest_file(pools_dir, "pool_scan")` and newest position scan via `newest_file(positions_dir, "position_scan")`
4. Freshness check: `is_fresh(path, max_age_seconds=3900)` (default 65 min). If stale → report failure, exit 2
5. Load wallet scan via `load_wallet_scan(wallet_scans_dir)` → `capital.summarize_wallet()` → `{idle_usdc, dust_total_usdc, deployable_usdc, ...}`
   - If no wallet scan → zeroed summary with error → OPEN signals skipped

### Dynamic Calibration Context Assembly
6. `dynamic.build_context(pools_dir, pool_path, pools, cfg)`:
   a. Load prior scans (up to 60 history scans for percentiles, 30 for regime)
   b. `build_norms()`: Build per-pair-class percentile reference windows from prior scans for components `["fee_yield", "turnover"]`
   c. `detect_regime()`: Classify current market regime from prior vs current volatility/APR medians
   d. `regime_weight_adjust()`: Shift base pool profile weights per regime, renormalize to 100
   e. Return dict: `{regime, norms, blend_alpha=0.7, weights{pair_class}, position_pnl, thresholds: None}`

### Pool Scoring Pass (First Pass)
7. For each pool in the scan:
   - `score_pool(pool, ctx)` called with dynamic context
   - **Classify pair**: `classify_pair(pool)` → `(pair_class, sym_x, sym_y)`
   - **Off-universe/unknown**: score=0, verdict=IGNORE, reason="off-universe pair: policy is stables/high-caps only"
   - **Profile lookup**: `cfg["pool_profiles"][pair_class]` → weights + vol_peak_pct + apr_cap_pct
   - **Component scores**:
     - `fee_yield`: `score_pool_fee_yield(pool, w["fee_yield"], profile["apr_cap_pct"])` + blend with percentile rank from `ctx["norms"]`
     - `turnover`: `score_pool_turnover(pool, w["turnover"])` + blend with percentile rank
     - `depth`: `score_pool_depth(pool, w["depth"])` (log scale $50k-$5M, 0 if tvl < min)
     - `volatility_fit`: `score_pool_volatility_fit(pool, w["volatility_fit"], profile["vol_peak_pct"])` (triangular curve)
     - `depeg_safety`: `score_pool_depeg_safety(pool, w["depeg_safety"], sym_x, sym_y)` (worst stable side; unknown peg → half credit)
   - **Missing data notes**: If `realized_fee_apr <= 0` → reason note; if `volatility <= 0` → reason note
   - **Total score**: `round(clamp(sum(components.values())), 2)`
   - **Verdict**: `pool_verdict(total, thresholds)` → `OPEN_CANDIDATE` (≥open), `WATCH` (≥watch but <open), `IGNORE` (<watch)
   - **Adaptive threshold re-tag** (if ctx dynamic thresholds exist): Re-compute verdicts using adaptive quantile-based thresholds; policy gates (Missy eligible) survive re-tagging

8. **Pool verdicts recorded** in report with evidence (components), reason, and dynamic flags

### Position Scoring Pass (Second Pass)
9. For each position in the position scan:
   - Determine scoring source: `missy` (from `sheldon_policy.json position_source`) or `local`
   - **Missy path** (`score_position_missy`): Check `position_features` version match; if mismatch → fallback to local path
   - **Feature view** (`_view_from_features()`): Map Missy's feature vector onto Sheldon's key names; prices come from raw position record
   - **Local path** (`score_position`): Derive inputs from raw position record
   - **Off-universe/unknown pair**: `_position_policy_close()` → score=0, verdict=CLOSE, reason="off-universe pair: policy is stables/high-caps only"
   - **Core scoring** (`_score_position_core()`):
     a. **Classify pair** from position
     b. **Profile lookup**: `cfg["position_profiles"][pair_class]` → weights
     c. **Component scoring** (each returns `(pts, known)`):
        - `range_status`: `score_position_range_status(pos, w["range_status"])`
          - If explicit bounds present and current within [lower, upper]:
            - `edge_dist = min(current - lower, upper - current) / (span/2)`
            - `edge_frac = clamp(edge_dist / 0.5, 0, 1)` (edge_dist-0→0, center→1)
            - Return `max_pts * (1.0 - edge_frac), True`
          - If bounds missing but `in_range` present:
            - If `in_range=False` and bounds missing → `max_pts * unknown_credit (0.5), False` (partial credit, flags unknown)
            - If `in_range=False` → `0.0, True` (out of range)
            - If `edge_distance_frac` present → `max_pts * (1.0 - clamp(edge, 0, 1)), True`
            - Else → `max_pts, True` (full credit, in range)
          - If no bounds and no `in_range` → `max_pts * unknown_credit, False`
        - `fee_capture`: `score_position_fee_capture(pos, w["fee_capture"], pool)`
          - `_fees_usd_or_none(pos)`: Returns `None` if raw sentinel (u64::MAX) or fees_usd missing
          - If fees unknown → `max_pts * unknown_credit, False` (half credit)
          - Else `estimate_expected_fees(pos, pool, fees_usd_known=True)`:
            - Uses Missy's `expected_fees_usd` if present > 0
            - Else: `value * apr/100 * min(days, fee_expect_max_days=30) / 365`
            - If expected ≤ 0 → half credit, unknown
          - APR normalization: daily fee yield / (value * APR/365) blended with raw capture ratio
          - `blended = 0.7 * clamp(efficiency, 0, 2) + 0.3 * (fees/expected)`
          - `fee_capture_pts = max_pts * clamp(blended, 0, 1)`
          - Efficiency cross-normalizes for pool fee rates; 70% efficiency, 30% raw ratio
        - `staleness`: `score_position_staleness(pos, w["staleness"])`
          - If `days_open` is None → `max_pts * unknown_credit, False` (unverifiable age)
          - If days ≤ grace (30) → `max_pts, True`
          - Else: `max_pts * clamp(1 - (days - grace)/(zero - grace), 0, 1), True` where zero=120
        - `depeg_exposure`: `score_position_depeg_exposure(pos, w["depeg_exposure"], sym_x, sym_y, pool)` (only for profiles that have it)
          - Peg fraction per side: `clamp(1 - |price - 1| / zero_dist, 0, 1)`; unknown price → half credit
          - Single-sided detection: if `single_sided=True` → `max_pts * held_peg_frac, known`
          - Two-sided → `max_pts * min(fx, fy), known`
        - `il_risk`: `score_position_il_risk(pos, w["il_risk"], pool)` (only for bluechip profiles)
          - Net yield coverage: whether fees cover impermanent loss
          - Positive net yield (fees > IL) scores above baseline
          - Break-even (fees ≈ IL) scores at baseline
          - Negative net yield (IL > fees) scores below baseline
          - `net_yield_pct = (fees - IL) / value`
          - `coverage = clamp(1.0 + net_yield_pct, 0, 2)`
          - `il_risk_pts = max_pts * coverage`
          - Replaces pure IL penalty with profitability signal
     d. **Component aggregation**: `components[comp] = pts`; `unknown_parts.append(name)` if not known
     e. **Total score**: `round(clamp(sum(components.values())), 2)`
     f. **Verdict determination** (open/close separation critical):
        - **Dynamic PNL override** (if enabled):
          - If PNL `decisive` and suggests CLOSE/REBALANCE but `unknown_parts` exist → **REVIEW** with reason "PNL suggests {action} but unknown: {parts}"
          - If PNL decisive and action is HOLD/CLOSE/REBALANCE without unknown_parts → verdict = action with detailed reason
        - **Score-based (no decisive PNL)**:
          - If total < close(40) and unknown_parts → **REVIEW**
          - If total < close(40) → **CLOSE** with reason "score {total} < close threshold 40"
          - If total < review(60) → **REVIEW** with reason "score {total} in review band [40, 60)"
          - If total >= review(60) → **HOLD** with reason "score {total} >= hold threshold 60"
        - **Append unknown parts** to reason if verdict != REVIEW and unknown_parts exist
     g. **Fee harvesting**: 
        - `_fees_usd_or_none(pos)` for fees
        - `value = pos["current_value_usd"]`
        - `floor = max(collect_min_usd=5, collect_pct_of_value=0.01 * value)` if value > 0
        - `collect = fees is not None and fees >= floor`
     h. **Return dict** with all fields: position_id, pool, pool_address, pair_class, pair, lower_bound, upper_bound, current_price, current_value_usd, days_open, fees_usd, score, components, verdict, collect_fees, data_quality.unknown_components, reason, expected_pnl

10. **Position verdicts** recorded in report with score, components, data_quality, reason, expected_pnl

### Out-of-Range Grace Update
11. `_load_active_positions()`: Dict of pool_address → position dict for positions not closed
12. `range_state.update_out_of_range_counts(positions, counts)`:
    - For each position: `position_out_of_range()` checks explicit bounds first; if bounds present and current outside [lower, upper] → out of range
    - Falls back to `in_range` flag; if `in_range=False` and bounds missing → not verifiably out of range (return False)
    - Increments counter for out-of-range positions; removes counter for in-range positions
    - Drops counters for positions vanished from scan
13. `range_state.save_range_state(grace_counts, state_dir)`: Persist to `state/out_of_range.json`
14. `range_state.out_of_range_position_verdict(v, grace_counts)`: Applied to each verdict
    - If verdict is CLOSE or REBALANCE:
      - `seen = grace_counts.get(addr, 0)`
      - If seen <= 0 or seen >= allowed (2): pass through unchanged
      - If 1 <= seen < 2: downgrade to HOLD with reason "out-of-range grace: 1/2 consecutive out-of-range runs; {verdict} deferred to the next run"
    - Other verdicts: pass through unchanged

### Adaptive Threshold Re-tag (if dynamic enabled)
15. If dynamic calibration was successful:
    - Collect all pool scores excluding off_universe/unknown
    - `dynamic.adaptive_pool_thresholds(scores, cfg)`: Derive OPEN/WATCH from quantiles (90th/70th) of scored universe
    - Floor at policy min_open_score (70 or 90 from policy), ceil at 90/80
    - Re-tag pool verdicts using adaptive thresholds
    - Policy gates (Missy eligible) survive re-tagging

### Open Candidate Filtering
16. `_filter_open_candidates(open_candidates, active_positions, policy_windows, min_score, min_liquidity, min_volume, allowed_bin_steps, reserved_pools)`:
    a. **Dedup**: One position per pool (enforced earlier by `pools_by_addr`)
    b. **Center != 0**: Skip if center (active_bin/tick) is 0
    c. **Trading window**: `_in_trading_window()` checks `open_window_utc: "00:00-23:59"` and `close_window_utc: "00:00-23:59"` in Asia/Shanghai timezone; blackout dates checked
    d. **Policy gates** (from `sheldon_policy.json`):
       - `min_pool_liquidity_usd: 250k` (Note: profiles.json has different values; policy gates use sheldon_policy.json values)
       - `min_24h_volume_usd: 1M` (profiles has no explicit volume gate in first pass; policy enforces it)
       - `allowed_bin_steps: [4,10,20,25,50,100]` (Meteola only; fail-closed)
       - `min_fee_tvl_ratio: 0.05`
       - `max_volatility_pct: 50`
       - `max_turnover_ratio: 50`
    e. **Meteora bin-step check**: `meteora_bin_step_allowed(pool, "meteora")` — fail-closed if bin step not in allowed list
    f. **Top N by score**: Sort descending, take top `max_opens_per_cycle=3`
    g. **Reserved pools**: Pools marked as rotation targets blocked

17. **`build_strategies(kept_candidates, wallet)`**:
    a. **Open eligibility**: `open_eligible(wallet)` → `idle >= MIN_POSITION_USD (20) and suggested >= MIN`
       - `suggested = min(deployable * 0.75, DEFAULT_MAX_POSITION_USD=100)`
    b. If not eligible → return `[]`
    c. **Rank** kept_candidates by score descending, take top 3
    d. For each candidate:
       - `_range_center(pool, dex)`: center = `active_bin_id` for Meteora, `current_tick_index` → `active_bin_id` for Raydium/Orca
       - `adaptive_half_width(pool)`: Per-adaptive range algorithm (detailed in Section 2)
       - `lower = center - half_width`, `upper = center + half_width`
       - Build strategy dict with: pool_address, pool_name, dex, pair_class, score, suggested_usdc, center, half_width, bin_range, reason

### Funding & Readiness
18. **`capital_plan(wallet)`**: 
    - `deployable = idle_usdc + dust_total_usdc`
    - Strategies sized at 75% of deployable, capped at 100 USD

19. **`plan_funding(strategies, raw_wallet_assets, ...)`**:
    - For each strategy: compare 50/50 token needs against raw wallet balances
    - `SOL_RESERVE_LAMPORTS=20M` never spent
    - `BUY_BUFFER_PCT=2.0` buffer for SOL purchase
    - `MIN_PREP_SWAP_USD=1.0`, `SELL_SURPLUS_MIN_USD=2.0`
    - Returns `{funded, unfunded, prep_swaps, notes, wallet_mtime, assets}`

20. **`gate_prep_swaps(funding["prep_swaps"], signals_dir, funding["wallet_mtime"], ...)`**:
    - Blocks prep swaps when wallet scan older than `MAX_WALLET_SCAN_AGE_SECONDS=180`
    - Blocks if younger than last prep swap + `PREP_CONFIRM_GRACE_SECONDS=60`
    - Blocks if above `MAX_PREP_SWAPS_PER_MINT_PER_HOUR=3`
    - Returns `(allowed_preps, blocked_preps)`

21. **Readiness per strategy** (`build_readiness`):
    - Checks: close_settled, fresh wallet scan, fresh position scan, both tokens ready, SOL readiness, close settlement
    - If not ready → blocking_reasons logged; strategy becomes "review item" (no open signal)

22. **Gate enforcement**: 
    - Only strategies with passing readiness → kept
    - Blocked candidates → added to `open_skipped` with reason "readiness gate: {status} ({reasons})"
    - If close settlement gate blocked → all opens drop, prep swaps blocked

### Signal Writing
23. **`write_signals(report, signals_dir, positions_dir, ...)`**:
    a. **OPEN signals** first: for each ready strategy → `_build_open_signal()` + `strategy.build_strategies()`
       - Sizing: `position_usd = min(deployable * 0.75, max_position_usd=100)`, >= MIN_POSITION_USD (20) required
       - 50/50 USD split into `amount_x`/`amount_y` via prices/decimals
       - `max_slippage_bps=100` on the intent
       - Signal: `{"signal_id", "action": "open", "dex", "pool_address", "position_id", "position_usd", "max_slippage_bps", "reason", "created_at", ...}`
    b. **Dust swap_to_usdc**: Non-USDC dust assets → swap to USDC
    c. **Prep swaps**: Funded shortfall strategies → prep swap signals
    d. **CLOS/REBALANCE → close signals**: `CLOSE`/`REBALANCE` → `close` action; `COLLECT_FEES` → `claim_fees`
    e. **Out-of-range CLOSE/REBALANCE on grace run 1**: Downgraded to HOLD → no signal
    f. **Idle sweep** (after opens): `plan_sweep()` → `execute_sweep()` → `add_liquidity` signal

24. **Signal file format** (George-schema):
    - `signal_id`: `sheldon-{timestamp}`
    - `action`: `open`/`close`/`claim_fees`/`swap`/`add_liquidity`
    - `dex`: `meteora`/`raydium`/`orca`
    - `pool_address`, `position_id`
    - `position_usd`: capital amount
    - `bin_range` or `tick_range`
    - `side`: `quote_only` for adds, `base_only` etc.
    - `max_slippage_bps`: 100
    - `reason`: human readable
    - `created_at`: ISO in Asia/Shanghai timezone

### Idle Sweep (if enabled)
25. **`plan_sweep(report, signals_dir, wallet_mtime, add_state, ...)`**:
    a. Release confirmed reservations
    b. Staleness guard: wallet scan age < `add_policy.max_wallet_scan_age_seconds` (900s)
    c. Cooldown: last add + 6h; day cap: max 6 adds/day
    d. Idle math: `idle_net = idle_raw - committed - reserved`
    e. If `idle_net >= idle_max (20)` → skip (open path owns capital)
    f. If `idle_net < min_add (5)` → skip (not worth tx)
    g. **Target selection**: Among HOLD/REBALANCE tracked positions via `select_add_candidate()`:
       - Y side must be USDC
       - Y-side room >= 25% of range above active bin
       - Pool passes bin_step/TVL/volume gates
       - Headroom: `max_position - tracked_value >= add`
       - Score: live pool score or floor just under min_open_score
    h. Emit `add_liquidity` signal with `position_usd = min(idle_net, headroom)`
    i. Reservation persisted atomically

26. **`execute_sweep(signal, signals_dir, state, ...)`**:
    a. Reserve first (save add_state.json with pending_reservation)
    b. Write signal JSON file
    c. Return (ok, error)

### Output & Logging
27. **Append daily markdown log** to memory directory (YYYY-MM-DD.md)
28. **Emit wake hint**: `WAKE_GEORGE: N signal(s) -> filenames`
29. **If --json**: Dump full report to stdout
30. **Else**: Print `_short_summary(report, review)` human-readable summary

---

## 4. Mathematical & Algorithmic Logic

### Pool LP Opportunity Score Formula

Per pair class, the pool score is a weighted sum of components:

```
pool_score = sum(weight_i * component_i) for i in components
```

Where each component is scored 0-its-weight, then the total is clamped to 0-100.

#### Component Details

**1. fee_yield** (Realized Fee APR)
```
**1. fee_yield** (Realized Fee APR)
- **Updated: sqrt curve** — `fee_yield_pts = max_pts * ratio**2` where `ratio = clamp(realized_fee_apr / apr_cap_pct, 0.0, 1.0)`  
- Rewards moving from 0→10% APR more than 50→60% APR, dampening unsustainable ultra-high APR effects
- `realized_fee_apr` from pool scan
- `apr_cap_pct` from profile (100 for stable_stable, 300 for stable_bluechip/bluechip_bluechip)- `apr_cap_pct` from profile (100 for stable_stable, 300 for stable_bluechip/bluechip_bluechip)

**2. turnover** (Window Volume / TVL)
```
turnover_raw = volume_window / tvl (if tvl > 0, else 0)
turnover_pts = max_pts * clamp(turnover_raw / turnover_max, 0, 1)
```
- `turnover_max` from constants = 5.0 (i.e., 5x window/TLV maxes out the score)
- If tvl ≤ 0 → 0

**3. depth** (TVL Log Scale)
```
**3. depth** (TVL adequacy gate, binary)
- If TVL < depth_min_usd (50k) → **0 points** (slippage risk)
- If TVL ≥ depth_min_usd → **full max_pts** (depth requirement met)
- Further depth beyond minimum does **not** add reward — this is a gate, not a rewarddepth_pts = max_pts * clamp(frac, 0, 1)
```
- Log scale between $50k and $5M TVL
- Tvls below $50k score 0; above $5M score max_pts

**4. volatility_fit** (Triangular Curve)
```
vol = pool.volatility
if vol <= 0 → 0
if vol >= 3 * peak_pct → 0
if vol <= peak_pct → max_pts * (vol / peak_pct)
else → max_pts * ((3 * peak_pct - vol) / (2 * peak_pct))
```
- Peaks at class volatility peak, drops to 0 at 3x peak
- stable_stable: peak=0.5%; stable_bluechip: peak=8%; bluechip_bluechip: peak=10%

**5. depeg_safety** (Stable Peg Distance)
```
For each stable side (sym in STABLECOINS):
  price = pool.token_x_price_usd or pool.token_y_price_usd
  if price is None → unknown_credit (0.5), partial credit
  dist = |price - 1.0|
  side_score = clamp(1 - dist / depeg_zero_dist, 0, 1)  # depeg_zero_dist = 0.005
worst_side = min(sides) across all stable sides
if no stable sides → max_pts (component not applicable)
depeg_pts = max_pts * worst_side
```
- Worst (most-depegged) stable side determines the score
- Unknown price gets half credit (fail-suspicious)
- If no stable sides in the pair → full credit (component not applicable)

#### Blending with Rolling Percentiles
When dynamic calibration is active, absolute component scores are blended with percentile ranks:

```
blended_pts = max_pts * (alpha * rank + (1-alpha) * norm_abs)
```
- `alpha` = 0.7 (default from profiles.json `dynamic.percentile.blend_alpha`)
- `rank` = `percentile_rank(sorted_values, raw_value)`: fraction of reference window at or below raw_value (0.0-1.0)
- `norm_abs` = `abs_pts / max_pts`: the absolute score normalized to [0,1]
- Result: 70% weight to percentile rank, 30% to absolute score

**Percentile rank** (from `dynamic.py`):
```
bisect.bisect_right(sorted_values, value) / len(sorted_values)
```
- Fraction of the reference window at or below the value
- If window empty → 0.5 (median)

#### Dynamic Regime Weight Adjustment
```
factors = regime_shifts[regime_name]  # e.g. high_vol, fee_boom, etc.
adjusted[comp] = max(0.0, base_weight * factors.get(comp, 1.0))
total = sum(adjusted.values())
if total > 0: return {comp: round(w * 100 / total, 4) for comp, w in adjusted.items()}
else: return base_weights unchanged
```
- Regime shifts multiply base weights
- `neutral` regime: empty dict → base weights unchanged
- All adjustments renormalized to sum to 100

**Example: high_vol regime shifts** (from profiles.json):
```
fee_yield: 0.8 × base → reduced weight (fees less reliable in high vol)
turnover: 1.05 × base → slightly increased
depth: 1.0 × base → no change
volatility_fit: 1.4 × base → increased (volatility more informative)
depeg_safety: 1.3 × base → increased (peg risk more important)
```
Then renormalized to sum to 100.

#### Adaptive Pool Thresholds
```
values = sorted(all pool scores excluding off_universe/unknown)
if len(values) < min_pools (20): return None (keep static thresholds)

open_ = quantile(values, open_quantile=0.90)
watch_ = quantile(values, watch_quantile=0.70)

floor_open = max(floor_open=55, _min_open_score=70 from policy)
ceil_open = 90, ceil_watch = 80

open_ = min(max(open_, floor_open), ceil_open)
watch = min(max(watch_, floor_watch=40), ceil_watch)
if watch > open_: watch = open_

return {"open": round(open_, 2), "watch": round(watch_, 2), "source": "adaptive", "n": len(values)}
```
- The adaptive thresholds are derived from the actual scored universe, providing dynamic cut-offs
- Floor ensures minimum standard even in weak universes
- Ceil prevents excessively lenient thresholds in strong universes

#### Expected-PnL Position Verdict
```
value = pos.current_value_usd
apr = pool.realized_fee_apr
vol = pool.volatility
horizon = position_pnl.horizon_days (1.0 default)
entry_bps, exit_bps, claim_usd from pnl config

in_range = lower <= current <= upward
fee_mult = 1.0 if in_range else 0.0  # fees only accrue while in range

fwd_fees = value * (apr/100) * horizon / 365 * fee_mult
concentration = full_width_ticks / range_width (clamped [1, 4])
il_hold = value * concentration * (vol/100)^2 / 8 * 100 * horizon / 100

exit_cost = value * exit_bps / 10000 + claim_usd
entry_cost = value * entry_bps / 10000

expected_hold = fwd_fees - il_hold
expected_close = -exit_cost
expected_rebalance = fwd_fees - exit_cost - entry_cost

options = [("HOLD", expected_hold), ("CLOSE", expected_close), ("REBALANCE", expected_rebalance)]
ordered = sorted(options, key=lambda kv: kv[1], reverse=True)
action, best = ordered[0]
margin = best - ordered[1][1]  # second-best

decisive = margin >= min_pnl_margin_usd (0.01 default)

return {
  "action": action,
  "expected_hold_usd": round(expected_hold, 4),
  "expected_close_usd": round(expected_close, 4),
  "expected_rebalance_usd": round(expected_rebalance, 4),
  "margin_usd": round(margin, 4),
  "in_range": in_range,
  "forward_fees_usd": round(fwd_fees, 4),
  "il_hold_usd": round(il_hold, 4),
  "horizon_days": horizon,
  "decisive": decisive,
}
```
- HOLD: expected fees minus IL over horizon
- CLOSE: negative of exit cost (you lose entry costs + claim fee)
- REBALANCE: fees minus exit and entry costs
- Override only when margin decisively exceeds threshold

#### IL Estimation Formula
```
conc = clamp(il_conc_full_width_ticks / range_width_ticks, 1, 4)  # = 2000 / width, clamped
il_pct = conc * (vol_daily / 100)^2 / 8 * 100 * days_open
```
- Concentration multiplier: wider range → less IL concentration (down to 1x), narrower → more (up to 4x)
- Quadratic in volatility: doubling vol quadruples IL
- Linear in days: longer exposure → more IL

#### Range Status Centering Formula
```
span = upper - lower
edge_dist = min(current - lower, upper - current) / (span / 2)
edge_frac = clamp(edge_dist / 0.5, 0, 1)  # 0 at center, 1 at boundary
range_status_pts = max_pts * (1.0 - edge_frac)
```
- Center of range: 1.0 × max_pts (full credit)
- Edge of range: 0.0 × max_pts (no credit)
- Linear decay from center to boundary
- Out-of-range: 0.0 (verifiably current outside [lower, upper])
- Old formula `0.6 + 0.4 * edge_frac` rewarded being close to the edge; the new centering reward favors being safely in the middle of the range.

#### Range Efficiency (Replaces Staleness)
```
in_range_days = position record's explicit in-range-day count
total_days = days_open from position entry
efficiency_pct = in_range_days / total_days  # clamped to [0, 1]
range_efficiency_pts = max_pts * efficiency_pct
```
- Positions that have spent 90%+ of their lifetime in-range score near-full credit
- Positions that have frequently exited their range score proportionally less
- Brand-new positions (total_days ≈ 0) conservatively get full credit
- Old staleness model penalized long-holding positions; the new range efficiency rewards positions that have **proven** their range selection by staying in-range.

#### Pool Score Quantile Calculation
From `dynamic.py` `_quantile()`:
```
if len(values) == 1: return values[0]
idx = round(q * (len(values) - 1))
idx = min(max(idx, 0), len(values) - 1)
return values[idx]
```
- Linear index-based quantile
- For q=0.9 (90th pctile): idx ≈ 0.9 * (n-1) rounded

---

## 5. Key Takeaways & System Constraints

### Implicit Assumptions
1. **Missy scan data is the source of truth** for all position-level inputs (prices, fees, days_open, il_estimate_pct, etc.). When Missy data is absent or malformed, estimates are used but with reduced confidence (half credit via `unknown_credit=0.5`).
2. **Wallet scan is fresh** (default 3900s / 65min for pool/position, 180s / 3min for wallet in readiness). Stale scans zero out capital and block OPEN signals.
3. **Only stablecoin and high-cap pairs are eligible** for OPEN_CANDIDATE verdicts. Off-universe pairs are hard-gated to IGNORE (pools) or CLOSE (positions).
4. **One open position per pool** — the system enforces dedup early in the pipeline.
5. **Asia/Shanghai (UTC+8) is the canonical timezone** for all timestamps, trading windows, and daily rotation tracking.

### Potential Bottlenecks
1. **Dynamic calibration depends on scan history depth**: With too few historical scans (below min_samples=10 for percentiles, 20 for thresholds), the engine falls back to static behavior. Cold starts will always use static thresholds.
2. **Pool score quantiles require ≥20 investable pools**: If the universe is small (few stable/stable-cap pairs), adaptive thresholds cannot be computed and static thresholds are used.
3. **Position scoring requires complete data**: Any missing component (days_open, fees, price bounds, volatility) reduces the verdict capability — CLOSE requires all components known; otherwise REVIEW.
4. **Out-of-range grace needs 2 consecutive runs**: A position that is truly out-of-range will take 2 full cycles before CLOSE can be emitted. This is a safety feature but adds latency.
5. **Signal writing is sequential**: Opens first, then prep swaps, then closes/claims, then idle sweep. A failure in an early phase can cascade (e.g., if open signal write fails, idle sweep may have insufficient idle capital available).

### Hardware/API Limits
1. **No RPC calls** — the system is entirely offline, relying on Missy's pre-computed scan data. This means:
   - Latency is I/O-bound (reading JSON files)
   - No real-time price data; all prices come from the scan
   - System can run on laptop/limited hardware
2. **File-based state**: State persists in `state/` directory between cycles (`out_of_range.json`, `add_state.json`, `rotation_state.json`)
3. **George integration**: Signal writing depends on George's executor being available at the signal directory path. If George is down, signals queue but may not execute.
4. **Wallet scan directory**: Default `/data/missy-data/wallet_screens`. Missing directory or no scans → zeroed capital → OPEN signals skipped.

### System Dependencies
1. **Missy scan outputs**: `pool_scan-*.json`, `position_scan-*.json`, `wallet_screen-*.json` — must be present and reasonably fresh
2. **profiles.json**: Must be valid JSON with correct weight sums; invalid → fatal ConfigError, exit 1
3. **sheldon_policy.json**: Provides policy rails (min_open_score, windows, sizing, add_policy). The `scoring.source` field determines whether Missy's default scores are used or Sheldon's local scorer.
4. **George signal executor**: For signal writing and execution; signal format must match George's expectations
5. **State directory**: `state/` under the project root for out-of-range counters, sweep reservations, and rotation state

### Edge Cases & Fail-Safes
1. **Missing wallet scan**: → Zeroed capital, OPEN signals skipped, but CLOSE/REVIEW still functional
2. **Stale pool/position data**: → Cycle returns with failures; if any failure → exit 2 (stale/missing data)
3. **Invalid profiles.json**: → ConfigError, exit 1 (fail-closed)
4. **Off-universe pair**: → Always CLOSE/IGNORE regardless of score
5. **Unknown pair class**: → score=0, verdict=IGNORE (pools) or CLOSE (positions)
6. **Sentinel values (u64::MAX in fees_owed_raw)**: → Fees treated as unknown, never zero; `_fees_usd_or_none()` checks for `>= U64_MAX`
7. **Degenerate bin ranges (upper <= lower)**: → Position treated as in-range (fail-safe), or skipped in sweep selection
8. **Empty universes**: → Adaptive thresholds return None → static thresholds used; if no pools score, no OPEN_CANDIDATES
9. **Dynamic disabled**: → All percentile blending, regime shifts, and adaptive thresholds disabled; static profiles.json behavior
10. **Rotation + close settlement**: Two-cycle workflow — cycle N emits ROTATE/close; cycle N+1 settles close before next opens can be emitted. Prevents capital double-counting.