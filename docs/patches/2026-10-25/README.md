# Sheldon patch drafts — 2026-10-25 (owner-requested review)

Drafts only — nothing applied. Each file: symptom with live evidence, root
cause with file:line, exact diff, tests, verification, rollback.

| # | Change | Files touched | Fixes |
|---|--------|---------------|-------|
| 01 | Swappable idle SOL counts as deployable capital | `sheldon_policy.json`, `strategy/sheldon_policy.py`, `strategy/position_sizing.py`, `strategy/capital_plan.py`, `strategy/candidate_filtering.py`, `tests/test_capital_stable_only.py` (extend) | Live deadlock: $83.87 SOL + $19.69 USDC read as $19.69 → 17h with zero opens |
| 02 | Suppress the open-rubric close artifact | `range_state.py`, `run_cycle/signals.py`, new `tests/test_close_rubric_artifact.py` | `CLOSE score=94.17` artifact executed a real close of a HOLD-scored position (sheldon-1792856108-2) |
| 03 | Capital prep runs after the sizing gate | `run_cycle/signals.py`, new `tests/test_prep_sequencing.py` | 6 prep swap legs on 2026-10-24 for an open the $20 floor then refused |

Applies cleanly against Sheldon-agent @ 0fd1840. George executor unchanged.

Dependency: 03's gate reads `strategy["suggested_usdc"]`, which patch 01
re-bases onto total capital — apply 01 first, then 03, then 02 (independent).

Apply order: 01 → 03 → 02. Run the full Sheldon unittest suite after each.
