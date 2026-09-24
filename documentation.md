# Documentation

## Pair Classification and Universe Gate

Universe policy: **stablecoins and high-caps only**. Every pool/position is classified
from its pair symbols (explicit `token_x_symbol`/`token_y_symbol`, or parsed from the
pool name like `SOL-USDC (bin 4)`):

- both in `STABLECOINS` → `stable_stable`
- one stable, one in `HIGH_CAPS` → `stable_bluechip`
- both in `HIGH_CAPS` → `bluechip_bluechip`
- anything else → `off_universe` (pool: IGNORE; position: CLOSE)
- unparseable → `unknown` (same gating as off_universe)

Each class has its own scoring profile (`POOL_PROFILES`, `POSITION_PROFILES`) with
class-appropriate weights, volatility peak, and APR cap.

## Scoring Formulas

### Pool LP Opportunity Score

Each component is scored 0-its-weight, then summed to 0-100.

#### `score_pool_fee_yield(pool, max_pts, apr_cap_pct)`

```
fee_yield = max_pts * clamp(apr / apr_cap, 0, 1)
```

- `realized_fee_apr` only; fee/TVL is NOT added (it duplicates the turnover signal)
- `apr_cap_pct`: 100 for stable_stable, 300 for bluechip classes

#### `score_pool_turnover(pool, max_pts)`

```
turnover = max_pts * clamp( (volume_window / tvl) / 5, 0, 1)
```

- Turnover of 5x over the window maxes out; 0 if TVL is 0

#### `score_pool_depth(pool, max_pts)`

```
if tvl < 50_000: score = 0
else: score = max_pts * log(tvl / 50_000) / log(5_000_000 / 50_000)
```

- Log-scale between DEPTH_MIN_USD (50K) and DEPTH_MAX_USD (5M)

#### `score_pool_volatility_fit(pool, max_pts, peak_pct)`

Triangular curve peaking at the profile's volatility peak
(0.5% stable_stable, 8% stable_bluechip, 10% bluechip_bluechip):
- vol <= 0 → 0; vol >= 3*peak → 0
- vol <= peak: max_pts * vol / peak; vol > peak: max_pts * (3*peak - vol) / (2*peak)

#### `score_pool_depeg_safety(pool, max_pts, sym_x, sym_y)`

Stable side(s) only; worst side wins:

```
per stable side: clamp(1 - |price_usd - 1| / 0.005, 0, 1)
score = max_pts * min(side_scores)
score = max_pts * 0.5 for a side with unknown price (fail-suspicious)
```

- 0.5% from $1 (DEPEG_ZERO_DIST) zeroes the side
- Not applicable (bluechip_bluechip): component omitted

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

## Verdict Thresholds

- Pool: open >= 70.0, watch >= 55.0
- Position: close < 40.0, review >= 40.0 and < 60.0, hold >= 60.0
- Collect fees: HOLD position with fees_usd >= 10.0

## Signal Building

### `_build_open_signal(v, idx, base)`

Builds a George-schema `open` signal for supported DEXes.

**Requirements** (returns `None` if any unmet):
- `dex` in `SUPPORTED_DEXES` (meteora, raydium, orca)
- `_pool` present in verdict
- `token_x_price_usd`, `token_y_price_usd` > 0
- `token_x_decimals`, `token_y_decimals` > 0
- `center` (bin/tick index) != 0
- `idle_usdc` > 0

**Allocation rule**:
```
position_usd = min(idle_usdc * 0.25, DEFAULT_MAX_POSITION_USD)
               >= MIN_POSITION_USD (15.0)

half_usd = position_usd / 2
amount_x = int((half_usd / px_x) * 10^dec_x)
amount_y = int((half_usd / px_y) * 10^dec_y)

half_width = max(1, DEFAULT_MAX_RANGE_WIDTH // 2)  # 100
bin_range = [center - 100, center + 100]
```

### `_range_center(pool, dex)`

Returns the pool's current position index:
- **Meteora**: `active_bin_id` (DLMM bin ID)
- **Raydium/Orca**: `current_tick` first, then `active_bin_id` fallback

### `_read_usdc_balance()`

Resolution order:
1. `SHELDON_IDLE_USDC` env var (USD float)
2. `/data/missy-data/wallet_balances.json` → `USDC.usd_value`
3. Solana RPC `getTokenAccountsByOwner` via George's config
4. 0.0 (skips OPEN signal)

### `_fetch_usdc_balance_rpc()`

Queries Solana RPC for the wallet in George's config (`agent.config.json`). Returns raw USDC balance / 1_000_000 (6 decimals).

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

## Constants

| Constant | Value | Description |
|---|---|---|
| `MIN_POSITION_USD` | `15.0` | Minimum position USD value |
| `DEFAULT_MAX_POSITION_USD` | `100.0` | Maximum position USD value |
| `DEFAULT_MAX_RANGE_WIDTH` | `200` | Max range width in bins/ticks |
| `DEFAULT_MAX_SLIPPAGE_BPS` | `100` | Max slippage in basis points |
| `SUPPORTED_DEXES` | `{"meteora", "raydium", "orca"}` | DEXes George can execute |
| `USDC_MINT` | `EPjFWdd5...TDtWv` | USDC mint address on Solana |