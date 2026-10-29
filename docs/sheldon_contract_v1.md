# Sheldon ↔ Missy pool_features v1 contract

**Consumer:** Sheldon (`pool_features.py`, `lp_scoring.py`, `run_cycle/gates.py`,
`strategy/candidate_filtering.py`)
**Producer:** Missy (`pool_features_version = 1`)
**Upstream spec:** `/Users/manthalib/Missy-agent/docs/missy_sheldon_contract_v1.md`
**Status:** implemented behind two flags (both default `false`).

Sheldon answers one question for v1 records: *which safety-surviving pool earns
the most per dollar?* Missy answers *is this pool real, safe and data-complete?*
Score is audit-only on the v1 path.

---

## 1. Gate predicate (`pool_gate.enabled = true`)

```python
safety = pool["safety"]
if not safety["eligible"]:
    skip(pool, safety["reject_codes"])
if require_range_ready and not pool["data_quality"]["range_ready"]:
    skip(pool, "no live tick/bin")
```

Removed from the v1 open path:

- `scoring.min_open_score` / `pool_eligibility.min_open_score`;
- dual-model agreement and weighted combination (`new_fc_score`,
  `new_fc_verdict`, `use_weighted_combination`);
- adaptive-threshold re-tagging (`dynamic.adaptive_pool_thresholds`);
- the legacy TVL/volume backstop;
- the prep-sizing `score_ok` re-check;
- the duplicate rotation policy re-check (mirrors the same v1 predicate).

Kept for audit/backtest but never used to gate or rank v1 records: `score`,
`new_fc_score`, `opportunity_score`, `safety_score`, and every legacy field.

Sheldon rails are unchanged and still run before/alongside the gate: trading
windows/blackouts, dedup/already-holding, rotation reservations, reopen
cooldown, Meteora bin-step rail, sizing, capital plan, readiness, close
settlement, funding/prep-swap, position scoring.

---

## 2. Ranker (`pool_ranker.enabled = true`)

`net_daily_pct` is expected **net fee yield per day, in percent**:

```
apr_daily_pct   = pool["fee_capture"]["realized_fee_apr"] / 365.0
in_range        = pool["expected_in_range_frac"]
vol             = pool["expected_in_range"]["vol_daily_pct"]
ref_width       = pool["expected_in_range"]["reference_half_width_pct"]
deployed_width  = the half-width Sheldon will actually open (percent/side)
conc            = clamp(deployed_width / ref_width, 0.25, 4.0)
slip_bps        = pool["slippage_estimate_bps"][str(size_usd)]

gross_daily_pct    = apr_daily_pct * in_range
variance_drag_pct  = conc * (vol ** 2) / variance_divisor
slippage_drag_pct  = (2.0 * slip_bps / 100.0) / hold_days
net_daily_pct      = gross_daily_pct - variance_drag_pct - slippage_drag_pct
```

Sort key: `(-net_daily_pct, pool_address)`. Stable and clock-free.

- `realized_fee_apr` is fee-only (rewards excluded).
- `reference_half_width_pct` is read **per record**; no constant width is
  assumed.
- `deployed_width` comes from Sheldon's own range policy
  (`strategy.range.adaptive_half_width`) converted to a price fraction, so
  `conc = 1.0` when Sheldon and Missy agree on the band.
- `il_estimate_pct` is a **guard**, not a subtractive term. Above
  `max_il_pct` the record is skipped (`il_policy: "skip"`) or demoted
  (`"demote"`); the variance term already prices range risk.
- Null `expected_in_range_frac` / `vol_daily_pct` / `reference_half_width_pct`
  makes the record unrankable: it ranks last (`unknown_rank: "last"`) or is
  dropped (`"skip"`) — never treated as zero risk.
- Null slippage table values fall back to
  `1e4 * size_usd / slippage_model.depth_proxy_usd`; if that is also null the
  term is zero but `slippage_known: false` is logged.
- `opportunity_score` is never used for ranking (gross APR ordinal, no depth,
  no net yield).

`strategy/sheldon_selector.py` now uses `net_daily_pct` against
`strategy_config.high_net_daily_pct` (default `0.10` %/day) for Curve/BidAsk
on the v1 path. The legacy v0 path still uses `high_score_threshold` with a
score.

### Ranker caveats

- **Survival vs time-in-range.** Missy's `expected_in_range_frac` is
  P(staying in band over `horizon_days`), a *conservative lower bound* on
  expected time-in-range for a position that exits on OOR. If calibration shows
  it is too harsh, request Missy v1.1 `expected_time_in_range_frac`
  (additive, per-record) and switch the multiplier. Do **not** re-implement
  Missy's barrier model inside Sheldon.
- **Edge IL.** `il_estimate_pct` is divergence loss at the band edge (worst
  case inside the range). The variance term already prices range risk, so it
  is a cap, not a second subtraction.
- **Screening slippage.** Missy's table is a TVL/volatility depth proxy, not
  an execution quote. George re-reads state before signing.
- **Unknown is not zero.** `pool_age_days`, `tvl_trend_24h_pct`, `lp_lock`,
  `owner_concentration_pct` (always null in v1) and null slippage values are
  unknown, never fabricated zeros.
- **`complete` is not the gate.** `data_quality.complete` includes
  `range_ready`; without RPC enrichment many economically sound records are
  `complete: false`. The gate uses `range_ready` only.
- **Audit.** `safety.thresholds_used` is logged with each gate decision so a
  new candidate can be attributed to a Missy reject code or to the lowered
  floors, not to a Sheldon bug.

---

## 3. Policy keys

`sheldon_policy.json`:

```jsonc
"scoring": {
  "source": "missy",
  "contract_version": 1,          // 0 = legacy score-gate branch
  "gate": "safety",
  "min_open_score": 90.0,         // legacy/v0 only
  "position_source": "missy",
  "position_features_version": 1
},
"pool_gate": {
  "version": 1,
  "enabled": false,               // Phase A; flip to true in Phase C
  "require_range_ready": true,
  "v0_fallback": "legacy"
},
"pool_ranker": {
  "version": 1,
  "enabled": false,               // Phase A; flip to true in Phase C
  "hold_days": 3.0,
  "variance_divisor": 800.0,
  "max_il_pct": 5.0,
  "il_policy": "skip",            // skip | demote
  "slippage": { "round_trip": true, "size_usd": 100 },
  "high_net_daily_pct": 0.10,
  "tie_break": "pool_address",
  "concentration_min": 0.25,
  "concentration_max": 4.0,
  "unknown_rank": "last",         // last | skip
  "min_net_daily_pct": 0.0,
  "enforce_min_net": false        // owner opt-in; prefer ranking survivors
}
```

`strategy_config.high_net_daily_pct` (default `0.10`) replaces
`high_score_threshold` on the v1 selector path. `profiles.json` carries an
audit-only `pool_features` block; `sheldon_policy.json` remains the single
source for the live switches.

**Rollout rule:** `enabled: true` is the only change needed. With both flags
`false`, Sheldon's live behaviour is identical to the pre-v1 build.

---

## 4. v0 fallback

A record without `pool_features_version` (or with version `< 1`) is v0:

- `score >= scoring.min_open_score` (90) and Missy `eligible != false`;
- legacy TVL/volume backstop when `eligible` is absent;
- dual-model and adaptive-threshold rails exactly as before;
- `build_strategies` sorts by `score`;
- selector uses `high_score_threshold`.

An old scan can therefore never widen exposure. Mixed files are read
per-record.

---

## 5. Phase-B shadow report format

While `pool_gate.enabled` / `pool_ranker.enabled` are still false, compute the
v1 decisions without changing emitted signals and attach one diff per cycle.
`pool_features.shadow_report(pools, min_open_score=90.0)` returns exactly this
shape (deterministic, no clock):

```jsonc
"pool_features_shadow": {
  "contract_version": 1,
  "gate": {
    "v1_records": 67,
    "legacy_open": 11,
    "safety_eligible": 26,
    "safety_ready": 6,
    "kept": ["<pool_address>", ...],
    "skipped": [
      {"pool_address": "...", "reject_codes": ["low_tvl"], "reason": "..."}
    ],
    "flips_in":  [{"pool_address": "...", "from": "score", "why": "low_tvl_floor_lowered | score_bypassed"}],
    "flips_out": [{"pool_address": "...", "reject_codes": [], "why": "range_ready=false"}]
  },
  "ranker": {
    "top_by_old_score": ["..."],
    "top_by_net_daily_pct": ["..."],
    "inversions": [{"pool_address": "...", "score_rank": 11, "net_rank": 29,
                    "inputs": {"realized_fee_apr": 37.48, "in_range": 0.4816,
                               "vol_daily_pct": 10.2286,
                               "reference_half_width_pct": 5.1163,
                               "deployed_half_width_pct": 5.1163,
                               "concentration": 1.0, "slippage_bps": 0.03}}]
  }
}
```

Exit criteria for Phase B: 24 hourly cycles, no crash, and the diff attached
to every cycle. Phase C flips both flags and keeps the legacy branch behind
`scoring.contract_version: 0`.

---

## 6. Deliberate non-goals

No Missy changes, no George/execution-rail changes, no position scoring or
`position_features` changes, no new dependency, no ranking on
`opportunity_score`, and no re-implementation of Missy's in-range/IL/slippage
models inside Sheldon.
