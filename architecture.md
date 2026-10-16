## Verdict Mapping

### RFNE Model Key Shifts (newwest update)

| Old Assumption | New Assumption |
|---|---|
|---|---|
| Staleness = age penalty (older = worse) | Staleness → Range efficiency (how long stayed in-range) |
| IL = pure penalty (always subtracts from score) | IL → Net yield coverage (fees - IL; profitable positions score above baseline) |
| Range edge proximity = safety (closer to edge = lower score but still rewarded) | Range centering = safety (center is safest; approaching edge reduces score) |
| Fee capture = `fees / expected` raw ratio | Fee efficiency = ratio normalized by pool APR (high-fee pools rewarded) |
| Depeg exposure = additive score component | Depeg risk = multiplicative penalty cap (max 30% score reduction) |



- **Pool** (static defaults): ≥ 70 → OPEN_CANDIDATE, ≥ 55 → WATCH, else IGNORE
  - With adaptive thresholds, cut-offs come from the quantiles of the scored universe
- **Position**: < 40 → CLOSE, < 60 → REVIEW, ≥ 60 → HOLD
  - CLOSE **only** when every component was scored on known data
  - Any unknown component caps the verdict at REVIEW (missing data is a data
    problem, not a position problem)
  - Off-universe/unknown pair → CLOSE regardless of data (policy rule)
  - Expected-PnL override can choose HOLD/CLOSE/REBALANCE when all required inputs exist
- REBALANCE: CLOSE/REVIEW position whose pool is still an OPEN_CANDIDATE
- COLLECT_FEES: HOLD position with fees ≥ max($5, 1% of position value);
  unknown fees never trigger collection
- Every verdict carries a human-readable `reason` string

## Estimations (when Missy data is absent)

| Missing input | Estimation | Fallback |
|---|---|---|
| `expected_fees_usd` | `value × realized_fee_apr/100 × min(days_open, 30)/365` | Component unknown (half credit, verdict capped) |
| `il_estimate_pct` | `conc × (vol_daily/100)² / 8 × 100 × days`, where `conc = clamp(2000/range_width_ticks, 1, 4)` | Component unknown |
| `fees_usd` sentinel (`u64::MAX` raw) | — | Fees unknown, never zero |
| `days_open` missing | — | Staleness unknown |
| Pool volatility 0 | — | volatility_fit 0 + reason note |

## Backtesting

`backtest.py` replays the scan history:

- **Pool replay**: scores every historical pool scan; tracks forward realized
  fee APR and range survival over a horizon of subsequent scans for
  OPEN_CANDIDATE pools, against an all-pool baseline.
- **Position replay**: scores historical position scans; for each verdict,
  tracks forward value trajectory for the same position address.
- **Synthetic PnL**: per-candidate PnL with entry/exit swap costs (bps),
  claim cost (USD), and a configurable position size.
- **Robustness**: walk-forward stats plus bootstrap resampling (`--bootstrap-samples`, `--seed`).

Run: `python3 backtest.py [--data-dir /data/missy-data] [--pools-dir D] [--positions-dir D] [--horizon N] [--width N] [--position-value-usd V] [--entry-cost-bps N] [--exit-cost-bps N] [--claim-cost-usd V] [--bootstrap-samples N] [--seed N] [--dynamic] [--json] [--detail]`

Add `--dynamic` to run with rolling history, regime shifts and adaptive thresholds.

`tuner.py` runs the same backtest over `--candidates` weight perturbations,
splits scans `--train-frac` (default 0.8) train/validation, and writes the
top rows to `--output` (default `profiles.tuned.json`). `--apply` copies the
best weights into `profiles.json` (with a `profiles.json.bak.<ts>` backup).

## Signal Generation

`run_cycle.py` converts verdicts into George-schema signal files:
`CLOSE`/`REBALANCE` → `close`, `COLLECT_FEES` → `claim_fees`,
`OPEN_CANDIDATE` → `open` via `_build_open_signal` + `strategy.build_strategies`,
dust assets → `swap_to_usdc`, funding shortfalls/surpluses → prep `swap`,
leftover idle → `add_liquidity` sweep. HOLD/REVIEW/WATCH/IGNORE produce no signal.
Out-of-range CLOSE/REBALANCE on grace run 1 is downgraded to HOLD (no signal).

### OPEN Signal Pipeline

```
_filter_open_candidates: dedup (one position per pool) → center != 0 →
  trading window/blackout → policy TVL/volume/score gates →
  meteora_bin_step_allowed (fail-closed)
  → top max_opens_per_cycle=3 by score (build_strategies)
  → plan_funding: funded → open; shortfall → prep swap; surplus → sell
  → gate_prep_swaps: rescan-wait (PREP_CONFIRM_GRACE_SECONDS=60) + hourly loop guard
```

Sizing: `position_usd = min(deployable_usdc * 0.75, DEFAULT_MAX_POSITION_USD)`,
`>= MIN_POSITION_USD` required; 50/50 USD split into `amount_x`/`amount_y`
via prices/decimals; `max_slippage_bps=100` on the intent.

### Adaptive Ranges (strategy.py)

```
target_half_fraction = (volatility_pct / 100) * WIDTH_FACTOR (0.5)
Meteora: step_ratio = 1 + bin_step/10000
Raydium/Orca: step_ratio = 1.0001 ^ tick_spacing
half_width = clamp(ceil(log(1+f) / log(step_ratio)), MIN_HALF_WIDTH=10, per-DEX max)
Meteora max: George max_meteora_range_width=70 bins inclusive → half-width 34
Raydium/Orca max: MAX_HALF_WIDTH=1000
volatility <= 0 or unknown spacing → MIN_HALF_WIDTH
bin_range = [center - half_width, center + half_width]
```

Where `center` is `active_bin_id` for Meteora, tick for Raydium/Orca
(`current_tick` → `current_tick_index` → `active_bin_id`).

Policy gates live in `sheldon_policy.json` (`strategy.get_policy()`):
`min_open_score=70`, `min_pool_liquidity_usd=250k`, `min_24h_volume_usd=1M`,
`allowed_bin_steps=[4,10,20,25,50,100]`, `min_fee_tvl_ratio=0.05`,
`max_volatility_pct=50`, `max_turnover_ratio=50`, `min_position_usd=20`,
`default_max_position_usd=100`, `max_opens_per_cycle=3`,
windows `00:00-23:59` + `Asia/Shanghai`, `default_max_slippage_bps=100`,
`add_policy={enabled, idle_max=20, min_add=5, cooldown=6h, max/day=6, y_side_room=25%, max_wallet_scan_age=900}`.
Universe note: scoring enforcement uses `lp_scoring.STABLECOINS/HIGH_CAPS`;
`sheldon_policy.json:universe` is wider — the scoring lists win.

### Readiness / Prep Swaps (readiness.py)

`plan_funding` compares each strategy's 50/50 token needs against raw wallet
balances (SOL reserve `SOL_RESERVE_LAMPORTS=20M` never spent; buy buffer
`BUY_BUFFER_PCT=2.0`; `MIN_PREP_SWAP_USD=1.0`; `SELL_SURPLUS_MIN_USD=2.0`).
`gate_prep_swaps` blocks prep swaps when the wallet scan is older than
`MAX_WALLET_SCAN_AGE_SECONDS=180`, younger than the last prep swap plus
`PREP_CONFIRM_GRACE_SECONDS=60`, or above `MAX_PREP_SWAPS_PER_MINT_PER_HOUR=3`.

### Idle Sweep (idle_sweep.py)

When `add_policy.enabled` and net idle (after committed opens/preps/
reservations) satisfies `min_add_usd <= idle < idle_max_usd` with a fresh
scan, emit one `add_liquidity` into the best tracked position: Y side must be
USDC, headroom `max_position_usd - tracked_value >= add`, pool passes
bin_step/TVL/volume gates when present, per-pool cooldown + daily cap
(`state/add_state.json`), `y_side_room_pct` headroom above the active bin,
reservation persisted atomically (`RESERVATION_MAX_AGE_SECONDS=3600`).

### Capital Input (capital.py)

No env-var override and no RPC lookup. `summarize_wallet` reads the newest
Missy wallet scan: `idle_usdc` (USDC mint), `dust_assets` (non-reserved,
non-USDC, `value > DUST_MIN_USD=1.0`; `RESERVED_MINTS` = SOL + owner hold),
`deployable = idle + dust_total`.

### USDC Balance Resolution Order

1. Newest Missy wallet scan under `--wallet-scans-dir` (`wallet_screen-latest.json`)
   via `capital.summarize_wallet` and `readiness.load_raw_wallet`
2. Fallback 0.0 → OPEN signals skipped

## Output Artifacts

| Artifact | Description |
|---|---|
| JSON report | Full scoring data via `--json` flag (incl. `capital_plan`, `strategies`, `funding`, `out_of_range_grace`, `idle_sweep`, `open_skipped`) |
| Human summary | Default stdout line, with per-verdict reasons |
| Signal JSON | Written to pending queue directory (`open` first, then dust/prep/close/claim, then sweep) |
| Daily markdown log | Appended to memory directory (YYYY-MM-DD.md) |
| State files | `state/out_of_range.json` (grace counters), `state/add_state.json` (sweep cooldowns/reservations) |

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Fatal error (including invalid profiles.json) |
| 2 | Stale/missing input data |
