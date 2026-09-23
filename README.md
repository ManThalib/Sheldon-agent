# Sheldon LP Scoring Engine

Deterministic LP opportunity and health scoring for Meteora DLMM, Raydium CLMM, and Orca Whirlpool positions.

## Usage

```bash
python3 lp_scoring.py [--pools-dir D] [--positions-dir D] [--json]
```

```bash
python3 run_cycle.py [--write-signals] [--signals-dir D] [--json] [--max-age-seconds N]
```

## Output

- **Human-readable summary**: top pool scores and open positions status
- **JSON report**: full scoring data with components and verdicts
- **Signal files**: George-schema `open`/`close`/`claim_fees` signals for supported DEXes
- **Daily markdown log**: appended to memory directory

## Environment Variables

| Variable | Description |
|---|---|
| `SHELDON_IDLE_USDC` | Idle USDC balance in USD (overrides cache and RPC lookup) |

## Scoring Model

- Pool LP Opportunity Score (0-100): fee_yield + turnover + depth + volatility_fit + bin_step_fit
- Position Health Score (0-100): range_status + fee_capture + il_risk + time_decay
- Verdicts: OPEN_CANDIDATE / WATCH / IGNORE / CLOSE / REVIEW / HOLD / COLLECT_FEES

## Supported DEXes

| DEX | Position index type | Notes |
|---|---|---|
| Meteora | `active_bin_id` | DLMM bins |
| Raydium | `current_tick` or `active_bin_id` | CLMM ticks |
| Orca | `current_tick` or `active_bin_id` | Whirlpool ticks |

## Configuration

Tunable weights and thresholds defined in `lp_scoring.py`:
- `POOL_WEIGHTS`: fee_yield(35), turnover(20), depth(15), volatility_fit(20), bin_step_fit(10)
- `POSITION_WEIGHTS`: range_status(35), fee_capture(25), il_risk(25), time_decay(15)
- Pool thresholds: open >= 70, watch 55-70
- Position thresholds: close < 40, review 40-60, hold >= 60

## Rail Constants (George defaults)

Defined in `run_cycle.py`:
- `MIN_POSITION_USD = 15.0` — minimum position value
- `DEFAULT_MAX_POSITION_USD = 100.0` — maximum position value
- `DEFAULT_MAX_RANGE_WIDTH = 200` — max range width in bins/ticks
- `DEFAULT_MAX_SLIPPAGE_BPS = 100` — max slippage in bps (1%)

## USDC Balance Resolution Order

1. `SHELDON_IDLE_USDC` environment variable
2. `/data/missy-data/wallet_balances.json` (Missy cache)
3. Live Solana RPC lookup via George's config
4. Fallback 0.0 (OPEN signals skipped when capital unknown)