# Sheldon LP Scoring Engine

Deterministic LP opportunity and health scoring for Meteora DLMM, Raydium CLMM, and Orca Whirlpool positions.

## Usage

```bash
python3 lp_scoring.py [--pools-dir D] [--positions-dir D] [--wallet-scans-dir D] [--json]
```

```bash
python3 run_cycle.py [--pools-dir D] [--positions-dir D] [--wallet-scans-dir D] \
  [--write-signals] [--signals-dir D] [--memory-dir D] [--state-dir D] \
  [--json] [--max-age-seconds N]
```

```bash
python3 backtest.py [--data-dir /data/missy-data] [--pools-dir D] [--positions-dir D] \
  [--horizon N] [--width N] [--position-value-usd V] \
  [--entry-cost-bps N] [--exit-cost-bps N] [--claim-cost-usd V] \
  [--bootstrap-samples N] [--seed N] [--dynamic] [--json] [--detail]
```

```bash
python3 tuner.py [--data-dir D] [--pools-dir D] [--horizon N] [--width N] \
  [--candidates N] [--seed N] [--train-frac F] [--output F] [--apply]
```

## Modules

- `lp_scoring.py` — core deterministic scoring engine (config-driven, data-quality aware)
- `run_cycle/` — modular cycle runner split into focused sub-modules:
  - `gates.py` — trading window and open-candidate filtering
  - `signals.py` — George-signal building and writing
  - `report.py` — human-readable logs and summaries
- `dynamic/` — modular dynamic calibration:
  - `__init__.py` — package re-exports
  - `calibration.py` — context assembly, norms, expected-PnL verdicts
  - `helpers.py` — percentile ranking and norms building
  - `regime.py` — market regime classification and weight adjustment
  - `thresholds.py` — adaptive pool OPEN/WATCH cut-offs from quantiles
- `readiness/` — modular capital readiness:
  - `__init__.py` — package re-exports
  - `wallet.py` — raw wallet scan loading
  - `funding.py` — funding plan builder
  - `prep_swap_gates.py` — swap gating (rescan wait + hourly loop guards)
  - `prep_swap.py` — build George-schema swap signals
- `backtest/` — historical replay with synthetic PnL:
  - `__init__.py` — package re-exports
  - `pool_replay.py` — pool scan replay
  - `position_replay.py` — position scan replay
  - `reports.py` — replay reports
  - `synthetic_pnl.py` — synthetic PnL computation
- `strategy/` — sizing and adaptive ranges from `sheldon_policy.json`
- `capital.py` — wallet scan loader: idle USDC, dust assets
- `range_state.py` — out-of-range grace: CLOSE/REBALANCE deferred 1 run
- `idle_sweep.py` — idle-capital sweep into best tracked position
- `tuner.py` — out-of-sample weight search over historical PnL
- `profiles.json` / `profiles.tuned.json` — scoring profiles and tuned outputs
- `sheldon_policy.json` — strategy policy: pool eligibility, sizing, windows, slippage, add_policy
- `models.py` — legacy/unused: nothing imports them (scoring returns plain dicts; config lives in `profiles.json` via `lp_scoring.DEFAULT_CONFIG`)

## Output

- **Human-readable summary**: top pool scores and open positions status
- **JSON report**: full scoring data with components and verdicts, plus `capital_plan`, `strategies`, `funding`, `out_of_range_grace`, `idle_sweep`
- **Signal files**: George-schema `open`/`close`/`claim_fees` signals, plus `swap` (dust to USDC, prep buys/sells) and `add_liquidity` sweep signals for supported DEXes
- **Daily markdown log**: appended to memory directory

## Wallet Input (replaces SHELDON_IDLE_USDC)

Capital comes from Missy's newest wallet scan (`wallet_screen-*.json` /
`wallet_screen-latest.json` under `--wallet-scans-dir`, default
`/data/missy-data/wallet_screens`) via `capital.summarize_wallet` /
`readiness.load_raw_wallet`. There is no `SHELDON_IDLE_USDC` env var and no
RPC lookup: unknown/missing scan means idle 0 and OPEN signals are skipped.

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

Scoring tunables live in `profiles.json` (missing file → built-in
`lp_scoring.DEFAULT_CONFIG`; invalid file → fatal `ConfigError`, exit 1).
Strategy rails live in `sheldon_policy.json` (see below). `tuner.py --apply`
overwrites `profiles.json` with the best candidate (backup
`profiles.json.bak.<ts>`); the raw search output stays in `profiles.tuned.json`.

Scoring (in `profiles.json` / `lp_scoring.py`):
- `STABLECOINS` / `HIGH_CAPS`: universe whitelist
- `POOL_PROFILES` / `POSITION_PROFILES`: per-pair-class weights, volatility peak, APR cap
- Pool thresholds: open >= 70, watch 55-70 (adaptive mode: quantiles of the
  scored universe, floored by policy `min_open_score`)
- Position thresholds: close < 40, review 40-60, hold >= 60
- `DEPTH_MIN_USD` / `DEPTH_MAX_USD`, `DEPEG_ZERO_DIST`, `STALE_GRACE_DAYS` / `STALE_ZERO_DAYS`,
  `COLLECT_MIN_USD` / `COLLECT_PCT_OF_VALUE`

Note: `profiles.json` also carries `constants.il_vol_window_days` (currently
unread by code) and `dynamic.percentile.min_samples=10` (code default is 20
when no config is loaded). `profiles.py` / `models.py` are unused legacy
modules.

## Policy Rails (`sheldon_policy.json`, via `strategy.py`)

- `position_sizing`: `min_position_usd=20`, `default_max_position_usd=100`, `max_opens_per_cycle=3`
- `pool_eligibility`: `min_open_score=70`, `min_pool_liquidity_usd=250k`,
  `min_24h_volume_usd=1M`, `allowed_bin_steps=[10,20,25,50,100]`,
  `min_fee_tvl_ratio=0.05`, `max_volatility_pct=50`, `max_turnover_ratio=50`
- `windows`: `open_window_utc` / `close_window_utc` (`00:00-23:59`),
  `blackout_dates`, timezone `Asia/Shanghai`
- `execution_intent`: `default_max_slippage_bps=100`
- `add_policy`: `enabled`, `idle_max_usd=20`, `min_add_usd=5`,
  `cooldown_hours=6`, `max_adds_per_day=6`, `y_side_room_pct=25`,
  `max_wallet_scan_age_seconds=900`
- Sizing: `suggested = min(deployable * 0.75, max_position_usd)`; open gate is
  `idle >= min` and `suggested >= min`
- Ranges are volatility-adaptive (`WIDTH_FACTOR=0.5`, `MIN_HALF_WIDTH=10`,
  `MAX_HALF_WIDTH=1000`; Meteora capped at George's `max_meteora_range_width=70`
  bins inclusive → half-width 34). There is no fixed `DEFAULT_MAX_RANGE_WIDTH=200`.
- Universe note: scoring enforcement uses `lp_scoring.STABLECOINS/HIGH_CAPS`
  (`USDC,USDT,PYUSD,USDG` + `SOL,WSOL,WBTC,CBBTC,WETH,ETH,JSOL,MSOL,BSOL,ZEC`);
  `sheldon_policy.json:universe` lists a wider set — the scoring lists win.

## Capital / Readiness Rails

- `capital.py`: `USDC_MINT`, `SOL_MINT`, `RESERVED_MINTS` (SOL + owner hold),
  `DUST_MIN_USD=1.0`; `deployable = idle_usdc + dust_total`
- `readiness.py`: `SOL_RESERVE_LAMPORTS=20M (0.02 SOL)`, `BUY_BUFFER_PCT=2.0`,
  `MIN_PREP_SWAP_USD=1.0`, `SELL_SURPLUS_MIN_USD=2.0`,
  `MAX_PREP_SWAPS_PER_MINT_PER_HOUR=3`, `PREP_CONFIRM_GRACE_SECONDS=60`,
  `MAX_WALLET_SCAN_AGE_SECONDS=180`
- `range_state.py`: `ALLOWED_OUT_OF_RANGE_RUNS=2` (run 1 downgrades
  CLOSE/REBALANCE to HOLD, run 2 lets it through)
- `idle_sweep.py`: `RESERVATION_MAX_AGE_SECONDS=3600`, floor-score epsilon 0.01
  below `min_open_score` so opens always outrank sweeps

## USDC Balance Resolution Order

1. Newest Missy wallet scan under `--wallet-scans-dir` (`wallet_screen-latest.json`)
   via `capital.summarize_wallet` (idle USDC + dust) and `readiness.load_raw_wallet`
   (per-token balances for prep swaps)
2. Fallback 0.0 (OPEN signals skipped when capital unknown)

Tests: `python3 -m unittest test_lp_scoring test_run_cycle test_dynamic test_idle_sweep test_range_state`