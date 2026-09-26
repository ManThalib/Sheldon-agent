# Architecture

## Overview

Sheldon is a deterministic LP scoring engine that evaluates DeFi liquidity pool
positions on **Meteora DLMM**, **Raydium CLMM**, and **Orca Whirlpool**. It
consists of four modules:

1. **`lp_scoring.py`** — Core scoring engine (v2: config-driven, data-quality aware)
2. **`run_cycle.py`** — Cycle runner that loads data, invokes scoring, builds
   George-schema signals, and logs results
3. **`profiles.json`** — Externalized scoring configuration (weights,
   thresholds, constants). Missing file → built-in defaults; invalid file →
   fatal error (fail-closed).
4. **`test_lp_scoring.py`** — Stdlib unit tests (`python3 test_lp_scoring.py -v`)
5. **`backtest.py`** — Historical replay of pool/position verdicts over the
   Missy scan archive (see "Backtesting" below)

## Data Flow

1. **Input**: Newest `pool_scan-*.json` and `position_scan-*.json` files from
   configured directories
2. **Validation**: Freshness check (max-age, default 3900 s), malformed data
   handling, config validation at load time
3. **Scoring**: Each pool gets a 0-100 score across 4-5 weighted components;
   each position gets a 0-100 score plus a `data_quality` record listing
   components scored on missing/unusable inputs. `score_pool` embeds the
   original `_pool` dict; OPEN_CANDIDATE verdicts carry it for signal building
4. **Verdicts**: Scores map to verdicts. **Open/close separation**: a position
   verdict of CLOSE requires all components to be scored on known data — any
   unknown component caps the verdict at REVIEW. Policy CLOSE
   (off-universe/unknown pair) is exempt and always CLOSE.
5. **Signal building**: For supported DEXes, `CLOSE`/`COLLECT_FEES` produce
   direct signals; `OPEN_CANDIDATE` goes through `_build_open_signal`
6. **Output**: JSON report, human summary, signal files, daily markdown log

## Scoring Components

### Pool LP Opportunity Score (weights per pair class, sum to 100)

| Component | stable_stable | stable_bluechip | bluechip_bluechip | Description |
|---|---|---|---|---|
| `fee_yield` | 35 | 30 | 30 | Realized fee APR vs class APR cap |
| `turnover` | 15 | 20 | 20 | Window volume / TVL (5x window = max) |
| `depth` | 15 | 15 | 15 | TVL log-scale $50k-$5M |
| `volatility_fit` | 10 | 20 | 35 | Triangular curve peaking at class vol peak |
| `depeg_safety` | 25 | 15 | — | Stable-side distance from $1 |

### Position Health Score (sum to 100)

| Component | stable_stable | stable_bluechip | bluechip_bluechip | Description |
|---|---|---|---|---|
| `range_status` | 35 | 30 | 30 | In-range with edge-distance fade |
| `fee_capture` | 25 | 20 | 20 | fees_usd / expected_fees_usd (estimated if missing) |
| `depeg_exposure` | 25 | — | — | Stable peg health + single-sided penalty |
| `il_risk` | — | 30 | 30 | IL estimate (volatility × concentration) or reported |
| `staleness` | 15 | 20 | 20 | Grace 30d, erodes to 0 at 120d |

Each position result carries `data_quality.unknown_components` — the list of
components whose inputs were missing or unusable (e.g. Orca fee sentinel
`u64::MAX`, zero pool volatility).

## Verdict Mapping

- **Pool**: ≥ 70 → OPEN_CANDIDATE, ≥ 55 → WATCH, else IGNORE
- **Position**: < 40 → CLOSE, < 60 → REVIEW, ≥ 60 → HOLD
  - CLOSE **only** when every component was scored on known data
  - Any unknown component caps the verdict at REVIEW (missing data is a data
    problem, not a position problem)
  - Off-universe/unknown pair → CLOSE regardless of data (policy rule)
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

Run: `python3 backtest.py [--data-dir /data/missy-data] [--horizon N] [--json]`

## Signal Generation

`run_cycle.py` converts verdicts into George-schema signal files (unchanged in
v2): `CLOSE` → `close`, `COLLECT_FEES` → `claim_fees`, `OPEN_CANDIDATE` →
`open` via `_build_open_signal`. HOLD/REVIEW/WATCH/IGNORE produce no signal.

### OPEN Signal Allocation Logic

```
position_usd = min(idle_usdc * 0.25, DEFAULT_MAX_POSITION_USD)
               >= MIN_POSITION_USD required

half_usd = position_usd / 2
amount_x = int((half_usd / px_x) * 10^dec_x)
amount_y = int((half_usd / px_y) * 10^dec_y)

half_width = max(1, DEFAULT_MAX_RANGE_WIDTH // 2)
bin_range = [center - half_width, center + half_width]
```

Where `center` is `active_bin_id` for Meteora, tick for Raydium/Orca.

### USDC Balance Resolution Order

1. `SHELDON_IDLE_USDC` environment variable
2. `/data/missy-data/wallet_balances.json` (Missy cache)
3. Live Solana RPC lookup via George's config
4. Fallback 0.0 → OPEN signals skipped

## Output Artifacts

| Artifact | Description |
|---|---|
| JSON report | Full scoring data via `--json` flag |
| Human summary | Default stdout line, with per-verdict reasons |
| Signal JSON | Written to pending queue directory |
| Daily markdown log | Appended to memory directory (YYYY-MM-DD.md) |

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Fatal error (including invalid profiles.json) |
| 2 | Stale/missing input data |
