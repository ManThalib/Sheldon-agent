"""Signal building and writing logic extracted from run_cycle.py.

Contains:
  - _build_open_signal: build a George-schema 'open' signal from a strategy and verdict
  - _build_dust_swap_signal: build a dust swap signal
  - write_signals: convert verdicts into George-schema signal files

Import build_prep_swap_signal from readiness module for prep swap signal building.
"""

from pathlib import Path
import json
import os
import time
from datetime import datetime, timedelta, timezone

# Import prep swap signal builder from readiness module
from readiness import build_prep_swap_signal
from readiness import prep_ledger
from readiness import prep_ledger
import pool_features
from run_cycle.close_settlement import (
    close_emit_suppressions,
    position_close_pending,
    load_close_state,
)


def _utc_iso():
    """Return current time in Asia/Shanghai (UTC+8) ISO format."""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _build_open_signal(strategy: dict, v: dict, pool: dict, idx: int, base: int,
                       min_score: float, supported_dexes: set) -> dict:
    """Build a George-schema 'open' signal from a strategy and verdict.

    Returns signal dict or None if signal cannot be built.
    """
    dex = v.get("dex") or "unknown"
    if dex not in supported_dexes or not pool:
        return None

    # George enforces bin_step rail fail-closed
    from run_cycle.gates import _meteora_bin_step_allowed
    bin_ok, _reason = _meteora_bin_step_allowed(pool, dex)
    if not bin_ok:
        return None

    # Score must be present and at/above the policy floor — except for v1
    # records when the safety gate is enabled: there the gate verdict is
    # safety.eligible + range_ready, and the score is audit-only.
    gate_cfg = pool_features.gate_policy()
    if pool_features.gate_active(pool, gate_cfg):
        if not pool_features.gate_v1(
                pool, bool(gate_cfg.get("require_range_ready", True)))["ok"]:
            return None
        score = v.get("score")
    else:
        score = v.get("score")
        if score is None or float(score) < min_score:
            return None

    center = strategy.get("center")
    if center is None:
        return None

    # Pull prices and decimals.
    px_x = float(pool.get("token_x_price_usd") or 0.0)
    px_y = float(pool.get("token_y_price_usd") or 0.0)
    dec_x = int(pool.get("token_x_decimals") or 0)
    dec_y = int(pool.get("token_y_decimals") or 0)
    if px_x <= 0 or px_y <= 0 or dec_x <= 0 or dec_y <= 0:
        return None

    position_usd = strategy.get("suggested_usdc", 0.0)
    from run_cycle.gates import MIN_POSITION_USD
    if position_usd < MIN_POSITION_USD:
        return None

    # 50/50 USD split.
    half_usd = position_usd / 2.0
    amount_x = int((half_usd / px_x) * (10 ** dec_x))
    amount_y = int((half_usd / px_y) * (10 ** dec_y))
    if amount_x <= 0 or amount_y <= 0:
        return None

    bin_range = strategy.get("bin_range", {})
    if not bin_range:
        return None

    return {
        "signal_id": f"sheldon-{base}-{idx}",
        "action": "open",
        "dex": dex,
        "wallet_id": "main",
        "pool_address": pool.get("pool_address"),
        "side": "bidirectional",
        "bin_range": {"lower": bin_range.get("lower", 0), "upper": bin_range.get("upper", 0)},
        "liquidity": {
            "amount_x": str(amount_x),
            "amount_y": str(amount_y),
        },
        "position_usd": position_usd,
        "strategy_type": strategy.get("strategy_type", "Spot"),
        "score": float(score or 0.0),
        "score_policy": {"source": "missy", "version": 1, "min_open_score": min_score},
        "max_slippage_bps": 100,
        "reason": (
            f"OPEN score={v.get('score')} threshold={min_score} dex={dex} "
            f"position_usd={position_usd:.2f} center={strategy.get('center')}"
        ),
        "created_at": _utc_iso(),
    }


def _build_dust_swap_signal(asset: dict, idx: int, base: int) -> dict:
    return {
        "signal_id": f"sheldon-{base}-{idx}",
        "action": "swap_to_usdc",
        "wallet_id": "main",
        "mint": asset["mint"],
        "symbol": asset["symbol"],
        "decimals": asset["decimals"],
        "amount_raw": asset["amount_raw"],
        "amount_ui": asset["amount_ui"],
        "value_usd": asset["value_usd"],
        "reason": (
            f"dust token: {asset.get('symbol') or 'unknown'} valued "
            f"${asset['value_usd']:.2f} (threshold ${asset.get('dust_threshold_usd', 1)})"
        ),
        "created_at": _utc_iso(),
    }


def write_signals(report: dict, signals_dir: str, positions_dir: str,
                  min_open_score: float = 70.0,
                  min_position_usd: float = 20.0,
                  supported_dexes: set = None,
                  state_dir: str = None,
                  journal_dir: str = None,
                  close_emission: dict = None) -> tuple:
    """Convert verdicts into George-schema signal files.

    Returns:
        (created_paths, review_items)
        created_paths: list of files written to George's pending queue.
        review_items: list of items that need human review.

    When ``state_dir`` is set, each emitted prep swap is recorded in the prep
    ledger so the next cycle can suppress a duplicate by state, not by clock.
    Duplicate-close ghost guards (2026-10-16) are also settlement-state
    derived and therefore active only when ``state_dir`` is provided: the
    close family (CLOSE/REBALANCE/ROTATE) is suppressed while the position
    scan is stale, while the pool settled a close after the scan was taken,
    or while a close for the same position is already in flight. Claims
    (COLLECT_FEES) are never guarded — nothing here can block a close or a
    claim; these rails only defer Sheldon's automated emission.
    """
    if supported_dexes is None:
        supported_dexes = {"meteora", "raydium", "orca"}

    os.makedirs(signals_dir, exist_ok=True)
    active = _load_active_positions(positions_dir)
    created = []
    review = []

    # Duplicate-close ghost guards: computed once per call from the
    # settlement state and the position scan mtime (state_dir-gated).
    close_state = load_close_state(state_dir) if state_dir else {}
    close_global_reason = None
    close_pool_reasons: dict = {}
    if state_dir:
        close_global_reason, close_pool_reasons = close_emit_suppressions(
            close_state, _position_scan_mtime(positions_dir),
            policy={"close_emission": close_emission or {}})

    # Dedup/center-guard drops from main() surface as review items so the
    # report explains why a high-scoring pool produced no signal.
    for item in report.get("open_skipped", []):
        review.append({
            "pool_address": item.get("pool_address"),
            "dex": item.get("dex"),
            "score": item.get("score"),
            "evidence": None,
            "reason": item.get("reason", "open candidate skipped"),
        })

    base = int(time.time())
    idx = 1

    wallet = report.get("wallet") or {}
    strategies = {s["pool_address"]: s for s in report.get("strategies", [])}

    # 0. Capital prep swaps for the highest-scored unfunded strategy.
    # Gate before writing: no re-emit while the wallet rescan has not yet
    # caught up, and a hard stop when the loop stops converging.
    funding = report.get("funding")
    if funding is None:
        # Fail safe: without a funding plan, refuse to open blind.
        funding = {
            "funded": [],
            "unfunded": [
                {"pool_address": s.get("pool_address"),
                 "reason": "funding plan missing; refusing to open blind"}
                for s in report.get("strategies", [])
            ],
            "prep_swaps_allowed": [],
            "prep_swaps_blocked": [],
            "notes": [],
        }
    allowed_preps = funding.get("prep_swaps_allowed") or []

    # 0a. Blocked prep swaps surface as review items, never as signals.
    for b in funding.get("prep_swaps_blocked") or []:
        review.append({
            "pool_address": b.get("for_pool"),
            "dex": "jupiter",
            "score": None,
            "evidence": None,
            "reason": f"prep swap {b.get('direction')} blocked: {b.get('reason')}",
        })

    # Sizing-gate filter for prep targets. plan_funding() runs on the
    # pre-split strategy list, so an allowed prep can otherwise pay swap
    # costs for an open the split/readiness/sizing gates then refuse
    # (live 2026-10-24: 3 BUYs + 1 surplus sell for an open whose suggested
    # size never cleared the $20 floor). A prep is only worth a transaction
    # when its target pool has an OPEN_CANDIDATE that passes every open
    # gate this cycle; the open itself may still wait a cycle for the prep
    # to land and the wallet to rescan — that ordering is correct.
    from run_cycle.gates import MIN_POSITION_USD as _MIN_POS_USD
    sized_pools = set()
    gate_cfg = pool_features.gate_policy()
    require_range_ready = bool(gate_cfg.get("require_range_ready", True))
    for v in report.get("verdicts", []):
        if v.get("action") != "OPEN_CANDIDATE":
            continue
        pool_addr = v.get("pool_address")
        dex = v.get("dex") or "unknown"
        strategy = strategies.get(pool_addr) or {}
        pool = v.get("_pool") or {}
        if pool_features.gate_active(pool, gate_cfg):
            # v1: the same safety gate the open filter used. No score read.
            score_ok = pool_features.gate_v1(
                pool, require_range_ready)["ok"]
        else:
            try:
                score_ok = (v.get("score") is not None
                            and float(v["score"]) >= min_open_score)
            except (TypeError, ValueError):
                score_ok = False
        if not (pool_addr in strategies and dex in supported_dexes
                and score_ok
                and strategy.get("center") is not None
                and strategy.get("bin_range")
                and float(strategy.get("suggested_usdc") or 0.0) >= _MIN_POS_USD):
            continue
        sized_pools.add(pool_addr)

    # 0b. Allowed prep swaps execute before any position signals.
    for spec in allowed_preps:
        if spec.get("for_pool") not in sized_pools:
            review.append({
                "pool_address": spec.get("for_pool"),
                "dex": "jupiter",
                "score": (strategies.get(spec.get("for_pool")) or {}).get("score"),
                "evidence": None,
                "reason": ("prep deferred: target pool has no OPEN_CANDIDATE "
                           "passing the sizing gates this cycle (prep runs "
                           "after the sizing gate, not before)"),
            })
            continue
        signal = build_prep_swap_signal(spec, idx, base)
        path = os.path.join(signals_dir, f"{signal['signal_id']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(signal, fh, indent=2)
            fh.write("\n")
        created.append(path)
        if state_dir:
            prep_ledger.record_emitted(spec, signal["signal_id"], state_dir=state_dir)
        idx += 1

    # 1. Dust swap signals (executed before any new positions).
    # Anti-oscillation (mirrors the surplus-sell guard in plan_funding): a
    # mint bought as capital prep within the oscillation window exists to
    # fund an open, not to be round-tripped to USDC as "dust" — even when it
    # carries no symbol and the wallet classifier tags it. After the window
    # (or once the open lands) it is sweepable again.
    recent_prep_buys: set = set()
    if state_dir:
        try:
            recent_prep_buys = prep_ledger.recent_buy_mints(
                prep_ledger.load_ledger(state_dir),
            )
        except Exception:
            recent_prep_buys = set()
    for asset in wallet.get("dust_assets", []):
        if asset.get("mint") in recent_prep_buys:
            continue
        signal = _build_dust_swap_signal(asset, idx, base)
        path = os.path.join(signals_dir, f"{signal['signal_id']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(signal, fh, indent=2)
            fh.write("\n")
        created.append(path)
        idx += 1

    # 2. Position signals.
    # Bug #6 gate inputs: closes George already executed recently (any
    # signal_id, incl. manual out-of-band closes Sheldon never saw).
    _recent = prep_ledger.recently_executed_closes(journal_dir=journal_dir)
    recent_close_ids = _recent["position_ids"]
    recent_close_pools = _recent["pool_addrs"]
    for v in report.get("verdicts", []):
        action = v.get("action")
        pool_addr = v.get("pool_address")
        dex = v.get("dex") or "unknown"

        if action == "OPEN_CANDIDATE":
            # Only the top strategies chosen by build_strategies() get signals.
            if pool_addr not in strategies:
                review.append({
                    "pool_address": pool_addr,
                    "dex": dex,
                    "score": v.get("score"),
                    "evidence": v.get("evidence"),
                    "reason": "OPEN candidate skipped (capital or ranking gate)",
                })
                continue
            # Never emit an open when the strategy is not funded; the prep
            # swap above handles the shortfall and a later cycle opens.
            entry = next(
                (e for e in (funding.get("unfunded") or [])
                 if e.get("pool_address") == pool_addr),
                None,
            )
            if entry:
                review.append({
                    "pool_address": pool_addr,
                    "dex": dex,
                    "score": v.get("score"),
                    "evidence": v.get("evidence"),
                    "reason": f"OPEN deferred: {entry.get('reason') or 'tokens not funded'}",
                })
                continue
            # Phase 1 backstop: never emit an open unless the structured
            # readiness object for this pool passes every check. This holds
            # even when write_signals is called without run_cycle's filter.
            readiness = (report.get("readiness") or {}).get(pool_addr)
            if readiness is None or not readiness.get("ready"):
                review.append({
                    "pool_address": pool_addr,
                    "dex": dex,
                    "score": v.get("score"),
                    "evidence": v.get("evidence"),
                    "reason": "OPEN blocked by readiness gate: "
                              + ((readiness or {}).get("status")
                                 or "no readiness object"),
                })
                continue
            # Get pool info for signal building
            pool = v.get("_pool") or {}
            strategy = strategies.get(pool_addr, {})
            signal = _build_open_signal(strategy, v, pool, idx, base, min_open_score, supported_dexes)
            if signal is None:
                review.append({
                    "pool_address": pool_addr,
                    "dex": dex,
                    "score": v.get("score"),
                    "evidence": v.get("evidence"),
                    "reason": "OPEN candidate; could not build signal (missing enrichment)",
                })
                continue
        elif action in {"CLOSE", "REBALANCE", "COLLECT_FEES", "ROTATE", "RECENTER"}:
            # Live-fire bug #6 gate (2026-10-10): never re-emit a close or
            # claim for a position George already closed recently — the
            # settlement state can miss a manual/out-of-band close, and the
            # position scan can lag. A duplicate only produces a failed
            # "Position not found" dispatch; surface a review item instead.
            # RECENTER shares the gate: its first leg IS a close of the same
            # position, so every close-family guard applies to it unchanged.
            pos_id = str(v.get("position") or "")
            already_closed = (
                (pos_id and pos_id in recent_close_ids)
                or (not pos_id and pool_addr and pool_addr in recent_close_pools)
            )
            if already_closed:
                review.append({
                    "pool_address": pool_addr,
                    "dex": dex,
                    "score": v.get("score"),
                    "evidence": None,
                    "reason": (f"{action} suppressed: close already executed in "
                               f"George's journal for position {pos_id or pool_addr} "
                               f"within the re-emit window (bug #6 gate)"),
                })
                continue
            # Duplicate-close ghost guards (2026-10-16): close family only.
            if action in {"CLOSE", "REBALANCE", "ROTATE", "RECENTER"}:
                suppress = close_global_reason or close_pool_reasons.get(pool_addr)
                if suppress:
                    review.append({
                        "pool_address": pool_addr,
                        "dex": dex,
                        "score": v.get("score"),
                        "evidence": None,
                        "reason": f"{action} suppressed: {suppress}",
                    })
                    continue
                pending_sid = position_close_pending(close_state, pool_addr, pos_id)
                if pending_sid:
                    review.append({
                        "pool_address": pool_addr,
                        "dex": dex,
                        "score": v.get("score"),
                        "evidence": None,
                        "reason": (f"{action} suppressed: close {pending_sid} "
                                   f"already in flight for position {pos_id} "
                                   f"(position-level idempotency)"),
                    })
                    continue
            # Open-rubric close artifact (live 2026-10-24, signal
            # sheldon-1792856108-2: "CLOSE score=94.17" with pool-rubric
            # evidence, whose real close-rubric verdict was 57.89 — and the
            # artifact is the one that executed). A close decision must be
            # evidenced by the POSITION rubric or by OOR provenance stamped
            # by range_state. A close-family verdict carrying only pool
            # rubric components (fee_yield/depth/turnover/volatility_fit)
            # is demoted to review instead of reaching George.
            if action in {"CLOSE", "REBALANCE", "ROTATE", "RECENTER"}:
                _evidence = v.get("evidence") or {}
                if isinstance(_evidence, str):
                    # Verdicts sometimes carry evidence as a JSON string
                    # (older scorer output); decode before inspecting keys.
                    try:
                        _evidence = json.loads(_evidence)
                    except ValueError:
                        _evidence = {}
                if not isinstance(_evidence, dict):
                    _evidence = {}
                _pos_rubric = ("range_status", "fee_capture", "staleness",
                               "depeg_exposure", "il_risk")
                if not (any(k in _evidence for k in _pos_rubric)
                        or _evidence.get("oor_side")):
                    review.append({
                        "pool_address": pool_addr,
                        "dex": dex,
                        "score": v.get("score"),
                        "evidence": _evidence,
                        "reason": ("close demoted: pool-rubric evidence on "
                                   "close-family verdict, no position-rubric "
                                   "components and no OOR provenance "
                                   "(open-rubric close artifact)"),
                    })
                    continue
            lower = v.get("lower_bound")
            upper = v.get("upper_bound")
            try:
                lower = int(lower) if lower is not None else 0
                upper = int(upper) if upper is not None else 0
            except (TypeError, ValueError):
                lower, upper = 0, 0
            if action == "ROTATE":
                sig_reason = (
                    f"ROTATE score={v.get('score')}; "
                    f"target={v.get('candidate_pool_address')}; "
                    f"evidence={json.dumps(v.get('evidence'))}"
                )
            elif action == "RECENTER":
                sig_reason = (
                    f"RECENTER score={v.get('score')}; "
                    f"{v.get('recenter_reason')}"
                )
            else:
                sig_reason = (
                    f"{action} score={v.get('score')}; "
                    f"evidence={json.dumps(v.get('evidence'))}"
                )
            signal = {
                "signal_id": f"sheldon-{base}-{idx}",
                "action": ("claim_fees" if action == "COLLECT_FEES"
                           else "recenter" if action == "RECENTER"
                           else "close"),
                "dex": dex,
                "wallet_id": v.get("wallet_id") or "main",
                "pool_address": pool_addr,
                "position_id": v.get("position"),
                "side": "bidirectional",
                "bin_range": {"lower": lower, "upper": upper},
                "liquidity": {"amount_x": "0", "amount_y": "0"},
                "max_slippage_bps": 100,
                "reason": sig_reason,
                "created_at": _utc_iso(),
            }
            if action == "RECENTER":
                # George's validator requires position_id, bin_range,
                # liquidity, position_usd on a recenter. The verdict carries
                # the reopen payload computed by the trigger; a missing piece
                # demotes to review instead of emitting a signal that would
                # be rejected downstream.
                rng = v.get("recenter_bin_range") or {}
                liq = v.get("recenter_liquidity") or {}
                usd = v.get("recenter_position_usd")
                if not (rng.get("lower") is not None and rng.get("upper") is not None
                        and liq.get("amount_x") and liq.get("amount_y")
                        and isinstance(usd, (int, float)) and usd > 0):
                    review.append({
                        "pool_address": pool_addr,
                        "dex": dex,
                        "score": v.get("score"),
                        "evidence": v.get("evidence"),
                        "reason": ("RECENTER demoted: incomplete reopen payload "
                                   "(bin_range/liquidity/position_usd)"),
                    })
                    continue
                signal["bin_range"] = {"lower": int(rng["lower"]),
                                       "upper": int(rng["upper"])}
                signal["liquidity"] = {"amount_x": str(liq["amount_x"]),
                                       "amount_y": str(liq["amount_y"])}
                signal["position_usd"] = float(usd)
                signal["strategy_type"] = "Spot"
                signal["recenter_reason"] = v.get("recenter_reason")
            if action != "COLLECT_FEES":
                repaired = _repair_close_position_id(signal, v, positions_dir)
                if repaired is None:
                    review.append({
                        "pool_address": pool_addr,
                        "dex": dex,
                        "score": v.get("score"),
                        "evidence": v.get("evidence"),
                        "reason": ("close demoted: no unambiguous position_id "
                                   "on verdict or in fresh scan (would be "
                                   "rejected downstream)"),
                    })
                    continue
                signal = repaired
        else:
            # HOLD, REVIEW, WATCH, IGNORE => no signal.
            continue

        # Only protocols George can execute get a queued signal.
        if dex not in supported_dexes:
            review.append({
                "pool_address": pool_addr,
                "dex": dex,
                "score": v.get("score"),
                "evidence": v.get("evidence"),
                "reason": f"{action} candidate ({dex}); unsupported protocol",
            })
            continue

        path = os.path.join(signals_dir, f"{signal['signal_id']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(signal, fh, indent=2)
            fh.write("\n")
        created.append(path)
        idx += 1

    return created, review


def _position_scan_mtime(positions_dir: str) -> float:
    """Mtime of the newest position scan, or 0.0 when none is readable."""
    import lp_scoring
    pos_path = lp_scoring.newest_file(positions_dir, "position_scan")
    if not pos_path:
        return 0.0
    try:
        return os.path.getmtime(pos_path)
    except OSError:
        return 0.0


def _load_active_positions(positions_dir: str) -> dict:
    """Return dict of pool_address -> position dict for positions not closed.

    Includes 'inactive' (zero-liquidity but still open on-chain) positions:
    re-opening a pool that already holds one creates duplicate positions,
    which is exactly the bug this dedup exists to prevent.
    """
    import lp_scoring
    pos_path = lp_scoring.newest_file(positions_dir, "position_scan")
    if not pos_path:
        return {}
    try:
        data = lp_scoring.load_json(pos_path)
    except Exception:
        return {}
    positions = data.get("positions", []) if isinstance(data, dict) else data
    return {p.get("pool_address"): p for p in positions
            if p.get("pool_address") and p.get("status") != "closed"}


def _repair_close_position_id(signal: dict, verdict: dict,
                              positions_dir: str) -> dict:
    """Fill a close signal's missing position_id from the freshest scan.

    Live 2026-10-17 (sheldon-1792169350-1): a close verdict reached the
    writer with an empty ``position`` field; George's validator correctly
    rejected it ('close requires position_id') and a retry 52s later
    succeeded after the id appeared. Sheldon owns the signal, so the fix
    belongs here: repair from the freshest position scan when exactly one
    open row exists on the pool (unambiguous); never guess with more than
    one candidate — demote to a review item instead.
    Returns the (possibly repaired) signal, or None to demote.
    """
    if signal.get("position_id"):
        return signal
    import lp_scoring
    pool_addr = signal.get("pool_address")
    pos_path = lp_scoring.newest_file(positions_dir, "position_scan")
    candidates = []
    if pos_path and pool_addr:
        try:
            data = lp_scoring.load_json(pos_path)
        except Exception:
            data = {}
        rows = data.get("positions", []) if isinstance(data, dict) else data
        for row in rows or []:
            if not isinstance(row, dict) or row.get("pool_address") != pool_addr:
                continue
            if str(row.get("status") or "").lower() in {
                    "closed", "closed_empty", "empty", "inactive"}:
                continue
            pid = (row.get("position_id") or row.get("position_address")
                   or row.get("position_mint"))
            if pid:
                candidates.append(str(pid))
    if len(candidates) == 1:
        signal["position_id"] = candidates[0]
        signal["reason"] = (
            f"{signal.get('reason', '')}; position_id repaired from fresh "
            f"scan (verdict carried none)"
        )
        return signal
    return None