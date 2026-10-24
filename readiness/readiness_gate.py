#!/usr/bin/env python3
"""Structured pre-open readiness gate for Sheldon (tokens + SOL).

Phase 1 of the 2026-10-09 agent update. A candidate is openable only when a
single readiness object reports *every* check true:

    close_settled, wallet_scan_fresh, position_scan_fresh, candidate_eligible,
    token_x_ready, token_y_ready, sol_ready, exposure_ready

Two behaviours this module owns:

* **Token readiness** (Change D) — required vs available for both pool sides,
  so a shortfall is explicit instead of implied by the funding walk.
* **SOL readiness** (Change B) — a real rent + fee + reserve model instead of
  the old "keep 0.02 SOL" spend cap, which never proved 0.02 was enough.
  For a SOL-paired pool, up to half the position capital is added.

When the *only* failing check is SOL and USDC remains after position funding,
``plan_sol_top_up`` returns a targeted USDC->SOL prep spec (Change C). It is
never emitted on a generic "SOL is low" basis.

Pure functions only: no RPC, no file I/O. Callers pass scan mtimes, wallet
assets and the close-settlement verdict. Fail closed: missing mtime, missing
required amounts or an unreadable scan all yield ``ready == False``.

SOL amounts in ``DEFAULT_SOL_POLICY`` are *placeholders*. Exact per-DEX
calibration from live simulation is host-only work (see 06-host-only-work.md,
item 5). Policy may override every component.
"""

import math
from typing import Any, Dict, List, Optional

from capital import SOL_MINT, USDC_MINT
from .funding import _required_amounts

LAMPORTS_PER_SOL = 1_000_000_000
USDC_DECIMALS = 6

# ---------------------------------------------------------------------------
# Readiness status codes (target status model, plan 02 -> Change A)
# ---------------------------------------------------------------------------
OPEN_READY = "OPEN_READY"
OPEN_BLOCKED_SOL = "OPEN_BLOCKED_SOL"
OPEN_BLOCKED_TOKEN_X = "OPEN_BLOCKED_TOKEN_X"
OPEN_BLOCKED_TOKEN_Y = "OPEN_BLOCKED_TOKEN_Y"
OPEN_BLOCKED_WALLET_SCAN_STALE = "OPEN_BLOCKED_WALLET_SCAN_STALE"
OPEN_BLOCKED_POSITION_SCAN_STALE = "OPEN_BLOCKED_POSITION_SCAN_STALE"
OPEN_BLOCKED_CLOSE_UNSETTLED = "OPEN_BLOCKED_CLOSE_UNSETTLED"
OPEN_BLOCKED_EXPOSURE = "OPEN_BLOCKED_EXPOSURE"
OPEN_BLOCKED_CANDIDATE_STALE = "OPEN_BLOCKED_CANDIDATE_STALE"

# Status precedence: the first failing check wins, so the reported status is
# the most fundamental blocker (a stale scan outranks a token shortfall).
_STATUS_ORDER = (
    ("candidate_eligible", OPEN_BLOCKED_CANDIDATE_STALE),
    ("wallet_scan_fresh", OPEN_BLOCKED_WALLET_SCAN_STALE),
    ("position_scan_fresh", OPEN_BLOCKED_POSITION_SCAN_STALE),
    ("close_settled", OPEN_BLOCKED_CLOSE_UNSETTLED),
    ("exposure_ready", OPEN_BLOCKED_EXPOSURE),
    ("token_x_ready", OPEN_BLOCKED_TOKEN_X),
    ("token_y_ready", OPEN_BLOCKED_TOKEN_Y),
    ("sol_ready", OPEN_BLOCKED_SOL),
)

# ---------------------------------------------------------------------------
# Defaults (overridable from sheldon_policy.json)
# ---------------------------------------------------------------------------
DEFAULT_READINESS_POLICY: Dict[str, Any] = {
    "require_fresh_wallet_scan": True,
    "require_fresh_position_scan": True,
    "require_both_tokens": True,
    "require_sol_readiness": True,
    "require_close_settlement": True,
    "max_wallet_scan_age_seconds": 900,
    "max_position_scan_age_seconds": 900,
}

# Per-DEX SOL components. position_rent / ata_rent are the placeholder
# calibration values; reserve + fees + headroom come from the top level.
DEFAULT_SOL_POLICY: Dict[str, Any] = {
    "reserve_lamports": 20_000_000,
    "auto_top_up_enabled": True,
    "auto_top_up_source_mint": "USDC",
    "max_top_up_usd": 10.0,
    "top_up_buffer_pct": 20.0,
    "min_post_top_up_lamports": 20_000_000,
    "per_dex": {
        "meteora": {
            "position_rent_lamports": 57_240_000,
            "ata_rent_lamports": 4_078_560,   # 2 x 2_039_280
            "tx_fee_lamports": 5_000,
            "priority_fee_lamports": 1_000_000,
        },
        "raydium": {
            "position_rent_lamports": 22_000_000,
            "ata_rent_lamports": 4_078_560,
            "tx_fee_lamports": 5_000,
            "priority_fee_lamports": 1_000_000,
        },
        "orca": {
            "position_rent_lamports": 15_000_000,
            "ata_rent_lamports": 4_078_560,
            "tx_fee_lamports": 5_000,
            "priority_fee_lamports": 1_000_000,
        },
        "default": {
            "position_rent_lamports": 25_000_000,
            "ata_rent_lamports": 4_078_560,
            "tx_fee_lamports": 5_000,
            "priority_fee_lamports": 1_000_000,
        },
    },
}

# ---------------------------------------------------------------------------
# Policy merging
# ---------------------------------------------------------------------------
def merge_readiness_policy(policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return defaults with any caller-provided overrides applied."""
    merged = dict(DEFAULT_READINESS_POLICY)
    for key, value in (policy or {}).items():
        if value is not None:
            merged[key] = value
    return merged

def merge_sol_policy(policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return defaults with overrides applied; per_dex merges key by key."""
    merged = dict(DEFAULT_SOL_POLICY)
    merged["per_dex"] = {
        dex: dict(components)
        for dex, components in DEFAULT_SOL_POLICY["per_dex"].items()
    }
    for key, value in (policy or {}).items():
        if key == "per_dex" and isinstance(value, dict):
            for dex, components in value.items():
                base = dict(merged["per_dex"].get(dex) or {})
                base.update(components or {})
                merged["per_dex"][dex] = base
        elif value is not None:
            merged[key] = value
    return merged

# ---------------------------------------------------------------------------
# Wallet asset helpers
# ---------------------------------------------------------------------------
def asset_index(assets: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """Index raw wallet-scan assets by mint."""
    index: Dict[str, Dict[str, Any]] = {}
    for a in assets or []:
        mint = (a.get("mint") or "").strip()
        if not mint:
            continue
        index[mint] = a
    return index

def _raw_amount(asset: Optional[Dict[str, Any]]) -> int:
    if not asset:
        return 0
    try:
        return max(0, int(asset.get("amount_raw") or 0))
    except (TypeError, ValueError):
        return 0

def _price(asset: Optional[Dict[str, Any]]) -> float:
    if not asset:
        return 0.0
    try:
        return float(asset.get("price_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0

def sol_price_usd(assets: Optional[List[Dict[str, Any]]]) -> float:
    """SOL price from the wallet scan; 0.0 when unavailable (fails closed)."""
    return _price(asset_index(assets).get(SOL_MINT))

def pool_sol_token(pool: Dict[str, Any]) -> bool:
    """True when SOL/WSOL is one of the pool's two sides."""
    for key in ("token_x_address", "token_y_address"):
        if (pool.get(key) or "").strip() == SOL_MINT:
            return True
    return False

# ---------------------------------------------------------------------------
# Change B — SOL requirement model
# ---------------------------------------------------------------------------
def required_sol_lamports(
    dex: Optional[str],
    pool: Optional[Dict[str, Any]],
    position_usd: float,
    sol_price: float,
    sol_policy: Optional[Dict[str, Any]] = None,
) -> int:
    """Lamports an open needs: rent + ATAs + fees + headroom + reserve.

    ``+ up to half the position capital`` when SOL is a pool token.
    """
    sp = merge_sol_policy(sol_policy)
    per_dex = sp["per_dex"]
    components = per_dex.get((dex or "").lower()) or per_dex["default"]
    total = (
        int(components.get("position_rent_lamports", 0))
        + int(components.get("ata_rent_lamports", 0))
        + int(components.get("tx_fee_lamports", 0))
        + int(components.get("priority_fee_lamports", 0))
        + int(sp.get("reserve_lamports", 0))
    )
    if pool_sol_token(pool or {}) and sol_price > 0 and position_usd > 0:
        total += int(math.ceil((position_usd / 2.0) / sol_price * LAMPORTS_PER_SOL))
    return total


def sol_overhead_lamports(
    dex: Optional[str],
    sol_policy: Optional[Dict[str, Any]] = None,
) -> int:
    """SOL held back for rent, ATAs, fees and reserve (no position capital).

    This is the floor ``plan_funding`` must leave unspent so the surplus-sell
    branch cannot liquidate the SOL an open needs.
    """
    return required_sol_lamports(dex, None, 0.0, 0.0, sol_policy)

# ---------------------------------------------------------------------------
# Change A + D — structured readiness object
# ---------------------------------------------------------------------------
def build_readiness(
    strategy: Dict[str, Any],
    *,
    assets: Optional[List[Dict[str, Any]]],
    wallet_mtime: float,
    position_mtime: float,
    now: float,
    funding_entry: Optional[Dict[str, Any]] = None,
    close_settled: bool = True,
    close_reason: Optional[str] = None,
    exposure_ready: bool = True,
    readiness_policy: Optional[Dict[str, Any]] = None,
    sol_policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the readiness object for one open candidate.

    ``funding_entry`` (from ``plan_funding``) supplies required/available
    amounts that already account for sequential consumption by higher-scored
    candidates; without it the requirement is recomputed from the pool and
    availability read straight from the wallet scan.
    """
    rp = merge_readiness_policy(readiness_policy)
    sp = merge_sol_policy(sol_policy)
    index = asset_index(assets)
    pool = strategy.get("_pool") or {}
    dex = (strategy.get("dex") or "unknown")
    position_usd = float(strategy.get("suggested_usdc") or 0.0)

    # --- required amounts -------------------------------------------------
    req = None
    if funding_entry and isinstance(funding_entry.get("required"), dict):
        required = funding_entry["required"]
        req = {
            "x_mint": required.get("x_mint"),
            "y_mint": required.get("y_mint"),
            "x_raw": int(required.get("x_raw") or 0),
            "y_raw": int(required.get("y_raw") or 0),
        }
    else:
        computed = _required_amounts(strategy)
        if computed is not None:
            req = {
                "x_mint": computed["x_mint"],
                "y_mint": computed["y_mint"],
                "x_raw": computed["x_raw"],
                "y_raw": computed["y_raw"],
            }

    # --- available amounts -------------------------------------------------
    if funding_entry and isinstance(funding_entry.get("have"), dict):
        have = funding_entry["have"]
        avail_x = int(have.get("x_raw") or 0)
        avail_y = int(have.get("y_raw") or 0)
    elif req is not None:
        avail_x = _raw_amount(index.get(req["x_mint"]))
        avail_y = _raw_amount(index.get(req["y_mint"]))
    else:
        avail_x = avail_y = 0

    # SOL availability is the *raw* balance: the requirement already
    # contains the reserve, so netting it out here would double-count.
    avail_sol = _raw_amount(index.get(SOL_MINT))
    sol_need = required_sol_lamports(
        dex, pool, position_usd, sol_price_usd(assets), sp)

    candidate_eligible = (
        req is not None
        and int(req["x_raw"]) > 0
        and int(req["y_raw"]) > 0
        and strategy.get("center") is not None
        and bool(strategy.get("bin_range"))
    )

    wallet_fresh = _fresh(wallet_mtime, rp["max_wallet_scan_age_seconds"], now) \
        if rp["require_fresh_wallet_scan"] else True
    wallet_ff = freshness_factor(wallet_mtime, rp["max_wallet_scan_age_seconds"], now) \
        if rp["require_fresh_wallet_scan"] else 1.0
    position_fresh = _fresh(position_mtime, rp["max_position_scan_age_seconds"], now) \
        if rp["require_fresh_position_scan"] else True
    close_ok = bool(close_settled) if rp["require_close_settlement"] else True
    tokens_required = bool(rp["require_both_tokens"])
    sol_required_flag = bool(rp["require_sol_readiness"])

    token_x_ready = (avail_x >= req["x_raw"]) if (req and tokens_required) else bool(req)
    token_y_ready = (avail_y >= req["y_raw"]) if (req and tokens_required) else bool(req)
    sol_ready = (avail_sol >= sol_need) if sol_required_flag else True

    checks = {
        "close_settled": bool(close_ok),
        "wallet_scan_fresh": bool(wallet_fresh),
        "position_scan_fresh": bool(position_fresh),
        "candidate_eligible": bool(candidate_eligible),
        "token_x_ready": bool(token_x_ready),
        "token_y_ready": bool(token_y_ready),
        "sol_ready": bool(sol_ready),
        "exposure_ready": bool(exposure_ready),
    }

    reasons: List[str] = []
    if not checks["candidate_eligible"]:
        reasons.append("candidate not eligible (missing pool enrichment or range)")
    if not checks["wallet_scan_fresh"]:
        reasons.append(_stale_reason("wallet scan", wallet_mtime, now,
                                     rp["max_wallet_scan_age_seconds"]))
    if not checks["position_scan_fresh"]:
        reasons.append(_stale_reason("position scan", position_mtime, now,
                                     rp["max_position_scan_age_seconds"]))
    if not checks["close_settled"]:
        reasons.append(f"close unsettled: {close_reason}" if close_reason
                       else "close unsettled")
    if not checks["exposure_ready"]:
        reasons.append("exposure rail not satisfied")
    if req is not None and not checks["token_x_ready"]:
        reasons.append(f"token_x short by {req['x_raw'] - avail_x} raw")
    if req is not None and not checks["token_y_ready"]:
        reasons.append(f"token_y short by {req['y_raw'] - avail_y} raw")
    if not checks["sol_ready"]:
        reasons.append(
            f"SOL short by {max(0, sol_need - avail_sol)} lamports "
            f"(need {sol_need}, have {avail_sol})"
        )

    status = OPEN_READY
    for check, code in _STATUS_ORDER:
        if not checks[check]:
            status = code
            break

    return {
        "pool_address": strategy.get("pool_address"),
        "dex": dex,
        "ready": all(checks.values()),
        "status": status,
        "checks": checks,
        "blocking_reasons": reasons,
        "required": {
            "x_mint": req["x_mint"] if req else None,
            "y_mint": req["y_mint"] if req else None,
            "token_x_raw": req["x_raw"] if req else None,
            "token_y_raw": req["y_raw"] if req else None,
            "sol_lamports": sol_need,
        },
        "available": {
            "token_x_raw": avail_x,
            "token_y_raw": avail_y,
            "sol_lamports": avail_sol,
        },
        "sol_is_pool_token": pool_sol_token(pool),
        "wallet_freshness_factor": wallet_ff,
    }

def _fresh(mtime: float, max_age_seconds: float, now: float) -> bool:
    """Fail closed: a missing/zero mtime is never fresh."""
    try:
        mtime = float(mtime)
    except (TypeError, ValueError):
        return False
    if mtime <= 0:
        return False
    return (now - mtime) <= float(max_age_seconds)


def freshness_factor(mtime: float, max_age_seconds: float, now: float) -> float:
    """Return a freshness factor in [0.0, 1.0].

    - 1.0: scan is fresh (within grace period)
    - 0.0: scan is too old (beyond max age), hard block
    - Between: proportional soft penalty - the scan is acceptable but
      positions will be proportionally smaller to reflect the risk.
    """
    try:
        mtime = float(mtime)
    except (TypeError, ValueError):
        return 0.0
    if mtime <= 0:
        return 0.0
    age = now - mtime
    if age <= 0:
        return 1.0
    max_age = float(max_age_seconds)
    if age >= max_age:
        return 0.0
    # Proportional factor: 1.0 at 0 age, 0.0 at max age
    return max(0.0, 1.0 - age / max_age)

def _stale_reason(label: str, mtime: float, now: float, max_age: float) -> str:
    if not mtime:
        return f"{label} missing"
    return f"{label} stale (age {now - mtime:.0f}s > {max_age:.0f}s)"

# ---------------------------------------------------------------------------
# Change C — SOL top-up planning
# ---------------------------------------------------------------------------
def plan_sol_top_up(
    readiness: Dict[str, Any],
    *,
    assets: Optional[List[Dict[str, Any]]],
    required_usdc_raw: int = 0,
    sol_policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Plan a targeted USDC->SOL prep for an otherwise-valid open.

    Returns ``{"allowed": bool, "spec": dict|None, "reason": str|None,
    "cost_usd": float}``. Allowed only when SOL is the *sole* failing check,
    USDC remains after the position's USDC side is preserved, and the cost
    fits ``max_top_up_usd``.
    """
    sp = merge_sol_policy(sol_policy)
    blocked = {"allowed": False, "spec": None, "reason": None, "cost_usd": 0.0}

    if readiness.get("ready"):
        return blocked
    checks = readiness.get("checks") or {}
    for name, ok in checks.items():
        if name == "sol_ready":
            continue
        if not ok:
            return blocked
    if checks.get("sol_ready"):
        return blocked
    if not sp.get("auto_top_up_enabled"):
        return {**blocked, "reason": "auto SOL top-up disabled by policy"}

    price = sol_price_usd(assets)
    if price <= 0:
        return {**blocked, "reason": "SOL price unavailable; cannot size top-up"}

    need = int((readiness.get("required") or {}).get("sol_lamports") or 0)
    have = int((readiness.get("available") or {}).get("sol_lamports") or 0)
    deficit = max(0, need - have)
    if deficit <= 0:
        return blocked

    buffer_pct = float(sp.get("top_up_buffer_pct") or 0.0)
    top_up_lamports = int(math.ceil(deficit * (1.0 + buffer_pct / 100.0)))
    floor = int(sp.get("min_post_top_up_lamports") or 0)
    if have + top_up_lamports < floor:
        top_up_lamports = floor - have

    cost_usd = top_up_lamports / LAMPORTS_PER_SOL * price
    max_usd = float(sp.get("max_top_up_usd") or 0.0)
    if cost_usd > max_usd:
        return {
            **blocked,
            "cost_usd": cost_usd,
            "reason": (f"SOL top-up ${cost_usd:.2f} exceeds "
                       f"max_top_up_usd ${max_usd:.2f}"),
        }

    # Amount is denominated in the INPUT mint (USDC), Jupiter ExactIn.
    usdc_raw = int(math.ceil(top_up_lamports / LAMPORTS_PER_SOL
                             * price * (10 ** USDC_DECIMALS)))
    if usdc_raw <= 0:
        return blocked

    index = asset_index(assets)
    usdc_avail = _raw_amount(index.get(USDC_MINT))
    usdc_budget = usdc_avail - max(0, int(required_usdc_raw or 0))
    if usdc_raw > usdc_budget:
        return {
            **blocked,
            "cost_usd": cost_usd,
            "reason": (f"SOL top-up needs {usdc_raw} USDC raw, only "
                       f"{max(0, usdc_budget)} left after position funding"),
        }

    spec = {
        "direction": "buy",
        "for_pool": readiness.get("pool_address"),
        "input_mint": USDC_MINT,
        "output_mint": SOL_MINT,
        "amount_raw": str(usdc_raw),
        "usd": usdc_raw / (10 ** USDC_DECIMALS),
        "need_ui": need / LAMPORTS_PER_SOL,
        "have_ui": have / LAMPORTS_PER_SOL,
        "symbol": "SOL",
        "readiness_dependency": "sol",
    }
    return {"allowed": True, "spec": spec, "reason": None, "cost_usd": cost_usd}

__all__ = [
    "OPEN_READY",
    "OPEN_BLOCKED_SOL",
    "OPEN_BLOCKED_TOKEN_X",
    "OPEN_BLOCKED_TOKEN_Y",
    "OPEN_BLOCKED_WALLET_SCAN_STALE",
    "OPEN_BLOCKED_POSITION_SCAN_STALE",
    "OPEN_BLOCKED_CLOSE_UNSETTLED",
    "OPEN_BLOCKED_EXPOSURE",
    "OPEN_BLOCKED_CANDIDATE_STALE",
    "DEFAULT_READINESS_POLICY",
    "DEFAULT_SOL_POLICY",
    "merge_readiness_policy",
    "merge_sol_policy",
    "asset_index",
    "sol_price_usd",
    "pool_sol_token",
    "required_sol_lamports",
    "sol_overhead_lamports",
    "build_readiness",
    "plan_sol_top_up",
]
