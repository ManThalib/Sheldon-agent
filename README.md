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

Universe policy: **stablecoin and high-cap pairs only** (`STABLECOINS`, `HIGH_CAPS` in
`lp_scoring.py`). Off-universe/unparseable pools are hard-gated to IGNORE; existing
positions in them get CLOSE.

Pairs are auto-classified (`stable_stable`, `stable_bluechip`, `bluechip_bluechip`)
and scored with class-specific profiles:

- Pool LP Opportunity Score (0-100):
  - stable_stable: fee_yield(35) + turnover(15) + depth(15) + depeg_safety(25) + volatility_fit(10)
  - stable_bluechip: fee_yield(30) + turnover(20) + depth(15) + volatility_fit(20) + depeg_safety(15)
  - bluechip_bluechip: fee_yield(30) + turnover(20) + depth(15) + volatility_fit(35)
- Position Health Score (0-100):
  - stable_stable: range_status(35, boundary-distance weighted) + fee_capture(25) + depeg_exposure(25) + staleness(15)
  - bluechip classes: range_status(30) + fee_capture(20) + il_risk(30) + staleness(20)
- Verdicts: OPEN_CANDIDATE / WATCH / IGNORE / CLOSE / REVIEW / HOLD / REBALANCE / COLLECT_FEES
- REBALANCE: position is CLOSE/REVIEW but its pool is an OPEN_CANDIDATE → close and re-range.
- Collect fees: HOLD with fees >= max($5, 1% of position value).

## Supported DEXes

| DEX | Position index type | Notes |
|---|---|---|
| Meteora | `active_bin_id` | DLMM bins |
| Raydium | `current_tick` or `active_bin_id` | CLMM ticks |
| Orca | `current_tick` or `active_bin_id` | Whirlpool ticks |

## Configuration

Tunables defined in `lp_scoring.py`:
- `STABLECOINS` / `HIGH_CAPS`: universe whitelist
- `POOL_PROFILES` / `POSITION_PROFILES`: per-pair-class weights, volatility peak, APR cap
- Pool thresholds: open >= 70, watch 55-70
- Position thresholds: close < 40, review 40-60, hold >= 60
- `DEPTH_MIN_USD` / `DEPTH_MAX_USD`, `DEPEG_ZERO_DIST`, `STALE_GRACE_DAYS` / `STALE_ZERO_DAYS`,
  `COLLECT_MIN_USD` / `COLLECT_PCT_OF_VALUE`

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