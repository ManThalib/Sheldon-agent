"""Capital readiness gate for Sheldon open signals.

Compares the tokens each open strategy needs against the wallet's actual
per-token balances from Missy's newest raw wallet scan, and produces prep
swap signals for the shortfall (full 50/50 rebalance):

- Funded strategy  -> open signal (handled by run_cycle.write_signals).
- Short strategy   -> buy swap(s) for the exact delta, USDC -> token.
- All funded and a token surplus remains -> sell the surplus back to USDC.

The wallet's native SOL is never deployed below ``SOL_RESERVE_LAMPORTS``;
that reserve always stays for rent, tx fees, and priority fees.

All inputs come from Missy scans; no RPC calls are made here.
"""

import glob
import json
import math
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from capital import RESERVED_MINTS, SOL_MINT, USDC_MINT

# --------------------------------------------------------------------------
# Rails (producer-side mirror of George's executor rails)
# --------------------------------------------------------------------------
USDC_DECIMALS = 6

# Native SOL never spent by swaps/positions: rent for token/position
# accounts plus tx fees and George's priority-fee cap need headroom.
SOL_RESERVE_LAMPORTS = 20_000_000  # 0.02 SOL

# Extra USDC added on top of a buy delta to cover swap slippage (rail max
# 1%) and price drift between the wallet scan and execution.
BUY_BUFFER_PCT = 2.0

# Never prep-swap less than this (fees would eat the round trip).
MIN_PREP_SWAP_USD = 1.0

# Only sell a surplus token when the leftover is worth at least this much.
SELL_SURPLUS_MIN_USD = 2.0

# Hard stop: more than this many prep swaps for one mint pair per hour means
# the loop is not converging; block and surface for review.
MAX_PREP_SWAPS_PER_MINT_PER_HOUR = 3

# A wallet scan taken less than this long after the last prep swap may not
# yet reflect the confirmed tx (RPC indexing lag); the rescan must be at
# least this much newer than the swap before another prep swap may fire.
# (Live incident 2026-09-29: rapid re-scans raced the chain and re-emitted
# the same buy swap seven times.)
PREP_CONFIRM_GRACE_SECONDS = 60

# Sheldon judges funding from the wallet scan; a scan older than this is
# stale no matter what it says. Prep swaps require a fresh scan.
MAX_WALLET_SCAN_AGE_SECONDS = 180

DEFAULT_SIGNALS_DIR = "/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/signals"


def _utc_iso():
    # Asia/Shanghai (UTC+8), matching the rest of Sheldon's outputs.
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _created_epoch(created_at: str) -> float:
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError):
        return 0.0


# --------------------------------------------------------------------------
# Raw wallet scan loading
# --------------------------------------------------------------------------
def load_raw_wallet(wallet_scans_dir: str) -> Dict[str, Any]:
    """Load the newest raw Missy wallet scan (per-mint amounts, decimals).

    ``newest_file`` returns the ``wallet_screen-latest.json`` symlink whose
    target is always the newest scan, so content and mtime are fresh.
    """
    import lp_scoring  # local import: lp_scoring owns file discovery

    path = lp_scoring.newest_file(wallet_scans_dir, "wallet_screen")
    if not path:
        return {"path": None, "mtime": 0.0, "assets": [], "error": "no wallet scan found"}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return {"path": path, "mtime": 0.0, "assets": [], "error": f"wallet scan unreadable: {exc}"}
    try:
        mtime = os.stat(path).st_mtime  # follows the latest.json symlink
    except OSError:
        mtime = 0.0
    assets = data.get("assets") if isinstance(data, dict) else None
    if not isinstance(assets, list):
        return {"path": path, "mtime": mtime, "assets": [], "error": "wallet scan malformed (no assets)"}
    return {"path": path, "mtime": mtime, "assets": assets, "error": None}


# --------------------------------------------------------------------------
# Requirements math (must match run_cycle._build_open_signal exactly)
# --------------------------------------------------------------------------
def required_amounts(strategy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Raw token amounts an open strategy will request, 50/50 USD split."""
    pool = strategy.get("_pool") or {}
    px_x = float(pool.get("token_x_price_usd") or 0.0)
    px_y = float(pool.get("token_y_price_usd") or 0.0)
    dec_x = int(pool.get("token_x_decimals") or 0)
    dec_y = int(pool.get("token_y_decimals") or 0)
    x_mint = (pool.get("token_x_address") or "").strip()
    y_mint = (pool.get("token_y_address") or "").strip()
    position_usd = float(strategy.get("suggested_usdc") or 0.0)
    if position_usd <= 0 or px_x <= 0 or px_y <= 0 or dec_x <= 0 or dec_y <= 0:
        return None
    if not x_mint or not y_mint:
        return None

    half_usd = position_usd / 2.0
    x_raw = int((half_usd / px_x) * (10 ** dec_x))
    y_raw = int((half_usd / px_y) * (10 ** dec_y))
    if x_raw <= 0 or y_raw <= 0:
        return None
    return {
        "x_mint": x_mint,
        "y_mint": y_mint,
        "x_raw": x_raw,
        "y_raw": y_raw,
        "x_dec": dec_x,
        "y_dec": dec_y,
        "x_px": px_x,
        "y_px": px_y,
        "half_usd": half_usd,
    }


# --------------------------------------------------------------------------
# Funding plan
# --------------------------------------------------------------------------
def plan_funding(strategies: List[Dict[str, Any]], raw_assets: List[Dict[str, Any]],
                 wallet_path: Optional[str] = None, wallet_mtime: float = 0.0,
                 dust_mints: Optional[set] = None) -> Dict[str, Any]:
    """Score-order funding walk across strategies.

    Returns per-strategy funded/unfunded state, prep swap specs for the
    highest-scored unfunded strategy, and surplus sell specs when everything
    selected is funded. Fails safe: no prep swaps without balance data.
    """
    assets: Dict[str, Dict[str, Any]] = {}
    for a in raw_assets or []:
        mint = (a.get("mint") or "").strip()
        if not mint:
            continue
        try:
            raw = int(a.get("amount_raw") or 0)
        except (TypeError, ValueError):
            raw = 0
        assets[mint] = {
            "amount_raw": max(0, raw),
            "amount_ui": float(a.get("amount_ui") or 0.0),
            "decimals": int(a.get("decimals") or 0),
            "price_usd": float(a.get("price_usd") or 0.0),
            "symbol": a.get("symbol") or "",
        }

    def spendable(mint: str) -> int:
        a = assets.get(mint)
        if not a:
            return 0
        if mint == SOL_MINT:
            return max(0, a["amount_raw"] - SOL_RESERVE_LAMPORTS)
        return a["amount_raw"]

    remaining = {mint: spendable(mint) for mint in assets}
    remaining.setdefault(USDC_MINT, 0)
    remaining.setdefault(SOL_MINT, 0)

    ordered = sorted(strategies, key=lambda s: (s.get("score") or 0.0), reverse=True)
    funded: List[Dict[str, Any]] = []
    unfunded: List[Dict[str, Any]] = []
    for s in ordered:
        entry = {
            "pool_address": s.get("pool_address"),
            "score": s.get("score"),
            "dex": s.get("dex"),
        }
        req = required_amounts(s)
        if req is None:
            entry.update({"funded": False, "reason": "missing pool enrichment (price/decimals/mint)"})
            unfunded.append(entry)
            continue
        entry["required"] = {"x_mint": req["x_mint"], "x_raw": req["x_raw"],
                             "y_mint": req["y_mint"], "y_raw": req["y_raw"]}
        entry["req"] = req
        have_x = remaining.get(req["x_mint"], 0)
        have_y = remaining.get(req["y_mint"], 0)
        entry["have"] = {"x_raw": have_x, "y_raw": have_y}
        if have_x >= req["x_raw"] and have_y >= req["y_raw"]:
            entry["funded"] = True
            remaining[req["x_mint"]] = have_x - req["x_raw"]
            remaining[req["y_mint"]] = have_y - req["y_raw"]
            funded.append(entry)
        else:
            entry["funded"] = False
            entry["reason"] = "insufficient token balance for open"
            unfunded.append(entry)

    prep_specs: List[Dict[str, Any]] = []
    notes: List[str] = []

    if unfunded:
        # Prep only the highest-scored unfunded strategy per cycle; the
        # swap -> rescan -> re-entry loop handles the rest one at a time.
        target = next((e for e in unfunded if e.get("req")), None)
        if target is None:
            notes.append("no fundable unfunded strategy (missing enrichment); no prep swap")
        else:
            target["prep_target"] = True
            req = target["req"]
            # USDC kept aside for this target's Y side if Y is USDC.
            budget = remaining.get(USDC_MINT, 0)
            if req["y_mint"] == USDC_MINT:
                budget -= req["y_raw"]
            for side in ("x", "y"):
                mint = req[f"{side}_mint"]
                need = req[f"{side}_raw"]
                have = remaining.get(mint, 0)
                if mint == USDC_MINT:
                    if have < need:
                        notes.append(
                            f"pool {target['pool_address']}: insufficient USDC "
                            f"({have / 10**USDC_DECIMALS:.2f} < {need / 10**USDC_DECIMALS:.2f}); no prep possible"
                        )
                    continue
                delta = need - have
                if delta <= 0:
                    continue
                dec = req[f"{side}_dec"]
                px = req[f"{side}_px"]
                if dec <= 0 or px <= 0:
                    notes.append(f"pool {target['pool_address']}: side {side} price/decimals unknown; no prep swap")
                    continue
                delta_ui = delta / (10 ** dec)
                cost_raw = int(math.ceil(delta_ui * px * (1.0 + BUY_BUFFER_PCT / 100.0) * (10 ** USDC_DECIMALS)))
                if cost_raw / (10 ** USDC_DECIMALS) < MIN_PREP_SWAP_USD:
                    continue
                if cost_raw > budget:
                    notes.append(
                        f"pool {target['pool_address']}: buy of {delta_ui:.6f} needs "
                        f"{cost_raw / 10**USDC_DECIMALS:.2f} USDC, only "
                        f"{budget / 10**USDC_DECIMALS:.2f} available; no prep swap"
                    )
                    continue
                a = assets.get(mint) or {}
                prep_specs.append({
                    "direction": "buy",
                    "for_pool": target.get("pool_address"),
                    "input_mint": USDC_MINT,
                    "output_mint": mint,
                    "amount_raw": str(cost_raw),
                    "usd": cost_raw / (10 ** USDC_DECIMALS),
                    "need_ui": need / (10 ** dec),
                    "have_ui": have / (10 ** dec),
                    "symbol": a.get("symbol") or mint[:6],
                })
                budget -= cost_raw
    elif not unfunded:
        # Full 50/50 rebalance: sell leftover surplus (per mint, net of the
        # funded strategies' needs and the SOL reserve) back to USDC.
        # Fires whenever no shortfall is pending, including when nothing is
        # open-eligible: idle surplus above the reserve is dead capital.
        # Mints already queued as dust are skipped: the dust path routes
        # them to owner review (swap_to_usdc); selling them here too would
        # double-handle the same balance.
        dust_mints = dust_mints or set()
        for mint, left in sorted(remaining.items()):
            # SOL is in RESERVED_MINTS (never dust), but its surplus above
            # the reserve is rebalanceable: spendable() already netted the
            # reserve out of `left`. Other reserved mints are owner holds.
            if mint == USDC_MINT or (mint in RESERVED_MINTS and mint != SOL_MINT) or mint in dust_mints:
                continue
            a = assets.get(mint)
            if not a or a["decimals"] <= 0 or a["price_usd"] <= 0 or left <= 0:
                continue
            ui = left / (10 ** a["decimals"])
            if ui * a["price_usd"] < SELL_SURPLUS_MIN_USD:
                continue
            prep_specs.append({
                "direction": "sell",
                "for_pool": None,
                "input_mint": mint,
                "output_mint": USDC_MINT,
                "amount_raw": str(left),
                "usd": ui * a["price_usd"],
                "need_ui": 0.0,
                "have_ui": ui,
                "symbol": a.get("symbol") or mint[:6],
            })

    return {
        "funded": funded,
        "unfunded": unfunded,
        "prep_swaps": prep_specs,
        "notes": notes,
        "wallet_path": wallet_path,
        "wallet_mtime": wallet_mtime,
        "sol_reserve_lamports": SOL_RESERVE_LAMPORTS,
    }


# --------------------------------------------------------------------------
# Prep swap gating (idempotency + loop guard)
# --------------------------------------------------------------------------
def _prep_swap_history(signals_dir: str) -> List[Dict[str, Any]]:
    """Prep swap history from the pending and processed queues.

    ``signals_dir`` may be either the signals root or its pending subdir
    (run_cycle passes George's pending dir); scan both layouts.
    """
    search_dirs = {signals_dir}
    parent = os.path.dirname(signals_dir.rstrip("/"))
    search_dirs.add(os.path.join(parent, "pending"))
    search_dirs.add(os.path.join(parent, "processed"))
    history = []
    for d in search_dirs:
        for path in glob.glob(os.path.join(d, "sheldon-prep-*.json")):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    sig = json.load(fh)
            except Exception:
                continue
            if sig.get("action") != "swap":
                continue
            history.append({
                "signal_id": sig.get("signal_id") or os.path.basename(path),
                "input_mint": sig.get("input_mint"),
                "output_mint": sig.get("output_mint"),
                "created_epoch": _created_epoch(sig.get("created_at") or ""),
                "path": path,
            })
    return history


def gate_prep_swaps(prep_specs: List[Dict[str, Any]], signals_dir: str,
                    wallet_mtime: float, now: Optional[float] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Filter prep swap specs through the rescan-wait and hourly loop guards.

    A spec is blocked when the newest prior prep swap for the same mint pair
    is newer than the wallet scan (rescan has not caught up yet), or when
    more than MAX_PREP_SWAPS_PER_MINT_PER_HOUR swaps for that pair were
    emitted in the last hour (loop not converging).
    """
    now = now if now is not None else time.time()
    history = _prep_swap_history(signals_dir)
    allowed: List[Dict[str, Any]] = []
    blocked: List[Dict[str, str]] = []

    scan_age = (now - wallet_mtime) if wallet_mtime else None
    if scan_age is None or scan_age > MAX_WALLET_SCAN_AGE_SECONDS:
        reason = (f"wallet scan stale or missing "
                  f"(age {scan_age:.0f}s > {MAX_WALLET_SCAN_AGE_SECONDS}s)"
                  if scan_age is not None else "no wallet scan mtime")
        for spec in prep_specs:
            blocked.append({"direction": spec["direction"],
                            "output_mint": spec["output_mint"],
                            "reason": reason})
        return allowed, blocked

    for spec in prep_specs:
        pair = (spec["input_mint"], spec["output_mint"])
        matches = [h for h in history
                   if (h["input_mint"], h["output_mint"]) == pair and h["created_epoch"] > 0]
        if matches:
            last = max(matches, key=lambda h: h["created_epoch"])
            if wallet_mtime < last["created_epoch"] + PREP_CONFIRM_GRACE_SECONDS:
                blocked.append({
                    "direction": spec["direction"],
                    "output_mint": spec["output_mint"],
                    "reason": (f"confirmation grace: wallet rescan must be taken "
                               f">={ PREP_CONFIRM_GRACE_SECONDS }s after last prep swap "
                               f"{last['signal_id']}"),
                })
                continue
            recent = [h for h in matches if h["created_epoch"] >= now - 3600]
            if len(recent) >= MAX_PREP_SWAPS_PER_MINT_PER_HOUR:
                blocked.append({
                    "direction": spec["direction"],
                    "output_mint": spec["output_mint"],
                    "reason": (f"loop guard: {len(recent)} prep swaps for this mint pair "
                               f"in the last hour"),
                })
                continue
        allowed.append(spec)
    return allowed, blocked


def build_prep_swap_signal(spec: Dict[str, Any], idx: int, base: int) -> Dict[str, Any]:
    """Build a George-schema 'swap' signal from a prep spec.

    ``amount`` is in the INPUT mint's base units (Jupiter ExactIn).
    """
    if spec["direction"] == "buy":
        reason = (
            f"CAPITAL PREP buy {spec['symbol']}: need {spec['need_ui']:.6f}, "
            f"have {spec['have_ui']:.6f} (SOL reserve "
            f"{SOL_RESERVE_LAMPORTS / 1e9:.4f} kept) for {spec['for_pool']}"
        )
    else:
        reason = (
            f"CAPITAL PREP sell {spec['symbol']} surplus {spec['have_ui']:.6f} "
            f"(~${spec['usd']:.2f}) back to USDC after funding opens"
        )
    return {
        "signal_id": f"sheldon-prep-{base}-{idx}",
        "action": "swap",
        "input_mint": spec["input_mint"],
        "output_mint": spec["output_mint"],
        "amount": str(spec["amount_raw"]),
        "max_slippage_bps": 100,
        "direction": spec["direction"],
        "prep_for_pool": spec.get("for_pool"),
        "reason": reason,
        "created_at": _utc_iso(),
    }
