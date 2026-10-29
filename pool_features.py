#!/usr/bin/env python3
"""Missy ``pool_features_version`` v1 consumer.

Two pure, deterministic responsibilities:

  1. ``gate_v1`` — answer "may Sheldon open this pool at all?" using only
     Missy's safety block (``safety.eligible`` / ``reject_codes``) plus
     ``data_quality.range_ready``.  No score is read.
  2. ``compute_net_yield`` — answer "which survivor earns most per dollar?"
     using Missy's explicit fee-capture / in-range / slippage inputs.

Both are gated by ``sheldon_policy.json``:

    "pool_gate":   {"enabled": false, ...}
    "pool_ranker": {"enabled": false, ...}

Flags default to false, so a v1 scan keeps today's score-gated behaviour
until the owner flips them.  v0 records (no ``pool_features_version`` key)
never enter either function.

Contract: /Users/manthalib/Missy-agent/docs/missy_sheldon_contract_v1.md
Schema:   /Users/manthalib/Missy-agent/docs/pool_features_v1.md
"""

import json
import os
from typing import Any, Dict, Optional

_POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "sheldon_policy.json")

# Defaults mirror the task contract.  ``enabled`` is the only switch the
# rollout should need to flip.
DEFAULT_GATE = {
    "version": 1,
    "enabled": False,
    "require_range_ready": True,
    "v0_fallback": "legacy",
}

DEFAULT_RANKER = {
    "version": 1,
    "enabled": False,
    "hold_days": 3.0,
    "variance_divisor": 800.0,
    "max_il_pct": 5.0,
    "il_policy": "skip",          # skip | demote
    "slippage": {"round_trip": True, "size_usd": 100},
    "high_net_daily_pct": 0.10,
    "tie_break": "pool_address",
    "concentration_min": 0.25,
    "concentration_max": 4.0,
    "unknown_rank": "last",       # last | skip
    "min_net_daily_pct": 0.0,
    "enforce_min_net": False,
}


def _deep_merge(base: Dict[str, Any], override: Any) -> Dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    if not isinstance(override, dict):
        return out
    for key, val in override.items():
        if key.startswith("_"):
            continue
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _policy_file() -> Dict[str, Any]:
    try:
        with open(_POLICY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def gate_policy() -> Dict[str, Any]:
    """Return the merged ``pool_gate`` block (defaults + manifest)."""
    return _deep_merge(DEFAULT_GATE, _policy_file().get("pool_gate"))


def ranker_policy() -> Dict[str, Any]:
    """Return the merged ``pool_ranker`` block (defaults + manifest)."""
    return _deep_merge(DEFAULT_RANKER, _policy_file().get("pool_ranker"))


def scoring_contract_version() -> int:
    """Policy-level contract version (0 = legacy, 1 = pool_features v1)."""
    try:
        return int((_policy_file().get("scoring") or {})
                   .get("contract_version") or 0)
    except (TypeError, ValueError):
        return 0


def contract_enabled() -> bool:
    """True when the manifest still consumes pool_features v1."""
    return scoring_contract_version() >= 1


# --------------------------------------------------------------------------
# Version handling
# --------------------------------------------------------------------------
def pool_features_version(pool: Dict[str, Any]) -> int:
    """Return the record's ``pool_features_version``; absent/malformed = 0."""
    if not isinstance(pool, dict):
        return 0
    try:
        return int(pool.get("pool_features_version") or 0)
    except (TypeError, ValueError):
        return 0


def is_v1(pool: Dict[str, Any]) -> bool:
    return pool_features_version(pool) >= 1


def gate_active(pool: Dict[str, Any], cfg: Optional[dict] = None) -> bool:
    """True only when v1 consumption is enabled and the record is v1.

    ``scoring.contract_version < 1`` is the documented rollback branch: it
    keeps the legacy score stack even if a flag was left on.
    """
    cfg = cfg if cfg is not None else gate_policy()
    return (is_v1(pool) and bool(cfg.get("enabled"))
            and contract_enabled())


def ranker_active(pool: Dict[str, Any], cfg: Optional[dict] = None) -> bool:
    cfg = cfg if cfg is not None else ranker_policy()
    return (is_v1(pool) and bool(cfg.get("enabled"))
            and contract_enabled())


# --------------------------------------------------------------------------
# Gate — safety only
# --------------------------------------------------------------------------
def gate_v1(pool: Dict[str, Any], require_range_ready: bool = True) -> Dict[str, Any]:
    """Return the v1 open gate decision.

    ``ok`` is True only when Missy's safety block is eligible and (when
    required) the record can take a live range.  The score is never read.

    The result carries the audit trail (``reject_codes`` and
    ``thresholds_used``) so every decision can be attributed to Missy's
    policy rather than to a Sheldon bug.
    """
    safety = pool.get("safety") or {}
    reject_codes = list(safety.get("reject_codes") or [])
    eligible = safety.get("eligible")
    if eligible is None:
        # v1 records always carry safety.eligible; fall back to the legacy
        # boolean only so a partially-written record cannot crash the gate.
        eligible = pool.get("eligible")

    thresholds = safety.get("thresholds_used") or {}
    data_quality = pool.get("data_quality") or {}
    range_ready = data_quality.get("range_ready")

    result = {
        "ok": False,
        "reject_codes": reject_codes,
        "thresholds_used": thresholds,
        "range_ready": range_ready,
        "rejected_reason": pool.get("rejected_reason") or "",
    }

    if eligible is not True:
        if reject_codes:
            result["reason"] = "Missy safety: " + ", ".join(reject_codes)
        else:
            result["reason"] = (
                "Missy safety: " + (pool.get("rejected_reason") or "eligible=false"))
        return result

    if require_range_ready and range_ready is not True:
        result["reason"] = "no live tick/bin (Missy range_ready=false)"
        return result

    result["ok"] = True
    result["reason"] = "Missy safety: eligible"
    return result


# --------------------------------------------------------------------------
# Ranker — expected net fee yield
# --------------------------------------------------------------------------
def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def deployed_half_width_pct(pool: Dict[str, Any],
                            override: Optional[float] = None) -> Optional[float]:
    """Return the half-width (percent per side) Sheldon will actually open.

    Mirrors ``strategy.range.adaptive_half_width`` in price space.  Missy's
    ``reference_half_width_pct`` is the same band, so the concentration
    multiplier is 1.0 when the two agree.  An explicit ``override`` is used
    by calibration/tests and by the optional policy override.
    """
    if override is not None:
        return _as_float(override)

    from strategy.range._7d_volatility_based import adaptive_half_width

    half_units = adaptive_half_width(pool)
    dex = (pool.get("dex") or "").lower()
    if dex == "meteora":
        try:
            bin_step = int(pool.get("bin_step") or 0)
        except (TypeError, ValueError):
            bin_step = 0
        if bin_step <= 0:
            return None
        step_ratio = 1.0 + bin_step / 10000.0
    else:
        step_ratio = 1.0001
    if step_ratio <= 1.0:
        return None
    return (step_ratio ** int(half_units) - 1.0) * 100.0


def _slippage_bps(pool: Dict[str, Any], cfg: Dict[str, Any]) -> tuple:
    """Return (bps, known) for the configured screening size.

    Missy emits a table at $100/$1k plus ``depth_proxy_usd`` for any other
    size.  A null table value is "unknown" (logged), not a silent zero.
    """
    slip_cfg = cfg.get("slippage") or {}
    size = _as_float(slip_cfg.get("size_usd", 100)) or 100.0
    table = pool.get("slippage_estimate_bps")
    if not isinstance(table, dict):
        table = {}

    for key in (str(int(size)), str(size), f"{size:g}"):
        if key in table:
            value = _as_float(table.get(key))
            if value is not None:
                return value, True

    depth = _as_float((pool.get("slippage_model") or {}).get("depth_proxy_usd"))
    if depth is not None and depth > 0:
        return 1e4 * size / depth, True

    return 0.0, False


def compute_net_yield(pool: Dict[str, Any], cfg: Optional[dict] = None,
                      deployed_width_pct: Optional[float] = None) -> Dict[str, Any]:
    """Compute expected net fee yield per day (percent).

    Formula (task contract §4.2):

        apr_daily_pct   = realized_fee_apr / 365
        in_range        = expected_in_range_frac
        vol             = expected_in_range.vol_daily_pct
        ref_width       = expected_in_range.reference_half_width_pct
        conc            = clamp(deployed_width / ref_width, 0.25, 4.0)
        gross           = apr_daily_pct * in_range
        variance_drag   = conc * vol**2 / 800
        slippage_drag   = (2 * slip_bps / 100) / hold_days
        net             = gross - variance_drag - slippage_drag

    ``il_estimate_pct`` is a guard (``max_il_pct``), never a subtractive
    term: the variance term already prices range risk.
    """
    cfg = cfg if cfg is not None else ranker_policy()

    fee_capture = pool.get("fee_capture") or {}
    apr = _as_float(fee_capture.get("realized_fee_apr"))
    if apr is None:
        apr = _as_float(pool.get("realized_fee_apr"))

    in_range = _as_float(pool.get("expected_in_range_frac"))
    expected = pool.get("expected_in_range") or {}
    vol = _as_float(expected.get("vol_daily_pct"))
    ref_width = _as_float(expected.get("reference_half_width_pct"))

    deployed = _as_float(deployed_width_pct)
    if deployed is None:
        deployed = _as_float(cfg.get("deployed_half_width_pct"))
    deployed_known = deployed is not None
    if deployed is None:
        deployed = deployed_half_width_pct(pool)
        deployed_known = deployed is not None
    if deployed is None:
        # Fail-closed but honest: compare against Missy's own band (conc=1)
        # and record that Sheldon could not derive its band.
        deployed = ref_width

    conc = None
    if deployed is not None and ref_width is not None and ref_width > 0:
        conc = _clamp(
            deployed / ref_width,
            float(cfg.get("concentration_min", 0.25)),
            float(cfg.get("concentration_max", 4.0)),
        )

    slip_bps, slip_known = _slippage_bps(pool, cfg)
    hold_days = _as_float(cfg.get("hold_days")) or 3.0
    variance_divisor = _as_float(cfg.get("variance_divisor")) or 800.0
    round_trip = 2.0 if (cfg.get("slippage") or {}).get("round_trip", True) else 1.0

    il = _as_float(pool.get("il_estimate_pct"))
    max_il = _as_float(cfg.get("max_il_pct"))
    il_guard = "unknown"
    il_ok = True
    if il is not None and max_il is not None:
        if il <= max_il:
            il_guard = "ok"
        else:
            il_guard = str(cfg.get("il_policy") or "skip").lower()
            il_ok = il_guard != "skip"
    elif il is not None:
        il_guard = "ok"

    inputs = {
        "realized_fee_apr": apr,
        "apr_daily_pct": None if apr is None else apr / 365.0,
        "expected_in_range_frac": in_range,
        "vol_daily_pct": vol,
        "reference_half_width_pct": ref_width,
        "deployed_half_width_pct": deployed,
        "deployed_width_known": deployed_known,
        "concentration": conc,
        "slippage_bps": slip_bps,
        "slippage_known": slip_known,
        "hold_days": hold_days,
        "variance_divisor": variance_divisor,
        "il_estimate_pct": il,
        "max_il_pct": max_il,
        "il_guard": il_guard,
        "il_ok": il_ok,
    }

    net = None
    rankable = False
    status = "ranked"
    if apr is None or in_range is None or vol is None or ref_width is None:
        status = "unknown_inputs"
    else:
        gross = (apr / 365.0) * in_range
        variance_drag = (conc or 1.0) * (vol ** 2) / variance_divisor
        slippage_drag = (round_trip * slip_bps / 100.0) / hold_days
        net = gross - variance_drag - slippage_drag
        inputs["gross_daily_pct"] = gross
        inputs["variance_drag_pct"] = variance_drag
        inputs["slippage_drag_pct"] = slippage_drag
        if not il_ok:
            status = "il_skip"
        elif il_guard == "demote":
            status = "il_demote"
            rankable = True
        else:
            rankable = True

    # Optional owner-requested floor.  Off by default: prefer ranking every
    # survivor and letting MAX_POSITION_OPEN_PER_CYCLE cut.
    if (rankable and cfg.get("enforce_min_net")
            and net is not None
            and net < float(cfg.get("min_net_daily_pct") or 0.0)):
        status = "below_min_net"
        rankable = False

    return {
        "net_daily_pct": net,
        "rankable": rankable,
        "status": status,
        "inputs": inputs,
    }


def rank_key(candidate: Dict[str, Any], cfg: Optional[dict] = None,
             metrics: Optional[dict] = None) -> tuple:
    """Deterministic sort key for v1 candidates: (-net, pool_address).

    Unrankable records sort last (or are dropped by the caller when
    ``unknown_rank == "skip"``); v0 records keep legacy score ordering and
    are never ranked as if they had a v1 net yield.
    """
    cfg = cfg if cfg is not None else ranker_policy()
    pool = candidate.get("_pool") or {}
    addr = candidate.get("pool_address") or ""

    if not is_v1(pool):
        return (1, -(float(candidate.get("score") or 0.0)), addr)

    m = metrics if metrics is not None else compute_net_yield(pool, cfg)
    if not m.get("rankable"):
        return (2, 0.0, addr)
    level = 1 if m.get("status") == "il_demote" else 0
    return (level, -(float(m.get("net_daily_pct") or 0.0)), addr)


# --------------------------------------------------------------------------
# Phase-B shadow diff (report-only; never changes emitted signals)
# --------------------------------------------------------------------------
def shadow_report(pools, gate_cfg: Optional[dict] = None,
                  ranker_cfg: Optional[dict] = None,
                  min_open_score: float = 90.0,
                  top_n: int = 10,
                  inversion_n: int = 3) -> Dict[str, Any]:
    """Return the Phase-B diff for a v1 scan.

    The caller computes this while the live flags stay off and attaches it to
    the cycle report; signals remain the legacy ones.  Every flip carries
    either Missy's reject codes or the deployability reason so a new candidate
    can never be mistaken for a Sheldon regression.
    """
    gate_cfg = gate_cfg if gate_cfg is not None else gate_policy()
    ranker_cfg = ranker_cfg if ranker_cfg is not None else ranker_policy()
    v1 = [p for p in pools or [] if is_v1(p)]
    require_range = bool(gate_cfg.get("require_range_ready", True))

    legacy_open = [
        p for p in v1
        if p.get("eligible") is not False
        and float(p.get("score") or 0.0) >= float(min_open_score)
    ]
    decisions = {p.get("pool_address"): gate_v1(p, require_range) for p in v1}
    safety = [p for p in v1
              if (p.get("safety") or {}).get("eligible") is True]
    kept = [p for p in safety if decisions[p.get("pool_address")]["ok"]]
    kept_addrs = {p.get("pool_address") for p in kept}
    legacy_addrs = {p.get("pool_address") for p in legacy_open}

    skipped = []
    for p in v1:
        d = decisions[p.get("pool_address")]
        if d["ok"]:
            continue
        skipped.append({
            "pool_address": p.get("pool_address"),
            "name": p.get("name"),
            "reject_codes": d["reject_codes"],
            "reason": d["reason"],
        })

    flips_in = []
    for p in kept:
        if p.get("pool_address") in legacy_addrs:
            continue
        metrics = (p.get("safety") or {}).get("metrics") or {}
        why = "score_bypassed"
        if float(metrics.get("tvl") or 0.0) < 25_000.0:
            why = "low_tvl_floor_lowered"
        elif float(metrics.get("volume_window") or 0.0) < 5_000.0:
            why = "low_volume_floor_lowered"
        elif float(metrics.get("fee_tvl_ratio") or 0.0) < 0.02:
            why = "low_fee_tvl_floor_lowered"
        flips_in.append({"pool_address": p.get("pool_address"),
                         "from": "score", "why": why})

    flips_out = []
    for p in legacy_open:
        if p.get("pool_address") in kept_addrs:
            continue
        d = decisions[p.get("pool_address")]
        flips_out.append({
            "pool_address": p.get("pool_address"),
            "reject_codes": d["reject_codes"],
            "why": ("range_ready=false" if not d["reject_codes"]
                    else ", ".join(d["reject_codes"])),
        })

    measured = []
    for p in v1:
        m = compute_net_yield(p, ranker_cfg)
        if m.get("rankable"):
            measured.append((p, m))
    by_score = sorted(
        v1, key=lambda p: (-(float(p.get("score") or 0.0)),
                           p.get("pool_address") or ""))[:top_n]
    by_net = sorted(
        measured,
        key=lambda x: (-x[1]["net_daily_pct"], x[0].get("pool_address") or ""),
    )[:top_n]
    score_rank = {
        p.get("pool_address"): i for i, p in enumerate(sorted(
            v1, key=lambda p: (-(float(p.get("score") or 0.0)),
                               p.get("pool_address") or "")), 1)
    }
    net_rank = {
        p.get("pool_address"): i for i, (p, _) in enumerate(sorted(
            measured,
            key=lambda x: (-x[1]["net_daily_pct"],
                           x[0].get("pool_address") or "")), 1)
    }
    inversions = []
    for p, m in measured:
        addr = p.get("pool_address")
        delta = score_rank[addr] - net_rank[addr]
        inversions.append((delta, p, m))
    inversions.sort(key=lambda x: (-abs(x[0]), x[1].get("pool_address") or ""))

    return {
        "contract_version": scoring_contract_version(),
        "gate": {
            "v1_records": len(v1),
            "legacy_open": len(legacy_open),
            "safety_eligible": len(safety),
            "safety_ready": len(kept),
            "kept": sorted(kept_addrs),
            "skipped": skipped,
            "flips_in": flips_in,
            "flips_out": flips_out,
        },
        "ranker": {
            "top_by_old_score": [p.get("pool_address") for p in by_score],
            "top_by_net_daily_pct": [p.get("pool_address") for p, _ in by_net],
            "inversions": [
                {
                    "pool_address": p.get("pool_address"),
                    "score_rank": score_rank[p.get("pool_address")],
                    "net_rank": net_rank[p.get("pool_address")],
                    "inputs": m.get("inputs"),
                }
                for _, p, m in inversions[:inversion_n]
            ],
        },
    }
