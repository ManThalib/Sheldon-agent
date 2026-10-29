# Changelog

## 2026-10-29 — Missy pool_features v1: safety gate + net fee-yield ranker

Implements the consumer side of Missy's frozen `pool_features_version: 1`
contract. Both switches default **off**; Sheldon's live behaviour is unchanged
until the owner flips them.

### Added

- `pool_features.py` — pure v1 consumer:
  - `gate_v1()`: safety-only open gate (`safety.eligible` +
    `data_quality.range_ready`), with `reject_codes` / `thresholds_used` in the
    decision for attribution.
  - `compute_net_yield()`: expected net fee yield per day, fee-only APR,
    per-record reference width, concentration multiplier, variance drag,
    round-trip slippage amortised over `hold_days`, IL guard.
  - deterministic `rank_key()`: `(-net_daily_pct, pool_address)`.
- `sheldon_policy.json`:
  - `scoring.contract_version: 1`, `scoring.gate: "safety"`;
  - `pool_gate` block (`enabled: false`, `require_range_ready: true`,
    `v0_fallback: "legacy"`);
  - `pool_ranker` block (`enabled: false`, `hold_days: 3.0`,
    `variance_divisor: 800.0`, `max_il_pct: 5.0`, `high_net_daily_pct: 0.10`,
    `tie_break: "pool_address"`, etc.).
- `strategy_config.high_net_daily_pct: 0.10`; the v1 selector path uses net
  yield, the v0 path keeps `high_score_threshold`.
- `docs/sheldon_contract_v1.md` — shipped formula, config keys, v0 fallback,
  ranker caveats, Phase-B diff format.
- `docs/pool_features_v1_comparison_2026-10-29.md` — fixture gate diff,
  old-score vs net-yield top-10, three largest inversions.
- Tests: v0 regression, safety-only gate, no hidden score dependence,
  dual-model removal, adaptive re-tag removal, ranker monotonicity,
  determinism, rails on v1 records, null safety, Missy fixture.

### Changed

- `lp_scoring.score_pool_missy`: when `pool_gate.enabled` and the record is v1,
  the verdict is safety-based; `score`/`new_fc_score` stay in the report for
  audit but never gate. v0 and flag-off records keep the legacy score rail.
- `lp_scoring.run_cycle`: adaptive threshold calibration/re-tagging excludes
  v1 records when the gate is on (static pass/fail verdicts).
- `run_cycle/gates._filter_open_candidates`: v1 records bypass
  `min_open_score`, dual-model agreement/weighting and the legacy TVL/volume
  backstop; `range_ready` replaces local tick sniffing. All Sheldon rails
  (window/blackout, dedup, reservation, cooldown, bin-step) still run.
- `strategy/candidate_filtering.build_strategies`: ranks v1 records by
  `net_daily_pct` when `pool_ranker.enabled`; score ordering unchanged when
  off.
- `run_cycle/signals.py`: prep-sizing `score_ok` and open-signal building use
  the same v1 gate verdict; signal schema is untouched.
- `run_cycle.py`: rotation candidate pre-checks mirror the v1 predicate.
- `dynamic/thresholds.py` (and the deprecated `dynamic.py` mirror):
  `adaptive_pool_thresholds()` returns `None` while the v1 gate is enabled.
- `backtest/pool_replay.run_gate_report`: attributes v1 rejections to
  `safety.reject_codes`.

### Fixed

- `scoring/pool/score.py::score_pool_missy` no longer raises
  `ImportError: cannot import name 'load_scoring_policy' from 'scoring.config'`.
  The stale copy now routes to the live `lp_scoring` implementation.

### Deprecated / documented

- `scoring/pool/score.py` `score_pool_missy`, `lp_scplic.py`,
  `dynamic.py`, and `backtest.py` are marked as mirrors of their live
  package/module counterparts. Do not wire new code to them.

### Migration note (v0 → v1)

1. `scoring.contract_version: 0 → 1` documents that the consumer understands
   pool_features v1. It does **not** change behaviour by itself.
2. Behaviour changes only when `pool_gate.enabled` / `pool_ranker.enabled`
   become `true`.
3. Rollback = set both flags back to `false` (or keep the legacy branch behind
   `scoring.contract_version: 0`). v0 records never widen exposure.
4. Phase B is mandatory: run 24 hourly cycles with flags **off** but v1
   decisions computed/reported, and attribute each new candidate to a Missy
   reject code or the lowered floors before flipping.
