# RFNE Model: Range-Fee-Net-Efficiency Position Scoring

## Table of Contents

- [Overview](#overview)
- [Key Philosophical Shifts](#key-philosophical-shifts)
- [Component Changes](#component-changes)
- [Implementation Timeline](#implementation-timeline)
- [Test Considerations](#test-considerations)
- [Migration Path for Existing Positions](#migration-path-for-existing-positions)



## Overview
Replaces the existing position scoring model with one that focuses on **fee capturing efficiency** and **range safety**, replacing staleness penalties with range efficiency rewards, and treating IL through net yield coverage rather than pure penalties.

## Key Philosophical Shifts

| Old Assumption | New Assumption |
|---|---|
| Staleness = age penalty (older = worse) | Staleness → Range efficiency (how long stayed in-range) |
| IL = pure penalty (always subtracts from score) | IL → Net yield coverage (fees - IL; profitable positions score above baseline) |
| Range edge proximity = safety (closer to edge = lower score but still rewarded) | Range centering = safety (center is safest; approaching edge reduces score) |
| Fee capture = `fees / expected` raw ratio | Fee efficiency = ratio normalized by pool APR (high-fee pools rewarded) |
| Depeg exposure = additive score component | Depeg risk = multiplicative penalty cap (max 30% score reduction) |

## Component Changes

### 1. `score_position_range_status` → **Range Centering**
**Old:** `max_pts * (0.6 + 0.4 * edge_frac)` — rewards being close to edge (0 at edge, 1 at center)
**New:** `max_pts * (1 - edge_frac)` — rewards being in center (center=full credit, edge=0)
- `edge_frac`: 0 at center, 1 at boundary (same input format)
- Linear decay from center to boundary
- Out-of-range still scores 0

**Rationale:** Being in the middle of range is objectively safer. A position grazing the boundary is one bad move away from exiting the range and losing fees.

---

### 2. `score_position_fee_capture` → **Fee Efficiency (APR-Normalized)**
**Old:** `fees_usd / expected_fees_usd` — raw ratio; 100% capture on 2% APR = same as 50% on 50% APR
**New:** Normalize by pool's realized APR so positions in high-fee pools are properly rewarded:

```python
pool_apr = pool.get("realized_fee_apr", 0.01)  # default 1% if missing
fee_yield_usd_per_day = fees_usd / max(days_open, 1)  # actual daily yield
pool_daily_yield_cap = position_value * (pool_apr / 100) / 365  # pool's potential daily yield
fee_ratio = fee_yield_usd_per_day / pool_daily_yield_cap  # how much of pool's potential was captured
fee_score = max_pts * clamp(fee_ratio / 2.0, 0, 1)  # cap at 2x so outliers don't dominate
```

**Rationale:** A position in a 10% APR pool capturing 40% of fees is more impressive than 80% capture on a 2% APR pool. This rewards placing liquidity where fees are actually meaningful.

---

### 3. **Net Yield Coverage** (Replaces separate `score_position_il_risk`)
**Old:** `max_pts * clamp(1 - il / il_zero_pct, 0, 1)` — IL is pure penalty; score maxes at 1.0 even if fees > IL
**New:** Single component `net_yield_coverage` that captures whether fees cover IL:

```python
il_usd = estimated_impermanent_loss_usd  # from existing IL estimation
net_yield_usd = fees_usd - il_usd
net_yield_pct = net_yield_usd / max(position_value, 1)  # as % of position value
# >0 = profitable, 0 = break-even, <0 = losing money
# Linearly mapped: 1.0 = break-even, each +1% net yield = +1% above baseline
coverage_score = max_pts * clamp(1.0 + net_yield_pct, 0, 2)
```

**Rationale:** This is the single most important health metric. A position with high fees but even higher IL is unhealthy. A position with moderate fees that cover IL is healthy. The score naturally goes above `max_pts` when profitable.

---

### 4. `score_position_staleness` → **Range Efficiency** (New, replaces staleness)
**Old:** `max_pts * clamp(1 - (days - grace)/(zero - grace), 0, 1)` — penalizes long-held positions (30d grace, 0 at 120d)
**New:** `max_pts * (in_range_days / total_days)` — rewards positions that have consistently stayed in-range

- `in_range_days`: count of days price stayed within [lower, upper] bounds
- `total_days`: `days_open` from position entry
- If `total_days == 0`: score = `max_pts` (brand new position, no data yet)
- If `in_range_days / total_days >= 0.9`: near-full credit
- If ratio < 0.5: partial credit

**Rationale:** Rewards good range selection, not punishes age. A position open 100 days with 95 in-range is better than one open 10 days with 8 in-range.

---

### 5. `score_position_depeg_exposure` → **Penalty Multiplier**
**Old:** Additive component in the score sum (e.g., `max_pts * min(peg_fractions)`)
**New:** Multiplier applied to the final combined score: `1.0 - (depeg_exposure * 0.3)`

- `depeg_exposure` = existing function output (0 to 1, where 1 = fully pegged)
- Max 30% score reduction for badly depegged pairs
- Well-pegged pairs (depeg_exposure ≈ 1) get no reduction

**Rationale:** Depeg risk is a safety penalty, not a reward signal. It should only ever reduce the score, never contribute positively.

---

### 6. Updated `_score_position_core` Component Order & Weights
**New component weights (per profile, renormalized to sum to 100):**

| Profile | range_status | fee_efficiency | net_yield_cov | range_efficiency | depeg_penalty |
|---|---|---|---|---|
| stable_stable | 30% | 25% | 25% | 15% | 5% |
| stable_bluechip | 25% | 25% | 30% | 15% | 5% |
| bluechip_bluechip | 25% | 25% | 30% | 15% | - (no depeg) |

**Note:** Weights sum to 100% and reflect the new model's priorities:
- **fee_efficiency** (25%) + **net_yield_cov** (30%) = 55% focused on profitability
- **range_status** (25%) + **range_efficiency** (15%) = 40% focused on range safety
- **depeg_penalty** (5%) = small safety cap

The old `il_risk` component is subsumed into `net_yield_cov`.

---

### 7. Required New Position Fields
The following fields must be present in position records for the new model to work:

| Field | Source | Description |
|---|---|---|
| `fees_usd` | Raw position / Missy | Fees earned in USD |
| `current_value_usd` | Raw position | Current position value |
| `edge_distance_frac` | Missy / computed | 0=center, 1=boundary (for range centering) |
| `in_range_days` | New / computed | Count of days price stayed within bounds |
| `il_estimate_pct` | Missy / estimated | IL percentage (for net yield) |

If `in_range_days` is not available, fall back to: if `days_open` is known and `in_range=True`, assume full efficiency (conservative). If neither is known, use `unknown_credit` partial credit.

---

### 8. Backward Compatibility
- All existing component functions remain available with their original names
- New functions use `_rfne_` prefix where they replace old ones:
  - `score_position_range_status` → unchanged signature but different formula
  - `score_position_fee_capture` → unchanged signature but APR-normalized
  - `score_position_il_risk` → deprecated; use net yield coverage instead
  - `score_position_staleness` → deprecated; use range efficiency instead
- Profile weights in `profiles.json` should be updated to new values
- The `score_position_core` aggregation point combines old and new components during transition

## Implementation Timeline

| Phase | Action |
|---|---|
| 1 | Update `score_position_range_status` in both `lp_scoring.py` and `scoring/position/score.py` |
| 2 | Update `score_position_fee_capture` with APR normalization |
| 3 | Add `net_yield_coverage` function (replace `score_position_il_risk` logic) |
| 4 | Add `range_efficiency` function (replace `score_position_staleness`) |
| 5 | Update `score_position_depeg_exposure` to penalty multiplier |
| 6 | Update `_score_position_core` to use new component order/weights |
| 7 | Update `profiles.json` weights to match new model |
| 8 | Update `EXPLANATION.md` with new formulas |
| 9 | Run tests (`test_lp_scoring.py`) to verify no regressions |
| 10 | Document in `RFNE-PLAN.md` and update any user-facing docs |

## Test Considerations
- Existing tests that check `score_position_range_status` returning `0.6+0.4*edge_frac` pattern will need updated expected values
- Tests checking `score_position_staleness` with specific days_open values will need new expectations
- Tests checking `score_position_il_risk` with specific IL values will need new net yield expectations
- The `unknown_credit` behavior should remain identical for missing data paths

## Migration Path for Existing Positions
Positions scored under the old model will get a **transition score** during the first cycle with the new model:
- If `in_range_days` is missing but `days_open` and `in_range=True`: assume 100% range efficiency (conservative)
- If `il_estimate_pct` is missing but `fees_usd` and `current_value_usd` known: compute net yield with IL=0 (will likely give above-baseline score, flagging for data review)
- Missing `edge_distance_frac`: default to 0.5 (center), giving 50% centering credit