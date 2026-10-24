# Sheldon Gate Improvement Summary

All 9 improvements have been implemented and verified. The system passes all existing tests (75+ tests across test_range_state, test_dynamic, test_close_emit_guards, etc.).

## Improvements Implemented

### 1. Profile Versioning
- **profiles.json**: Added `"version": 2` for A/B testing and rollback audit
- **lp_scoring.py**: Added `scoring_profile_version()` function
- **run_cycle.py**: Added `report["scoring_profile_version"]` field

### 2. Data Quality Warnings
- **lp_scoring.py**: `_UNKNOWN_WARNING_THRESHOLD = 3`, `_flag_data_quality()` function
- Adds `| data quality: N/M components unknown` to position reason string
- **render_summary()**: Surfaces warnings in human-readable output

### 3. Side-Aware OOR Grace Counters
- **range_state.py**: Already implemented - `update_out_of_range_state()` resets counter when side changes
- Test: `test_side_change_restarts_counter` PASSED

### 4. Calibrate Floor/Ceil Thresholds
- **profiles.json**: `dynamic.thresholds.floor_open`, `ceil_open`, `floor_watch`, `ceil_watch` already configured
- `dynamic.adaptive_pool_thresholds()` reads these with fallback defaults
- Operators adjust via profiles.json only

### 5. Make PnL Horizon Configurable
- **profiles.json**: `dynamic.position_pnl.horizon_days: 3.0` already set
- `dynamic.py` `expected_pnl_verdict()` reads from `pnl_cfg.get("horizon_days", 1.0)`
- Default 1.0 maintained for backward compatibility

### 6. Graduated Wallet Freshness Penalties
- **readiness/readiness_gate.py**: Added `freshness_factor(mtime, max_age, now)` → float [0.0, 1.0]
- **run_cycle.py**: Applies factor to reduce `suggested_usdc` proportionally; hard block at max_age still enforced

### 7. Pre-Screen Rotation Candidates
- **run_cycle.py**: Policy gate pre-screening in rotation detection
- Checks: min_pool_liquidity, allowed_bin_steps, max_turnover_ratio, max_volatility_pct, turnover ratio
- Skips rotation if candidate fails any gate

### 8. Improve Idle Sweep Headroom
- **idle_sweep.py**: Range utilization penalty in `select_add_candidate()`
- `effective_headroom = headroom * (1 - 0.5 * utilization)`, clipped [0.2, 0.8]
- `plan_sweep()` uses `effective_headroom_usd` for add amount cap

### 9. Add TTL to Close Settlement State
- **sheldon_policy.json**: `close_state_ttl_seconds: 3600` (1 hour, configurable)
- **run_cycle.py**: Passes TTL to `check_settlement()`
- **close_settlement.py**: `advance()` uses `close_ttl_seconds` at the two emission-timeout points, replacing hardcoded `UNKNOWN_TTL_SECONDS = 7 days`

---