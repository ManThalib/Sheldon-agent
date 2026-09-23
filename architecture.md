# Architecture

## Overview

Sheldon is a deterministic LP scoring engine that evaluates DeFi liquidity pool positions on Meteora DLMM. It consists of two main modules:

1. **`lp_scoring.py`** — Core scoring engine
2. **`run_cycle.py`** — Cycle runner that loads data, invokes scoring, and produces signals/logs

## Data Flow

1. **Input**: Newest `pool_scan-*.json` and `position_scan-*.json` files from configured directories
2. **Validation**: Freshness check (max 3900s default), malformed data handling
3. **Scoring**: Each pool gets a 0-100 score across 5 components; each position gets a 0-100 score across 4 components
4. **Verdicts**: Scores map to actionable verdicts (OPEN_CANDIDATE, HOLD, CLOSE, COLLECT_FEES, etc.)
5. **Output**: JSON report, human summary, optional George-schema signal files, daily markdown log

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
- `CLOSE` → `close` signal with bin range
- `COLLECT_FEES` → `claim_fees` signal
- `OPEN_CANDIDATE` (Meteora) → review item (not queued automatically)
- Non‑Meteora actions noted but not queued

## Output Artifacts

- JSON report via `--json` flag
- Human summary (default stdout)
- Daily markdown log in memory directory
- Signal JSON files in pending queue