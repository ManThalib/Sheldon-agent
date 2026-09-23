# Documentation

## Scoring Formulas

### Pool LP Opportunity Score

Each component is scored 0-its-weight, then summed to 0-100.

#### `score_pool_fee_yield(pool)`

```
fee_yield = 25 * clamp(apr / 300, 0, 1) + 10 * clamp(fee_tvl_ratio / 1, 0, 1)
```

- `realized_fee_apr`: realized annual fee APR percentage
- `fee_tvl_ratio`: fee/TVL ratio converted to percentage (×100)
- Ratio of 1% daily fee/TVL is exceptional and maxes the 10pt component

#### `score_pool_turnover(pool)`

```
turnover = 20 * clamp( (volume_window / tvl) / 5, 0, 1)
```

- Turnover of 5x over the window maxes the 20pt component
- Returns 0 if TVL is 0

#### `score_pool_depth(pool)`

```
if tvl < 50_000: score = 0
else: score = 15 * log(tvl / 50_000) / log(5_000_000 / 50_000)
```

- Log-scale between DEPTH_MIN_USD (50K) and DEPTH_MAX_USD (5M)
- Below minimum returns 0; above maximum returns 15

#### `score_pool_volatility_fit(pool)`

Triangular curve peaking at VOLATILITY_PEAK_PCT (8%):
- vol <= 0 → 0
- vol >= 3*peak (24%) → 0
- vol <= peak: score = 20 * vol / peak
- vol > peak: score = 20 * (3*peak - vol) / (2*peak)

#### `score_pool_bin_step_fit(pool)`

- bin_step <= 0 → neutral 5
- spacing_pct = bin_step * 0.02 (DLMM bin step in % price distance)
- ratio = spacing_pct / vol
- ratio < 0.2 → 10 * ratio / 0.2 * 0.5 (0-5, too fine)
- ratio <= 1.5 → 5 + 5 * (ratio - 0.2) / 1.3 (5-10, well-matched)
- ratio > 1.5 → max(0, 10 - (ratio - 1.5) * 4) (decays if too coarse)

### Position Health Score

Each component is scored 0-its-weight, then summed to 0-100.

#### `score_position_range_status(pos)`

- If `in_range` key present: 35 if True, 0 if False
- Else if lower/upper/current all present: 35 if current in [lower, upper], else 0
- Missing data → neutral-middle 17.5

#### `score_position_fee_capture(pos)`

```
if earned is None or expected is None or expected <= 0: 12.5
ratio = earned / expected
score = 25 * clamp(ratio, 0, 1)
```

- Over-earning caps at max (ratio > 1 still clamped to 1)
- Missing/zero expected → neutral 12.5

#### `score_position_il_risk(pos)`

```
il = il_estimate_pct
if il is None: 12.5
score = 25 * clamp(1 - il/10, 0, 1)
```

- il_estimate_pct of 10%+ → score 0
- Missing data → neutral 12.5

#### `score_position_time_decay(pos)`

```
days = days_open
if days is None: 15 (fresh)
if days <= 14: 15
else: 15 * clamp(1 - (days - 14) / 28, 0, 1)
```

- Decays linearly from 15 to 0 over 14→42 days (3x horizon)

## Verdict thresholds

- Pool: open >= 70.0, watch >= 55.0
- Position: close < 40.0, review >= 40.0 and < 60.0, hold >= 60.0
- Collect fees: HOLD position with fees_usd >= 10.0

## CLI Arguments

### `lp_scoring.py`

| Arg | Default | Description |
|---|---|---|
| `--pools-dir` | `/data/missy-data/pool_screens` | Directory containing `pool_scan-*.json` files |
| `--positions-dir` | `/data/missy-data/position_scans` | Directory containing `position_scan-*.json` files |
| `--json` | — | Output full JSON report to stdout |

### `run_cycle.py`

| Arg | Default | Description |
|---|---|---|
| `--pools-dir` | `/data/missy-data/pool_screens` | Pool data directory |
| `--positions-dir` | `/data/missy-data/position_scans` | Position data directory |
| `--write-signals` | — | Write George-schema signal files |
| `--signals-dir` | `/data/.openclaw/.../signals/pending` | Signal output directory |
| `--memory-dir` | `/data/.openclaw/.../sheldon/memory` | Daily markdown log directory |
| `--json` | — | Print full JSON report to stdout |
| `--max-age-seconds` | `3900.0` | Max age (seconds) for data freshness |