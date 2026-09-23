---
name: lp-scoring-script
description: Run the deterministic LP scoring engine as a headless script job; only wake the Sheldon agent for anomalies, OPEN candidates, or explicit human review.
---

# LP Scoring Script Job

This skill covers the **routine, token-efficient automation path** for Missy → Sheldon → George.
The `scoring-evaluator` skill is reserved for manual, anomaly, or review cycles.

## When to use this skill

- A scheduled job needs to score Missy's newest scans every cycle.
- The scoring logic in `scoring/lp_scoring.py` is unchanged and deterministic.
- The goal is to avoid loading the full agent context for no-signal cycles.

## When NOT to use this skill

- The engine logic, weights, or thresholds need to change (use `scoring-evaluator` and update `lp_scoring.py`).
- The scan data is malformed or anomalous (use `scoring-evaluator` to investigate).
- Mr. Man asked for an analysis or explanation (use `scoring-evaluator`).
- An OPEN candidate needs sizing/range decisions (use `scoring-evaluator`).

## Procedure

1. **Create or update the script job.** Use a `script` payload (`payload.kind: "script"`) in OpenClaw automation. Do not use `agentTurn`. A sample wrapper is in `run-scoring.py` next to this file. Completion check: the job runs `lp_scoring.py --json` and processes its JSON report without loading the agent context.

2. **Read the newest Missy outputs.** The wrapper finds the latest `pool_scan-*.json` in `/data/missy-data/pool_screens/` and `position_scan-*.json` in `/data/missy-data/position_scans/`. If either file is missing or stale, stop and wake the agent. Completion check: fresh pair identified or agent woken.

3. **Run the scoring engine.** Execute:
   `python3 /data/.openclaw/workspace-agents/sheldon/scoring/lp_scoring.py --json`
   Completion check: process exits 0 and emits valid JSON with a `generated_at` field.

4. **Act on the report.**
   - **OPEN_CANDIDATE**: do **not** write an incomplete OPEN signal. Wake the agent so it can finalize bin range, liquidity, and sizing using `scoring-evaluator`.
   - **CLOSE**: write a `close` signal JSON to George's pending queue with `position_id`.
   - **COLLECT_FEES**: write a `claim_fees` signal with `position_id`.
   - **WATCH**, **IGNORE**, **HOLD**, **REVIEW**, or empty results: write no signal.
   - See `scoring-evaluator/george-signal-schema.md` for the exact signal schema. Completion check: each written signal file is valid JSON, schema-exact, and named `sheldon-<UTC-timestamp>-<n>.json`.

5. **Append a short audit line to the daily log.** Write one line to `memory/YYYY-MM-DD.md` noting the scan sources, top pool scores, signals written, and that the run was script-driven. Completion check: log line exists.

6. **Conditional agent wake.** Wake the Sheldon agent (`agent:sheldon:main`) only when:
   - An OPEN candidate appears (sizing needed).
   - The engine returned a FAILURES block or exited non-zero.
   - The data is stale or malformed.
   - A score crosses the threshold by a margin that warrants review.
   Otherwise, complete silently. Completion check: no `sessions_send` fired on no-signal, healthy cycles.

## Token-efficiency rule

Never load the full agent context just to run deterministic Python. If a cycle only runs `lp_scoring.py`, produces no OPEN candidate, and no anomaly, it should consume zero model tokens.

## Exit codes for the script wrapper

- `0`: success — any CLOSE/claim_fees signals written, no OPEN candidate, no anomaly.
- `2`: failure — engine or data error; wake the agent.
- `3`: review — OPEN candidate(s) found; wake the agent for sizing.

## Reference
- Engine: `/data/.openclaw/workspace-agents/sheldon/scoring/lp_scoring.py`
- Sample wrapper: `run-scoring.py` (next to this file)
- Signal schema: `scoring-evaluator/george-signal-schema.md`
- Manual/anomaly review: `scoring-evaluator/SKILL.md`
