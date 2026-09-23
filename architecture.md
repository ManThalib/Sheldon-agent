# Architecture

## Overview

Sheldon is a deterministic LP scoring engine that evaluates DeFi liquidity pool positions on **Meteora DLMM**, **Raydium CLMM**, and **Orca Whirlpool**. It consists of two main modules:

1. **`lp_scoring.py`** — Core scoring engine
2. **`run_cycle.py`** — Cycle runner that loads data, invokes scoring, builds/caches signals, and logs results

## Data Flow

1. **Input**: Newest `pool_scan-*.json` and `position_scan-*.json` files from configured directories
2. **Validation**: Freshness check (max 3900s default), malformed data handling
3. **Scoring**: Each pool gets a 0-100 score across 5 components; each position gets a 0-100 score across 4 components. `score_pool` embeds the original `_pool` dict in its result; `verdicts` for OPEN_CANDIDATE actions include `_pool` for downstream use
4. **Verdicts**: Scores map to actionable verdicts (OPEN_CANDIDATE, HOLD, CLOSE, COLLECT_FEES, etc.)
5. **Signal building**: For supported DEXes, `CLOSE`/`COLLECT_FEES` produce direct signals; `OPEN_CANDIDATE` goes through `_build_open_signal` which resolves USDC balance, computes 50/50 token split, and derives bin/tick range
6. **Output**: JSON report, human summary, signal files written to pending queue, daily markdown log

## Scoring Components

### Pool Scores (sum to 0-100)

| Component | Weight | Description |
|---|---|---|
| `fee_yield` | 35 | Realized fee APR + fee/TVL ratio |
| `turnover` | 20 | Window volume / TVL |
| `depth` | 15 | TVL log-scale (deeper = safer) |
| `volatility_fit` | 20 | Triangular curve peaking at 8% volatility |
| `bin_step_fit` | 10 | Bin step vs volatility matching |

### Position Scores (sum to 0-100)

| Component | Weight | Description |
|---|---|---|
| `range_status` | 35 | Price inside range? |
| `fee_capture` | 25 | Fees earned vs expected |
| `il_risk` | 25 | Impermanent loss exposure |
| `time_decay` | 15 | Decay after 14-day horizon |

## Verdict Mapping

- **Pool**: >= 70 → OPEN_CANDIDATE, 55-70 → WATCH, < 55 → IGNORE
- **Position**: < 40 → CLOSE, 40-60 → REVIEW, >= 60 → HOLD
- **Collect fees**: HOLD position with earned fees >= $10

## Signal Generation

`run_cycle.py` converts verdicts into George-schema signal files:

| Verdict | Action | DEX Support | Notes |
|---|---|---|---|
| `CLOSE` | `close` | meteora, raydium, orca | Direct signal with bin range |
| `COLLECT_FEES` | `claim_fees` | meteora, raydium, orca | Direct signal with bin range |
| `HOLD` | — | — | No signal |
| `REVIEW` | — | — | No signal |
| `WATCH` | — | — | No signal |
| `IGNORE` | — | — | No signal |
| `OPEN_CANDIDATE` | `open` | meteora, raydium, orca | Full allocation logic via `_build_open_signal` |

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

Where `center` is `active_bin_id` for Meteora, `current_tick` (or `active_bin_id`) for Raydium/Orca.

### USDC Balance Resolution Order

1. `SHELDON_IDLE_USDC` environment variable
2. `/data/missy-data/wallet_balances.json` (Missy cache)
3. Live Solana RPC lookup via George's config (`agent.config.json`)
4. Fallback 0.0 → OPEN signals skipped

## Output Artifacts

| Artifact | Description |
|---|---|
| JSON report | Full scoring data via `--json` flag |
| Human summary | Default stdout line |
| Signal JSON | Written to pending queue directory |
| Daily markdown log | Appended to memory directory (YYYY-MM-DD.md) |

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Fatal error |
| 2 | Stale/missing input data |