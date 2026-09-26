#!/usr/bin/env python3
"""Sheldon cycle runner.

Runs the deterministic LP scoring engine and, if configured, emits George-schema
signal files. Zero model reasoning. Outputs: JSON report, human summary log line,
and optional signal JSON files for George.

Usage:
    python3 run_cycle.py [--write-signals] [--signals-dir /path] [--json]
Returns exit 0 on success, 1 on fatal error, 2 on stale/missing data.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

from lp_scoring import newest_file, load_json, ConfigError
import lp_scoring

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDtWv"

# Protocols George can execute. Signals for anything else stay in review.
SUPPORTED_DEXES = {"meteora", "raydium", "orca"}

# Rail mirrors (George defaults). Externalize when George exposes machine-readable rails.
MIN_POSITION_USD = 15.0
DEFAULT_MAX_POSITION_USD = 100.0
DEFAULT_MAX_RANGE_WIDTH = 200
DEFAULT_MAX_SLIPPAGE_BPS = 100


def _range_center(pool: dict, dex: str) -> int:
    """Return the pool's current position index.

    Meteora uses DLMM bin IDs (``active_bin_id``); Raydium CLMM and Orca
    Whirlpool use ticks. Missy may supply ``current_tick``,
    ``current_tick_index`` or reuse ``active_bin_id`` for the tick value.
    """
    if dex == "meteora":
        return int(pool.get("active_bin_id") or 0)
    for key in ("current_tick", "current_tick_index", "active_bin_id"):
        val = pool.get(key)
        if val not in (None, "", 0, "0"):
            return int(val)
    return 0

# Where George's executor looks for pending signals.
DEFAULT_SIGNALS_DIR = "/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/signals/pending"
DEFAULT_MEMORY_DIR = "/data/.openclaw/workspace-agents/sheldon/memory"


def _utc_iso():
    # Use Asia/Shanghai (UTC+8) as the canonical timezone for timestamps.
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _read_usdc_balance(balance_cache_path: str = None) -> float:
    """Return idle USDC balance in USD.

    Resolution order:
      1. ``SHELDON_IDLE_USDC`` environment variable (USD float).
      2. ``/data/missy-data/wallet_balances.json`` cache file written by Missy.
      3. Live Solana RPC lookup of the wallet configured in George's config.
      4. Fallback 0.0 (OPEN signals are skipped when no capital is known).
    """
    env = os.environ.get("SHELDON_IDLE_USDC")
    if env is not None:
        try:
            return float(env)
        except ValueError:
            pass
    if balance_cache_path is None:
        balance_cache_path = "/data/missy-data/wallet_balances.json"
    try:
        with open(balance_cache_path, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
        usdc = cache.get("USDC", {}) or {}
        val = float(usdc.get("usd_value") or 0.0)
        if val > 0:
            return val
    except Exception:
        pass
    return _fetch_usdc_balance_rpc() or 0.0


def _load_george_config() -> tuple:
    """Return (wallet_public_key, rpc_https_url) from env or George's config."""
    path = "/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/config/agent.config.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        pubkey = os.environ.get("SOLANA_PUBLIC_WALLET", cfg.get("wallet", {}).get("public_key"))
        rpc_url = os.environ.get("SOLANA_RPC_URL", cfg.get("rpc", {}).get("https_url"))
        return pubkey, rpc_url
    except Exception:
        return None, None


def _fetch_usdc_balance_rpc() -> float:
    """Query on-chain USDC balance for the wallet in George's config.

    Returns raw USDC amount as a USD float (USDC has 6 decimals).
    Returns 0.0 on any failure.
    """
    pubkey, rpc_url = _load_george_config()
    if not pubkey or not rpc_url:
        return 0.0
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            pubkey,
            {"mint": USDC_MINT},
            {"encoding": "jsonParsed"},
        ],
    }
    headers = {"Content-Type": "application/json"}
    try:
        req = urllib.request.Request(
            rpc_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("error"):
            return 0.0
        total = 0
        for item in data.get("result", {}).get("value", []):
            info = item.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            amount = info.get("tokenAmount", {}).get("amount")
            if amount:
                total += int(amount)
        return total / 1_000_000.0
    except Exception:
        return 0.0


def _load_active_positions(positions_dir: str) -> dict:
    """Return dict of pool_address -> position dict for currently active positions."""
    pos_path = newest_file(positions_dir, "position_scan")
    if not pos_path:
        return {}
    try:
        data = load_json(pos_path)
    except Exception:
        return {}
    positions = data.get("positions", []) if isinstance(data, dict) else data
    return {p.get("pool_address"): p for p in positions if p.get("pool_address")}


def write_signals(report: dict, signals_dir: str, positions_dir: str) -> tuple:
    """Convert verdicts into George-schema signal files.

    Returns:
        (created_paths, review_items)
        created_paths: list of files written to George's pending queue.
        review_items:  list of OPEN candidates that need bin/liquidity review.
    """
    os.makedirs(signals_dir, exist_ok=True)
    active = _load_active_positions(positions_dir)
    created = []
    review = []
    base = int(time.time())
    idx = 1

    for v in report.get("verdicts", []):
        action = v.get("action")
        pool_addr = v.get("pool_address")
        dex = v.get("dex") or "unknown"

        # Only protocols George can execute get a queued signal.
        if dex not in SUPPORTED_DEXES:
            review.append({
                "pool_address": pool_addr,
                "dex": dex,
                "score": v.get("score"),
                "evidence": v.get("evidence"),
                "reason": f"{action} candidate ({dex}); unsupported protocol",
            })
            continue

        if action in {"CLOSE", "REBALANCE", "COLLECT_FEES"}:
            lower = v.get("lower_bound")
            upper = v.get("upper_bound")
            try:
                lower = int(lower) if lower is not None else 0
                upper = int(upper) if upper is not None else 0
            except (TypeError, ValueError):
                lower, upper = 0, 0
            signal = {
                "signal_id": f"sheldon-{base}-{idx}",
                "action": "claim_fees" if action == "COLLECT_FEES" else "close",
                "dex": dex,
                "pool_address": pool_addr,
                "position_id": v.get("position"),
                "side": "bidirectional",
                "bin_range": {"lower": lower, "upper": upper},
                "liquidity": {"amount_x": "0", "amount_y": "0"},
                "max_slippage_bps": DEFAULT_MAX_SLIPPAGE_BPS,
                "reason": f"{action} score={v.get('score')}; evidence={json.dumps(v.get('evidence'))}",
                "created_at": _utc_iso(),
            }
        elif action == "OPEN_CANDIDATE":
            signal = _build_open_signal(v, idx, base)
            if signal is None:
                review.append({
                    "pool_address": pool_addr,
                    "dex": dex,
                    "score": v.get("score"),
                    "evidence": v.get("evidence"),
                    "reason": "OPEN candidate; could not build signal (missing enrichment or insufficient capital)",
                })
                continue
        else:
            # HOLD, REVIEW, WATCH, IGNORE => no signal.
            continue

        path = os.path.join(signals_dir, f"{signal['signal_id']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(signal, fh, indent=2)
            fh.write("\n")
        created.append(path)
        idx += 1

    return created, review


def _build_open_signal(v: dict, idx: int, base: int) -> dict:
    """Build a George-schema 'open' signal for a supported pool.

    Allocation rule from owner:
      - Use idle wallet money held in USDC.
      - Position value = min(25% of idle USDC, max_position_usd rail).
      - Minimum total position value = 15 USDC.
      - Split 50/50 USD between token_x and token_y.
      - Compute raw amounts from token decimals and prices.
      - Range = current index +/- max_range_width/2, where the index is a DLMM
        bin for Meteora and a tick for Raydium CLMM / Orca Whirlpool.
    """
    dex = v.get("dex") or "unknown"
    if dex not in SUPPORTED_DEXES:
        return None
    pool = v.get("_pool") or {}
    if not pool:
        return None

    # Pull prices and decimals.
    px_x = float(pool.get("token_x_price_usd") or 0.0)
    px_y = float(pool.get("token_y_price_usd") or 0.0)
    dec_x = int(pool.get("token_x_decimals") or 0)
    dec_y = int(pool.get("token_y_decimals") or 0)
    if px_x <= 0 or px_y <= 0 or dec_x <= 0 or dec_y <= 0:
        return None

    center = _range_center(pool, dex)
    if center == 0:
        return None

    idle_usdc = _read_usdc_balance()
    if idle_usdc <= 0:
        return None

    # 25% of idle USDC, capped by the per-position rail, with a hard minimum.
    position_usd = min(idle_usdc * 0.25, DEFAULT_MAX_POSITION_USD)
    if position_usd < MIN_POSITION_USD:
        return None

    # 50/50 USD split.
    half_usd = position_usd / 2.0
    amount_x = int((half_usd / px_x) * (10 ** dec_x))
    amount_y = int((half_usd / px_y) * (10 ** dec_y))
    if amount_x <= 0 or amount_y <= 0:
        return None

    half_width = max(1, DEFAULT_MAX_RANGE_WIDTH // 2)
    lower = center - half_width
    upper = center + half_width

    return {
        "signal_id": f"sheldon-{base}-{idx}",
        "action": "open",
        "dex": dex,
        "pool_address": pool.get("pool_address"),
        "side": "bidirectional",
        "bin_range": {"lower": lower, "upper": upper},
        "liquidity": {
            "amount_x": str(amount_x),
            "amount_y": str(amount_y),
        },
        "max_slippage_bps": DEFAULT_MAX_SLIPPAGE_BPS,
        "reason": (
            f"OPEN score={v.get('score')} dex={dex} "
            f"position_usd={position_usd:.2f} center={center}"
        ),
        "created_at": _utc_iso(),
    }


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


def append_log(report: dict, memory_dir: str, signals_created: list, review: list):
    os.makedirs(memory_dir, exist_ok=True)
    log_path = os.path.join(memory_dir, time.strftime("%Y-%m-%d") + ".md")
    lines = [
        f"## Cycle — {_utc_iso()}",
        f"- sources: pools={report.get('sources', {}).get('pools')} positions={report.get('sources', {}).get('positions')}",
        f"- verdicts: {len(report.get('verdicts', []))}",
    ]
    if report.get("failures"):
        lines.append(f"- failures: {report['failures']}")
    if review:
        lines.append(f"- review needed (OPEN candidates): {len(review)}")
        for r in review:
            lines.append(f"  - `{r['pool_address']}` score={r['score']} — {r['reason']}")
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
    parts = []
    opens = [v for v in report.get("verdicts", []) if v.get("action") == "OPEN_CANDIDATE"]
    closes = [v for v in report.get("verdicts", []) if v.get("action") == "CLOSE"]
    collects = [v for v in report.get("verdicts", []) if v.get("action") == "COLLECT_FEES"]
    if opens or (review and len(review) > 0):
        parts.append(f"OPEN={len(opens)}")
    if closes:
        parts.append(f"CLOSE={len(closes)}")
    if collects:
        parts.append(f"COLLECT={len(collects)}")
    if not parts:
        return "no actionable verdicts"
    return " | ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sheldon deterministic cycle runner")
    ap.add_argument("--pools-dir", default="/data/missy-data/pool_screens")
    ap.add_argument("--positions-dir", default="/data/missy-data/position_scans")
    ap.add_argument("--write-signals", action="store_true")
    ap.add_argument("--signals-dir", default=DEFAULT_SIGNALS_DIR)
    ap.add_argument("--memory-dir", default=DEFAULT_MEMORY_DIR)
    ap.add_argument("--json", action="store_true", help="print full JSON report to stdout")
    ap.add_argument("--max-age-seconds", type=float, default=3900.0)
    args = ap.parse_args()

    try:
        report = lp_scoring.run_cycle(args.pools_dir, args.positions_dir, args.max_age_seconds)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 1

    if report.get("failures"):
        err = "; ".join(report["failures"])
        print(f"ERROR: {err}", file=sys.stderr)
        if args.json:
            json.dump(report, sys.stdout, indent=2)
            print()
        return 2

    signals_created = []
    review = []
    if args.write_signals:
        signals_created, review = write_signals(report, args.signals_dir, args.positions_dir)
        _wake_george(signals_created, review)

    append_log(report, args.memory_dir, signals_created, review)

    # Emit a machine-readable wake hint so the calling Sheldon session can
    # notify George immediately when actionable signals were written.
    if signals_created:
        summary = ", ".join(Path(p).name for p in signals_created)
        print(f"WAKE_GEORGE: {len(signals_created)} signal(s) -> {summary}")

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(_short_summary(report, review))

    return 0


if __name__ == "__main__":
    sys.exit(main())
