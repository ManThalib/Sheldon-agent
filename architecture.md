# Architecture

## Overview

Sheldon is a deterministic LP scoring engine that evaluates DeFi liquidity pool
positions on **Meteora DLMM**, **Raydium CLMM**, and **Orca Whirlpool**. It
consists of these modules:

1. **`lp_scoring.py`** — Core scoring engine (config-driven, data-quality aware)
2. **`dynamic/`** — Modular dynamic calibration layer:
   - `calibration.py` — percentiles, regime detection, adaptive thresholds, expected-PnL verdicts
   - `helpers.py` — percentile ranking and norms building
   - `regime.py` — market regime classification and weight adjustment
   - `thresholds.py` — adaptive pool OPEN/WATCH cut-offs from quantiles
3. **`run_cycle/`** — Modular cycle runner:
   - `gates.py` — trading window and open-candidate filtering
   - `signals.py` — George-signal building and writing
   - `report.py` — human-readable logs and summaries
   - `__init__.py` — backward-compatible wrappers matching original `run_cycle.py` API
4. **`strategy/`** — Position sizing and volatility-adaptive ranges, loaded from
   `sheldon_policy.json` + George's `execution_limits.json`
5. **`capital.py`** — Wallet scan loader (`summarize_wallet`: idle USDC, dust, deployable)
6. **`readiness/`** — Modular capital readiness:
   - `__init__.py` — package re-exports
   - `wallet.py` — raw wallet scan loading
   - `funding.py` — funding plan builder
   - `prep_swap_gates.py` — swap gating (rescan wait + hourly loop guards)
   - `prep_swap.py` — build George-schema swap signals
7. **`range_state.py`** — Out-of-range grace (`state/out_of_range.json`,
   `ALLOWED_OUT_OF_RANGE_RUNS=2`)
8. **`idle_sweep.py`** — Idle-capital sweep into the best tracked position
   (`add_liquidity`, `state/add_state.json`)
9. **`tuner.py`** — Out-of-sample weight search over historical PnL (writes
   `profiles.tuned.json`, `--apply` with backup)
10. **`profiles.json`** — Externalized scoring configuration (weights,
    thresholds, constants, dynamic knobs). Missing file → built-in defaults;
    invalid file → fatal error (fail-closed).
11. **`sheldon_policy.json`** — Strategy policy (pool eligibility, sizing,
    windows, slippage, add_policy)
12. **`backtest.py`** — Historical replay of pool/position verdicts over the
    Missy scan archive with synthetic PnL + robustness stats
13. **`models.py`** — Legacy, currently unused (nothing imports them; scoring
    returns plain dicts)
14. **`lp_scoring.py`** — Main entry point, also re-exports key names for backward compatibility

Tests: `python3 -m unittest discover` or individually:
  `test_lp_scoring.py`, `test_dynamic.py`, `test_run_cycle.py`,
  `test_idle_sweep.py`, `test_range_state.py` (stdlib unittest).

## Data Flow

1. **Input**: Newest `pool_scan-*.json`, `position_scan-*.json`, and
   `wallet_screen-*.json` files from configured directories
2. **Validation**: Freshness check (max-age, default 3900 s), malformed data
   handling, config validation at load time
3. **Dynamic calibration**: `dynamic.py` builds a context from prior scans
   (rolling percentiles, regime, adaptive thresholds) and passes it into scoring
4. **Scoring**: Each pool gets a 0-100 score across 4-5 weighted components;
   each position gets a 0-100 score plus a `data_quality` record listing
   components scored on missing/unusable inputs. `score_pool` embeds the
   original `_pool` dict; OPEN_CANDIDATE verdicts carry it for signal building
5. **Verdicts**: Scores map to verdicts. **Open/close separation**: a position
   verdict of CLOSE requires all components to be scored on known data — any
   unknown component caps the verdict at REVIEW. Policy CLOSE
   (off-universe/unknown pair) is exempt and always CLOSE. With enough data,
   expected-PnL can override the score-based verdict.
6. **Out-of-range grace**: `range_state.py` counts consecutive out-of-range runs
   per pool (`state/out_of_range.json`). Run 1 downgrades CLOSE/REBALANCE to
   HOLD; run 2 lets it through; back-in-range resets.
7. **Open filtering + strategies**: `_filter_open_candidates` drops dedup/
   duplicate/center-unknown/out-of-window/policy-gated/bin-step-violating
   candidates; `strategy.build_strategies` ranks survivors by score (top
   `max_opens_per_cycle=3`) with volatility-adaptive ranges and 75%-of-
   deployable sizing.
8. **Funding + readiness**: `readiness.plan_funding` diffs strategy token needs
   against the raw wallet scan; `gate_prep_swaps` blocks rescan-races and
   hourly loops. Unfunded strategies yield prep `swap` signals, not opens.
9. **Signal building**: `write_signals` emits opens first, then dust
   `swap_to_usdc`, prep swaps, and closes/claims; `idle_sweep.plan_sweep`
   sweeps leftover sub-floor idle into the best tracked position.
10. **Output**: JSON report, human summary, signal files, daily markdown log,
   and a `dynamic` block showing the active regime and thresholds

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

## Dynamic Layers

| Layer | Module | Behaviour |
|---|---|---|
| 1. Rolling percentiles | `dynamic.py` | `fee_yield` and `turnover` are scored as a percentile of the same pair class over the trailing window, blended with the absolute score (`blend_alpha`) |
| 2. Regime detection | `dynamic.py` | Classifies market as `neutral`/`low_vol`/`high_vol`/`fee_boom`/`cracked_peg` and shifts component weights multiplicatively |
| 3. Adaptive thresholds | `dynamic.py` | `OPEN`/`WATCH` cut-offs are quantiles of the scored universe (floor/ceil clamps keep them sane) |
| 4. Expected-PnL verdicts | `dynamic.py` | Compares HOLD vs CLOSE vs REBALANCE expected value over a short horizon; only overrides when inputs are complete and the margin is decisive |
| 5. Weight tuner | `tuner.py` | Random search over weight perturbations, validated on the last 20% of scan history, writes `profiles.tuned.json` |

## Verdict Mapping

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
