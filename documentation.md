# Documentation

## Configuration

All weights, thresholds, and tuning constants live in `profiles.json` next to
`lp_scoring.py`. Rules:

- Missing file → built-in defaults (identical to the shipped profiles.json).
- Present but invalid (weights not summing to 100, unordered thresholds,
  bad constants) → fatal `ConfigError`, exit code 1 (fail-closed).
- Any subset may be overridden; the rest merges from defaults.

`lp_scoring.set_config()` swaps configuration programmatically (used by tests).

## Pair Classification and Universe Gate

Universe policy: **stablecoins and high-caps only**. Every pool/position is
classified from its pair symbols (explicit `token_x_symbol`/`token_y_symbol`,
or parsed from the pool name like `SOL-USDC (bin 4)`). Results are cached per
symbol triple.

- both in `STABLECOINS` → `stable_stable`
- one stable, one in `HIGH_CAPS` → `stable_bluechip`
- both in `HIGH_CAPS` → `bluechip_bluechip`
- anything else → `off_universe` (pool: IGNORE; position: CLOSE, policy rule)
- unparseable → `unknown` (same gating as off_universe)

## Scoring Formulas

### Scoring Source

Sheldon's scoring source is controlled by `sheldon_policy.json`:

```json
"scoring": {
  "source": "missy",
  "min_open_score": 70.0,
  "version": 1
}
```

- `"missy"` (default): consume the `score` and `score_breakdown` emitted by
  Missy for each pool. The raw score is used directly; verdicts are still
  computed against the local `pool_thresholds` (and adaptive thresholds when
  enabled). Component names are mapped from Missy breakdown keys:
  `yield_score → fee_yield`, `depth_score → depth`,
  `efficiency_score → turnover`, `risk_score → volatility_fit`.
- `"local"`: re-run Sheldon's own `score_pool()` from Missy's raw feature
  vector. This path is retained for backtesting and for pools that lack a
  Missy score.

When the pool record does not contain `score`, the Missy source falls back
locally so legacy scans and tests remain valid.

### Scoring-policy audit trail

Every cycle report carries `scoring_policy` (`source`, `version`,
`min_open_score`), every `pool_scores` entry from the Missy path carries
`score_policy` (`source`, `version`), and every emitted `open` signal carries
`score_policy` — so any verdict or signal can be traced to the exact policy
that produced it. George's `common/rails_loader.py` reads `min_open_score`
from the same `scoring` block (falling back to the legacy
`pool_eligibility` path) and exposes `scoring_source` for diagnostics.

### Backtest: scoring source and gate agreement

`backtest.py` replays history through either scorer:

```bash
python3 backtest.py --scoring-source policy   # follow sheldon_policy.json (default)
python3 backtest.py --scoring-source missy    # replay with Missy's score
python3 backtest.py --scoring-source local    # replay with Sheldon's own model
```

The chosen source is reported as `scoring_source` in the JSON output.
Compare sources before trusting a rule change: Missy's default score
distribution is more permissive than Sheldon's local model, so the OPEN rate differs
materially between sources.

The legacy TVL/volume backstop in `_filter_open_candidates` can only be
removed after a full verification window. Evidence tool:

```bash
python3 backtest.py --gate-report [--json]
```

It walks every historical pool scan and compares Missy's `eligible` flag
against the legacy gates, counts agreement and both mismatch directions
(Missy stricter / legacy stricter), histograms Missy reject reasons, and
prints a backstop verdict: keep while any scan lacks the flag or any pool
shows `legacy_strict`.

### Pool LP Opportunity Score

Each component is scored 0-its-weight, then summed to 0-100 (clamped).

#### `score_pool_fee_yield(pool, max_pts, apr_cap_pct)`

```
fee_yield = max_pts * clamp(realized_fee_apr / apr_cap, 0, 1)
```

- `realized_fee_apr` only; fee/TVL is NOT added (it duplicates turnover)
- `apr_cap_pct`: 100 for stable_stable, 300 otherwise

#### `score_pool_turnover(pool, max_pts)`

```
turnover = max_pts * clamp((volume_window / tvl) / turnover_max, 0, 1)
```

- `turnover_max` = 5.0 (config); 0 if TVL is 0

#### `score_pool_depth(pool, max_pts)`

```
if tvl < depth_min_usd (50k): 0
else: max_pts * log(tvl / 50k) / log(5M / 50k)
```

#### `score_pool_volatility_fit(pool, max_pts, peak_pct)`

Triangular curve peaking at the class peak (0.5% / 8% / 10%):

- vol ≤ 0 → 0 (and a reason note: "volatility unknown")
- vol ≥ 3×peak → 0
- vol ≤ peak: `max_pts * vol/peak`; above: `max_pts * (3*peak - vol)/(2*peak)`

#### `score_pool_depeg_safety(pool, max_pts, sym_x, sym_y)`

Stable side(s) only; worst side wins:

```
per stable side: clamp(1 - |price_usd - 1| / depeg_zero_dist, 0, 1)
score = max_pts * min(side_scores)
unknown stable price → unknown_credit (0.5) for that side
not applicable (no stable side) → full credit
```

### Position Health Score

Each component returns `(points, known)`. Unknown inputs still contribute
`max_pts × unknown_credit` (0.5) to the score but are recorded in
`data_quality.unknown_components`, which caps the verdict at REVIEW.

#### `score_position_range_status(pos, max_pts)`

- lower/upper/current all present and upper > lower:
  - out of range → 0
  - in range → `max_pts * (0.6 + 0.4 * edge_frac)`; edge_frac is 1 in the
    middle 50% of the range, fading to 0 at the bounds
- else if `in_range` flag present → max or 0
- else unknown (half credit)

#### `score_position_fee_capture(pos, max_pts, pool)`

- `fees_usd` unknown (missing, or `fees_owed_raw` contains a `u64::MAX`
  sentinel) → unknown
- `expected_fees_usd` used when reported; otherwise estimated:
  `value × realized_fee_apr/100 × min(days_open, fee_expect_max_days)/365`
  (`fee_expect_max_days` = 30)
- otherwise `max_pts * clamp(fees/expected, 0, 1)`

#### `score_position_il_risk(pos, max_pts, pool)` (bluechip classes)

- `il_estimate_pct` used when reported
- otherwise estimated from pool daily volatility and range concentration:
  `il% = conc × (vol/100)² / 8 × 100 × days`, with
  `conc = clamp(il_conc_full_width_ticks / range_width, 1, il_conc_max)`
  (2000 ticks reference, cap 4x; unknown width assumed fully concentrated)
- score `max_pts * clamp(1 - il / il_zero_pct)` (`il_zero_pct` = 10)
- unknown when pool volatility is 0 or days_open missing

#### `score_position_depeg_exposure(pos, max_pts, sym_x, sym_y, pool)` (stable classes)

- per stable side: `clamp(1 - |price - 1| / depeg_zero_dist, 0, 1)`;
  unknown price → half credit and marks the component unknown
- single-sided position (one token ui 0): score = held side's peg fraction
- two-sided: `max_pts * min(fx, fy)`

#### `score_position_staleness(pos, max_pts)`

- `days_open` missing → unknown (half credit)
- ≤ stale_grace_days (30) → full credit
- linear erosion to 0 at stale_zero_days (120)

## Verdict Thresholds (config: `pool_thresholds`, `position_thresholds`)

- Pool: open ≥ 70.0, watch ≥ 55.0 (adaptive mode: quantiles of the scored
  universe, floored by `max(floor_open, sheldon_policy min_open_score)`)
- Position: close < 40.0, review < 60.0, hold ≥ 60.0
- **Open/close separation**: CLOSE requires every component scored on known
  data; any unknown component caps at REVIEW. Policy CLOSE (off-universe /
  unknown pair) ignores this rule.
- Collect fees: HOLD + fees known + fees ≥ max(collect_min_usd $5,
  collect_pct_of_value 1% × value)

Every verdict includes a `reason` string, e.g.
`score 35.0 < close threshold 40.0 but unknown: fee_capture` or
`volatility unknown`.

## Data Quality Tracking

Each position result carries:

```json
"data_quality": {
  "unknown_components": ["fee_capture", "il_risk"],
  "policy_close": false
}
```

`policy_close: true` marks the off-universe hard gate. Consumers (dashboards,
George) can distinguish "bad position" from "bad data".

## Signal Building (run_cycle.py)

### `_build_open_signal(strategy, v, idx, base)`

Requirements (returns `None` if any unmet): supported dex, `_pool` present,
pool passes `meteora_bin_step_allowed` (fail-closed), score present and ≥
policy `min_open_score`, positive prices/decimals, `suggested_usdc >=
MIN_POSITION_USD`, and a non-`None` `strategy["bin_range"]` from
`strategy.build_strategies` (volatility-adaptive, not fixed ±100). Produces a
50/50-split `open` intent with `max_slippage_bps=100`.

Open candidates first pass `_filter_open_candidates`: dedup (one position per
pool), duplicate-pool, `center == 0` refusal, trading window/blackout
(`_in_trading_window`, Asia/Shanghai), Missy eligibility flag (Phase 2;
legacy TVL/volume backstop only when the flag is absent), and the
Meteora `allowed_bin_steps` rail. `build_strategies` keeps the top
`max_opens_per_cycle=3` by score. `plan_funding` + `gate_prep_swaps` then split
funded strategies (→ open) from shortfalls (→ prep `swap`) and surpluses
(→ sell `swap`). Opens are written first so `idle_sweep.plan_sweep` can treat
pending opens/preps as committed capital.

Other signals: `CLOSE`/`REBALANCE` (post-grace) → `close`, `COLLECT_FEES` →
`claim_fees`, dust assets → `swap_to_usdc`, funding deltas → prep `swap`,
leftover idle → `add_liquidity` sweep. HOLD/REVIEW/WATCH/IGNORE produce no signal.

### `_range_center(pool, dex)` (also `strategy._range_center`)

- Meteora: `active_bin_id`
- Raydium/Orca: `current_tick` → `current_tick_index` → `active_bin_id`

### Capital input (`capital.summarize_wallet`, `readiness.load_raw_wallet`)

1. Newest Missy wallet scan under `--wallet-scans-dir`
   (`wallet_screen-latest.json`): idle USDC + dust assets + per-token balances
2. 0.0 (skips OPEN signal)

No `SHELDON_IDLE_USDC` env var, no `wallet_balances.json` cache, no RPC lookup.

## Strategy, Readiness, Grace, Sweep

- `strategy.py`: `get_policy()` (from `sheldon_policy.json`; now includes
  `scoring.source`, `min_open_score`, `allowed_bin_steps`, and deprecated
  legacy gates), `suggested_position_usd` (`min(deployable*0.75, max)`), `open_eligible`, `open_eligible` gate, `build_strategies`
  (top 3 by score), `adaptive_half_width` (see architecture.md), `_range_center`,
  `meteora_bin_step_allowed` (fail-closed), `capital_plan`. Meteora width capped by
  George `execution_limits.json` `max_meteora_range_width=70`.
- `readiness.py`: `plan_funding`, `gate_prep_swaps`, `build_prep_swap_signal`,
  `load_raw_wallet`. Rails: `SOL_RESERVE_LAMPORTS=20M`, `BUY_BUFFER_PCT=2.0`,
  `MIN_PREP_SWAP_USD=1.0`, `SELL_SURPLUS_MIN_USD=2.0`,
  `MAX_PREP_SWAPS_PER_MINT_PER_HOUR=3`, `PREP_CONFIRM_GRACE_SECONDS=60`,
  `MAX_WALLET_SCAN_AGE_SECONDS=180`.
- `range_state.py`: `position_out_of_range` (price bounds, else `in_range` flag;
  unknown = in-range), `update_out_of_range_counts`,
  `out_of_range_position_verdict` (CLOSE/REBALANCE → HOLD on runs 1..`ALLOWED_OUT_OF_RANGE_RUNS-1=1`),
  `state/out_of_range.json`.
- `idle_sweep.py`: `plan_sweep`/`execute_sweep`, `load_add_state`,
  `state/add_state.json`, `RESERVATION_MAX_AGE_SECONDS=3600`,
  floor-score epsilon 0.01 below `min_open_score`.
- `capital.py`: `summarize_wallet`, `USDC_MINT`, `SOL_MINT`,
  `RESERVED_MINTS` (SOL + owner hold), `DUST_MIN_USD=1.0`,
  `deployable = idle + dust_total`.
- `profiles.py` / `models.py`: legacy, unused (nothing imports them).

## Backtesting

`backtest.py` replays the Missy scan archive through the scoring engine:

```
python3 backtest.py [--data-dir /data/missy-data] [--pools-dir D] [--positions-dir D]
  [--horizon 4] [--width 200] [--position-value-usd 100]
  [--entry-cost-bps 50] [--exit-cost-bps 50] [--claim-cost-usd 0.02]
  [--bootstrap-samples 1000] [--seed 42] [--dynamic] [--json] [--detail]
```

- **Pool replay**: scores each historical `pool_scan`; for pools that were
  OPEN_CANDIDATE at time T, measures forward `realized_fee_apr` over the
  next `--horizon` scans and range survival (did price stay inside the
  would-be ±width range). Baseline = all scored pools, so you can see
  whether OPEN selection beats average.
- **Position replay**: scores each historical `position_scan`; for each
  verdict tracks the forward `current_value_usd` trajectory of the same
  position address across later scans.

Caveat: scans before 2026-09-25 15:16 UTC lack Jupiter price enrichment
(`token_*_price_usd`), so older scans score lower on depeg/fee components.
The backtest reports per-scan input completeness so you can segment.
Beyond verdict replay it computes synthetic PnL per OPEN_CANDIDATE (entry/exit
bps + claim USD on `--position-value-usd`), walk-forward stats, and bootstrap
resampling (`--bootstrap-samples`, `--seed`).

`tuner.py` usage:

```
python3 tuner.py [--data-dir D] [--pools-dir D] [--horizon N] [--width N]
  [--candidates 30] [--seed 42] [--train-frac 0.8]
  [--output profiles.tuned.json] [--apply]
```

Randomly perturbs base pool weights, evaluates train/validation (last
`1-train_frac` of scans) mean PnL via the backtest, keeps candidates beating
the baseline on validation, writes the top 10 + best to `--output`.
`--apply` overwrites `profiles.json` (backup `profiles.json.bak.<ts>`).

## CLI Arguments

### `lp_scoring.py`

| Arg | Default | Description |
|---|---|---|
| `--pools-dir` | `/data/missy-data/pool_screens` | Pool scan directory |
| `--positions-dir` | `/data/missy-data/position_scans` | Position scan directory |
| `--wallet-scans-dir` | `/data/missy-data/wallet_screens` | Wallet scan directory |
| `--json` | — | Full JSON report to stdout |

### `run_cycle.py`

| Arg | Default | Description |
|---|---|---|
| `--pools-dir` | `/data/missy-data/pool_screens` | Pool scan directory |
| `--positions-dir` | `/data/missy-data/position_scans` | Position scan directory |
| `--wallet-scans-dir` | `/data/missy-data/wallet_screens` | Wallet scan directory |
| `--write-signals` | — | Write George-schema signal files |
| `--signals-dir` | george signals/pending | Signal output directory |
| `--memory-dir` | sheldon memory | Daily markdown log directory |
| `--state-dir` | `<repo>/state` | Grace + sweep state directory |
| `--json` | — | Full JSON report to stdout |
| `--max-age-seconds` | `3900.0` | Data freshness window |

Note: Missy's cron cadence has gaps > 3900 s (e.g. 06:59 → 11:56); raise
`--max-age-seconds` or align the schedule, else runs exit 2.

### `backtest.py`

| Arg | Default | Description |
|---|---|---|
| `--data-dir` | `/data/missy-data` | Base dir (pools/positions default under it) |
| `--pools-dir` / `--positions-dir` | `<data-dir>/pool_screens` etc. | Override scan dirs |
| `--horizon` | `4` | Scans to look ahead |
| `--width` | `200` | Would-be open range width in bins/ticks |
| `--position-value-usd` | `100` | Synthetic position size |
| `--entry-cost-bps` / `--exit-cost-bps` | `50` / `50` | Swap costs |
| `--claim-cost-usd` | `0.02` | Flat claim cost per close |
| `--bootstrap-samples` | `1000` | Bootstrap resamples |
| `--seed` | `42` | Random seed |
| `--dynamic` | — | Rolling history + adaptive thresholds + regime weights |
| `--json` / `--detail` | — | JSON output / per-candidate rows |

### `tuner.py`

| Arg | Default | Description |
|---|---|---|
| `--data-dir` / `--pools-dir` | `/data/missy-data` etc. | Scan dirs |
| `--horizon` / `--width` | `4` / `200` | Backtest horizon/width |
| `--candidates` | `30` | Weight perturbations tried |
| `--seed` | `42` | Random seed |
| `--train-frac` | `0.8` | Train split (rest is validation) |
| `--output` | `profiles.tuned.json` | Search output |
| `--apply` | — | Overwrite `profiles.json` (with backup) |

## Constants (config: `constants`)

| Key | Default | Description |
|---|---|---|
| `depth_min_usd` / `depth_max_usd` | 50k / 5M | Depth log-scale bounds |
| `depeg_zero_dist` | 0.005 | Stable depeg zero-credit distance |
| `stale_grace_days` / `stale_zero_days` | 30 / 120 | Staleness erosion window |
| `collect_min_usd` | 5.0 | Min fee USD to trigger collection |
| `collect_pct_of_value` | 0.01 | Fee/value share to trigger collection |
| `turnover_max` | 5.0 | Turnover window multiple that maxes the component |
| `unknown_credit` | 0.5 | Credit fraction for unknown inputs |
| `il_zero_pct` | 10.0 | IL% that zeroes il_risk |
| `il_conc_full_width_ticks` | 2000 | Reference range width for conc = 1 |
| `il_conc_max` | 4.0 | Max concentration multiplier |
| `fee_expect_max_days` | 30.0 | Cap on days used in fee expectation |
| `il_vol_window_days` | 1.0 | Present in `profiles.json` but currently unread by code |

Strategy rails live in `sheldon_policy.json` (via `strategy.get_policy()`):
`min_position_usd` 20, `default_max_position_usd` 100,
`max_opens_per_cycle` 3, `min_open_score` 70, `allowed_bin_steps` [10,20,25,50,100],
`min_fee_tvl_ratio` 0.05, `max_volatility_pct` 50, `max_turnover_ratio` 50,
`default_max_slippage_bps` 100, windows `00:00-23:59` + `Asia/Shanghai`,
`add_policy` (enabled, idle_max 20, min_add 5, cooldown 6h, max/day 6,
y_side_room 25%, max_wallet_scan_age 900). Adaptive widths:
`WIDTH_FACTOR` 0.5, `MIN_HALF_WIDTH` 10, `MAX_HALF_WIDTH` 1000,
Meteora max 70 bins inclusive (from George `execution_limits.json`).
`SUPPORTED_DEXES` {meteora, raydium, orca}.

Dynamic knobs (config: `dynamic`): rolling percentiles (`blend_alpha`,
`min_samples` — `profiles.json` ships 10, code default 20), regime shifts,
adaptive-threshold quantiles (floor `max(floor_open, policy min_open_score)`),
expected-PnL override (1d horizon, 50bps entry/exit, $0.02 claim, $0.01 margin).

Universe note: scoring enforces `lp_scoring.STABLECOINS/HIGH_CAPS`
(`USDC,USDT,PYUSD,USDG` + `SOL,WSOL,WBTC,CBBTC,WETH,ETH,JSOL,MSOL,BSOL,ZEC`);
`sheldon_policy.json:universe` is wider — the scoring lists win.

## Tests

```
python3 -m unittest test_lp_scoring test_run_cycle test_dynamic test_idle_sweep test_range_state
```

Covers config validation, classification, every pool/position component,
estimation math, the open/close data-quality rule, end-to-end cycles on
synthetic scans, and the dynamic calibration layer.
