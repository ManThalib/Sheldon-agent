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

- Pool: open ≥ 70.0, watch ≥ 55.0
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

## Signal Building (run_cycle.py, unchanged in v2)

### `_build_open_signal(v, idx, base)`

Requirements (returns `None` if any unmet): supported dex, `_pool` present,
positive prices/decimals, non-zero center index, idle USDC > 0, and the
allocation rails (min $15, max $100, 25% of idle USDC, 50/50 split,
±100 bin/tick range).

### `_range_center(pool, dex)`

- Meteora: `active_bin_id`
- Raydium/Orca: `current_tick` → `current_tick_index` → `active_bin_id`

### `_read_usdc_balance()`

1. `SHELDON_IDLE_USDC` env var
2. `/data/missy-data/wallet_balances.json` → `USDC.usd_value`
3. Solana RPC via George's config
4. 0.0 (skips OPEN signal)

## Backtesting

`backtest.py` replays the Missy scan archive through the scoring engine:

```
python3 backtest.py [--data-dir /data/missy-data] [--horizon 4] [--json]
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

## CLI Arguments

### `lp_scoring.py`

| Arg | Default | Description |
|---|---|---|
| `--pools-dir` | `/data/missy-data/pool_screens` | Pool scan directory |
| `--positions-dir` | `/data/missy-data/position_scans` | Position scan directory |
| `--json` | — | Full JSON report to stdout |

### `run_cycle.py`

| Arg | Default | Description |
|---|---|---|
| `--pools-dir` / `--positions-dir` | Missy data dirs | Input directories |
| `--write-signals` | — | Write George-schema signal files |
| `--signals-dir` | george signals/pending | Signal output directory |
| `--memory-dir` | sheldon memory | Daily markdown log directory |
| `--json` | — | Full JSON report to stdout |
| `--max-age-seconds` | `3900.0` | Data freshness window |

Note: Missy's cron cadence has gaps > 3900 s (e.g. 06:59 → 11:56); raise
`--max-age-seconds` or align the schedule, else runs exit 2.

### `backtest.py`

See Backtesting above.

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

Signal rails (in run_cycle.py): `MIN_POSITION_USD` 15, `DEFAULT_MAX_POSITION_USD`
100, `DEFAULT_MAX_RANGE_WIDTH` 200, `DEFAULT_MAX_SLIPPAGE_BPS` 100,
`SUPPORTED_DEXES` {meteora, raydium, orca}.

## Tests

`python3 test_lp_scoring.py -v` — 43 tests covering config validation,
classification, every pool/position component, estimation math, the
open/close data-quality rule, and two end-to-end cycles on synthetic scans.
