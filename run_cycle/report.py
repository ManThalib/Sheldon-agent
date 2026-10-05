"""Report and logging logic extracted from run_cycle.py.

Contains:
  - append_log: append cycle summary to daily markdown log
  - _short_summary: human-readable summary of verdicts
  - _wake_george: trigger George doorbell when signals written
"""

import os
import time
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta


def _utc_iso():
    """Return current time in Asia/Shanghai (UTC+8) ISO format."""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def append_log(report: dict, memory_dir: str, signals_created: list, review: list):
    """Append cycle summary to daily markdown log.

    Mirrors the original append_log from run_cycle.py.
    """
    os.makedirs(memory_dir, exist_ok=True)
    log_path = os.path.join(memory_dir, time.strftime("%Y-%m-%d") + ".md")

    wallet = report.get("wallet", {})
    capital = report.get("capital_plan", {})
    funding = report.get("funding") or {}

    lines = [
        f"## Cycle — {_utc_iso()}",
        f"- sources: pools={report.get('sources', {}).get('pools')} positions={report.get('sources', {}).get('positions')} wallet={report.get('sources', {}).get('wallet_scan')}",
        f"- verdicts: {len(report.get('verdicts', []))}",
        f"- capital: idle={wallet.get('idle_usdc', 0.0):.2f} dust={wallet.get('dust_total_usdc', 0.0):.2f} deployable={wallet.get('deployable_usdc', 0.0):.2f}",
        f"- plan: suggested_position={capital.get('suggested_position_usdc', 0.0):.2f} open_eligible={capital.get('open_eligible', False)}",
    ]
    if funding:
        lines.append(
            f"- funding: funded={len(funding.get('funded') or [])} "
            f"unfunded={len(funding.get('unfunded') or [])} "
            f"prep_allowed={len(funding.get('prep_swaps_allowed') or [])} "
            f"prep_blocked={len(funding.get('prep_swaps_blocked') or [])} "
            f"sol_reserve={(funding.get('sol_reserve_lamports') or 0) / 1e9:.4f}"
        )
        for note in funding.get("notes") or []:
            lines.append(f"  - funding note: {note}")
    sweep = report.get("idle_sweep") or {}
    if sweep:
        lines.append(
            f"- idle sweep: {sweep.get('decision', 'skip')} — "
            f"net_idle=${sweep.get('idle_net_usd', 0.0):.2f} "
            f"queued={sweep.get('queued_committed_usdc', 0.0):.2f} — "
            f"{sweep.get('reason') or sweep.get('signal_id', '')}"
        )
        for s in sweep.get("skipped") or []:
            lines.append(f"  - sweep target `{s.get('pool_address')}` — {s.get('reason')}")
    grace = report.get("out_of_range_grace") or {}
    if grace.get("deferred_positions"):
        lines.append(
            f"- out-of-range grace: {len(grace['deferred_positions'])} position(s) "
            f"held below {grace['allowed_runs']} consecutive out-of-range runs "
            f"({', '.join(grace['deferred_positions'])})"
        )
    if report.get("failures"):
        lines.append(f"- failures: {report['failures']}")
    if review:
        lines.append(f"- review needed: {len(review)}")
        for r in review:
            lines.append(f"  - `{r['pool_address']}` score={r.get('score')} — {r['reason']}")
    if signals_created:
        lines.append(f"- signals written: {len(signals_created)}")
        for p in signals_created:
            lines.append(f"  - `{p}`")
    else:
        lines.append("- signals written: 0 (no actionable verdicts)")
    lines.append("- summary: " + _short_summary(report, review))
    lines.append("")
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _short_summary(report: dict, review: list = None) -> str:
    """Human-readable summary of verdicts."""
    parts = []
    opens = [v for v in report.get("verdicts", []) if v.get("action") == "OPEN_CANDIDATE"]
    closes = [v for v in report.get("verdicts", []) if v.get("action") == "CLOSE"]
    collects = [v for v in report.get("verdicts", []) if v.get("action") == "COLLECT_FEES"]
    wallet = report.get("wallet", {})
    capital = report.get("capital_plan", {})

    if opens or (review and len(review) > 0):
        parts.append(f"OPEN={len(opens)}")
    if closes:
        parts.append(f"CLOSE={len(closes)}")
    if collects:
        parts.append(f"COLLECT={len(collects)}")
    if wallet:
        parts.append(f"idle={wallet.get('idle_usdc', 0.0):.2f}")
        parts.append(f"dust={wallet.get('dust_total_usdc', 0.0):.2f}")
    if capital:
        parts.append(f"suggested={capital.get('suggested_position_usdc', 0.0):.2f}")
        parts.append(f"eligible={capital.get('open_eligible', False)}")
    if not parts:
        return "no actionable verdicts"
    return " | ".join(parts)


def _wake_george(signals_created: list, review: list):
    """Trigger the George signal doorbell immediately when signals are written.

    The doorbell automation checks signals/pending/ and sends George's main
    session a wake message only when fresh files are detected. The regular
    2-minute schedule remains as a fallback; this call shortens latency.
    """
    if not signals_created:
        return

    # Doorbell automation ID (george-signal-wake).
    doorbell_id = "26a163bb-5e85-4fe8-ba99-8e584bc2e09e"
    cmd = ["openclaw", "automations", "run", doorbell_id, "--wait"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        # Fallback doorbell will catch it on its next 2-minute tick; log failure.
        print(f"WAKE_GEORGE_FAILED: {exc}", file=sys.stderr)