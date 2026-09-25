# Sheldon Scoring Engine Refactor Prompt

Self-contained handoff prompt for the next session.

## Goal

Refactor the Sheldon LP scoring engine located at `/data/.openclaw/workspace-agents/sheldon/scoring/` without changing the current scoring weights/thresholds and without building a backtesting framework yet.

## Context files

- `/data/.openclaw/workspace-agents/sheldon/scoring/lp_scoring.py` — core scoring engine
- `/data/.openclaw/workspace-agents/sheldon/scoring/run_cycle.py` — cycle runner
- `/data/.openclaw/workspace-agents/sheldon/scoring/architecture.md`
- `/data/.openclaw/workspace-agents/sheldon/scoring/documentation.md`
- `/data/.openclaw/workspace-agents/sheldon/scoring/README.md`

Latest Missy scan paths for verification:

- `/data/missy-data/pool_screens/pool_scan-20260925-170529.json`
- `/data/missy-data/position_scans/position_scan-20260925-171118.json`

## Tasks to implement

1. **Elaborate / document the current scoring profile** in `architecture.md` and `documentation.md`. Describe:
   - Pair classes (`stable_stable`, `stable_bluechip`, `bluechip_bluechip`, `off_universe`, `unknown`)
   - Pool score components and weights
   - Position health score components and weights
   - Thresholds (`POOL_THRESHOLDS`, `POSITION_THRESHOLDS`)
   - Universe policy constants (`STABLECOINS`, `HIGH_CAPS`)
   - Do **not** change any weights, thresholds, or constants.

2. **Keep current scoring profiles unchanged**. Do not move profiles to a config file yet, do not add dynamic thresholds, do not add risk factors (#2 and #4 from the original suggestion list are deferred). Hardcoded constants stay as-is.

3. **Separate "open" and "close" logic more cleanly**:
   - Missing/unknown data must **not** produce a `CLOSE` verdict for positions. Instead produce `REVIEW` or `UNKNOWN`.
   - Specifically update:
     - `score_position_range_status`: missing bounds → `REVIEW` path, not 0-score close.
     - `score_position_fee_capture`: when `expected_fees_usd` is missing, estimate expected fees from pool-level fee yield × position value × days open, or mark the component as unavailable rather than always defaulting to 30 %.
     - `score_position_il_risk`: missing `il_estimate_pct` → `REVIEW` instead of fail-closed 40 %.
     - `score_position_staleness`: missing `days_open` → `REVIEW` instead of default 50 %.
   - Add a new top-level position verdict `REVIEW` and make sure `run_cycle.py` handles it (no signal written for `REVIEW`; log it).
   - Preserve existing `CLOSE` behavior only when the score legitimately falls below threshold with good data.

4. **Improve position scoring inputs**:
   - Add `estimate_expected_fees(pos, pool)` helper that estimates expected fees using:
     - pool `realized_fee_apr`
     - position `current_value_usd`
     - `days_open`
     - fallback to 0 if data is missing.
   - Add a flag `fee_data_unreliable` when raw Orca fees decode to sentinel values (`>= u64::MAX-1`, `>= u32::MAX-1`, or exactly `18446744073709551615`). When unreliable, set `fees_usd = 0` and mark `collect_fees = False`; do not use fees for `fee_capture` scoring.
   - Add `estimate_position_value(pos, pool)` that falls back to a liquidity-based approximation only if `current_value_usd` is missing or zero, but never overwrites a nonzero Missy value.
   - Add a `data_quality` field to position score output listing missing/unreliable fields.

5. **Code-level improvements**:
   - Add a `profiles.py` (or `config.py`) module that exports constants only — profiles, thresholds, universe lists, scoring constants. Keep `lp_scoring.py` using them via import.
   - Add dataclasses `PoolScore` and `PositionScore` in a new `models.py` with typed fields and a `.to_dict()` method. Refactor `score_pool` and `score_position` to return these dataclasses.
   - Add `Reason` helper to build a human-readable `reason` string per verdict (e.g. `"volatility 14.1% > peak 10.0%"`).
   - Add import-time assertion that each profile's weights sum to 100.00 within 0.01 tolerance.
   - Refactor `run_cycle()` so input validation (freshness, file existence, JSON parsing) is in a separate `validate_inputs()` function. `score_pool`/`score_position` should not perform freshness checks.
   - Add `MISSING_DEFAULT` constants per component and document why each is chosen, or centralize them.
   - Add logging / explainability: each score result must include `reason` and `data_quality` fields.
   - Ensure all new functions are pure (no global state mutation).

## What NOT to build

- Do **not** create a backtesting framework.
- Do **not** add dynamic thresholds, adaptive scoring, or new risk factors (vol-of-vol, trend, drawdown, etc.).
- Do **not** change current profile weights, thresholds, or universe lists.

## Verification steps

1. `python3 -m py_compile lp_scoring.py run_cycle.py`
2. `python3 -m pytest` if tests exist; otherwise run:
   - `python3 lp_scoring.py --json > /tmp/report.json`
   - Confirm no exceptions.
   - Confirm pool `5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6` scores ~69 and verdict `WATCH`.
   - Confirm Orca position `ENy8aX1tgb8tkbMvy2QLJk8ZeoBhtSooDsraTgKpUFeU` scores ~68 and verdict `HOLD`.
   - Confirm position score outputs contain `reason` and `data_quality` fields.
3. `python3 run_cycle.py --json` completes with exit 0 and no stale-data failures.

## Deliverables

- Updated `lp_scoring.py`, `run_cycle.py`
- New `profiles.py` and `models.py` in `/data/.openclaw/workspace-agents/sheldon/scoring/`
- Updated `architecture.md` and `documentation.md`
- Brief summary of changes
