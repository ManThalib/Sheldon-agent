---
name: scoring-evaluator
description: Use for manual, anomaly, or review LP evaluation cycles — run the scoring engine, form verdicts, and write George-schema signals.
---

# Scoring Evaluator (DeFi LP)

I evaluate **LP position decisions only**: OPEN, HOLD, CLOSE, COLLECT_FEES.
I do not do market timing (no buy/sell price-direction calls) — that is out of scope by design.

## Multi-DEX handling

Missy now scans **Meteora, Raydium, and Orca**. The scoring engine ingests all
three, but George currently executes only **Meteora DLMM**. Therefore:
- Pool scores are produced for all DEXes.
- `CLOSE`/`COLLECT_FEES` signals are written only for Meteora positions.
- Non-Meteora candidates are logged as review items, not queued as signals.
- This skill's signal-writing step applies only to Meteora; non-Meteora OPEN
candidates stay in review until George is extended to handle them.

## Routine vs manual

**Routine cycles are automated and consume zero model tokens.** The
`missy-pool-scan` cron job runs
`scoring/run_cycle.py --write-signals --json` after each Missy scan. That script
does the scoring and writes signals; no agent wake happens on no-signal cycles.

Use this skill only when:
- Mr. Man asks for analysis or explanation of the latest scores.
- The script reports an anomaly (stale data, malformed scan, engine failure).
- A score is borderline and needs judgment before signaling George.
- The scoring logic, weights, or thresholds need to change.

## Procedure

1. **Read the newest Missy outputs.** Pool data: the newest `pool_scan-*.json` in `/data/missy-data/pool_screens/`. Position data: the newest `position_scan-*.json` in `/data/missy-data/position_scans/`. Use the files with the latest UTC timestamps; ignore `.failed` and `.invalid` files. An empty `positions` array is a valid "no active positions" input. If the newest files are older than two cycles, treat the data as stale and stop. Completion check: both newest files identified and parsed.

2. **Run the cycle runner.** Execute:
   `python3 /data/.openclaw/workspace-agents/sheldon/scoring/run_cycle.py --json`
   The engine computes two independent scores (weights and thresholds live in `lp_scoring.py` constants — never improvise per-cycle):
   - **Pool LP Opportunity Score (0–100)**: fee yield (35) + turnover (20) + depth (15) + volatility fit (20) + bin-step fit (10). Thresholds: >= 70 OPEN_CANDIDATE, 55–69 WATCH, < 55 IGNORE.
   - **Position Health Score (0–100)**: range status (35) + fee capture (25) + IL risk (25) + time decay (15). Thresholds: < 40 CLOSE, 40–59 REVIEW, >= 60 HOLD. Fees >= $10 pending → COLLECT_FEES.
   Completion check: engine ran clean with no `failures` list; a failure means stop and report the data problem.

3. **Form the verdicts.** For each pool: OPEN only when the engine says OPEN_CANDIDATE, no conflicting active LP position in the same pool, and free allocation remains. For each open position: CLOSE when health < 40, HOLD when >= 60, REVIEW needs Mr. Man's eye — report it, do not signal. Collect fees when flagged, independent of health.

4. **Write signals for George.** Run the runner with `--write-signals`; it writes one JSON file per actionable action into `/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/signals/pending/` using George's exact schema — see `george-signal-schema.md` next to this file. Mapping: CLOSE -> `action:"close"` with `position_id`; COLLECT_FEES -> `action:"claim_fees"`. HOLD/REVIEW/IGNORE/WATCH: no signal file. OPEN candidates are returned as review items, not signals, until price/bin data exists. Completion check: every signal's `reason` cites its numbers; no verdict without evidence.

5. **Log the cycle.** `run_cycle.py` appends inputs, scores, signals, and review items to `memory/YYYY-MM-DD.md` automatically. For manual analysis, add any extra reasoning above that entry.

## Reference notes
- The scoring engine is mine: `scoring/lp_scoring.py`. Missy's `pool_scorer` stays hers; do not copy or modify it.
- Threshold/weight changes happen only when Mr. Man orders them — then edit the constants block in `lp_scoring.py` and note the change in the log.
- Position-level inputs (fees_usd, in_range, il_estimate_pct, days_open) arrive only when George's data feed provides them; the engine scores neutral on missing fields and says so.
- Known gap: OPEN bin ranges and token amounts require allocation logic that Mr. Man wants to finalize later. Until then, OPEN candidates (even Meteora) are returned as review items, not as queued signals.
