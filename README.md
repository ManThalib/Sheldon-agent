# Sheldon LP Scoring Engine

Deterministic LP opportunity and health scoring for Meteora DLMM positions.

## Usage

```bash
python3 lp_scoring.py [--pools-dir D] [--positions-dir D] [--json]
```

```bash
python3 run_cycle.py [--write-signals] [--signals-dir D] [--json]
```

## Output

- **Human-readable summary**: top pool scores and open positions status
- **JSON report**: full scoring data with components and verdicts
- **Signal files**: George-schema close/claim_fees signals for Meteora positions

## Scoring Model

- Pool LP Opportunity Score (0-100): fee_yield + turnover + depth + volatility_fit + bin_step_fit
- Position Health Score (0-100): range_status + fee_capture + il_risk + time_decay
- Verdicts: OPEN_CANDIDATE / WATCH / IGNORE / CLOSE / REVIEW / HOLD / COLLECT_FEES

## Configuration

Tunable weights and thresholds defined in `lp_scoring.py`:
- `POOL_WEIGHTS`: fee_yield(35), turnover(20), depth(15), volatility_fit(20), bin_step_fit(10)
- `POSITION_WEIGHTS`: range_status(35), fee_capture(25), il_risk(25), time_decay(15)
- Pool thresholds: open >= 70, watch 55-70
- Position thresholds: close < 40, review 40-60, hold >= 60