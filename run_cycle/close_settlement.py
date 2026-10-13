"""Close settlement state machine — Phase 2 of the 2026-10-09 update.

Sheldon owns close-settlement state. A close is settled — and opens are
unblocked — only when the whole settlement chain is verified, not merely
when George's journal says "executed":

    CLOSE_EMITTED          close signal written to George's pending queue
    CLOSE_EXECUTED         journal decision == executed (legacy alias
                           CLOSE_ACCEPTED is mapped on load)
    CLOSE_CHAIN_VERIFIED   a position scan newer than the close confirmation
                           shows the source position absent
    POSITION_SCAN_VERIFIED the verifying scan is itself within max scan age
    FUNDS_VERIFIED         a wallet scan newer than the confirmation exists
                           (released capital visible or safely accounted
                           for); terminal — unblocks opens
    CLOSE_REJECTED         journal decision in {rejected, failed,
                           failed_verify}; the stored status stays raw and
                           the actionable status is re-derived every cycle
    CLOSE_RETRY_ALLOWED    rejection with a retryable cause, position still
                           present in a fresh scan; a NEW close signal (new
                           signal_id) may be emitted
    CLOSE_REVIEW_REQUIRED  ambiguous rejection (position already absent —
                           the close may have landed anyway) or a
                           non-retryable cause; blocks until a human
                           resolves it
    CLOSE_RESOLVED         terminal: superseded by a new emit, a stale
                           duplicate, or explicit human action
    CLOSE_UNKNOWN          no journal record; escalates to
                           CLOSE_UNKNOWN_TIMEOUT after the TTL (still
                           blocking — a lost close is resolved by a human,
                           never by timeout alone)

Design rules:

* Fail closed. Only FUNDS_VERIFIED and CLOSE_RESOLVED unblock opens; every
  other known or unknown status blocks.
* Classification is re-derived every cycle from stored raw facts, so a
  rejection that looked ambiguous clears itself once the evidence (a
  settled prior close for the same pool) arrives. A human resolution is
  marked ``manual: true`` and is never overridden.
* Manual resolution path: edit ``close_settlement_state.json``, set the
  entry's status to ``CLOSE_RESOLVED`` and add ``"manual": true`` plus a
  ``note``. There is deliberately no CLI shortcut for this.
* This gate only ever blocks OPENS. Nothing here can block a close, claim
  or de-risking remove — closing is always allowed.
"""

import datetime
import json
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from readiness.prep_ledger import DEFAULT_JOURNAL_DIR, read_journal

CLOSE_STATE_FILENAME = "close_settlement_state.json"

# States that do not block opens.
SETTLED_STATUSES: Set[str] = {"FUNDS_VERIFIED", "CLOSE_RESOLVED"}

# Journal decisions that mean the close did not go through.
REJECTED_DECISIONS: Set[str] = {"rejected", "failed", "failed_verify"}

UNKNOWN_TTL_SECONDS = 7 * 24 * 3600

# Journal reason fragments classified as retryable. Anything else (including
# an empty or unknown reason) requires review — fail closed.
RETRYABLE_REASON_PATTERNS = (
    "slippage",
    "price impact",
    "price_impact",
    "insufficient funds",
    "insufficient sol",
    "insufficient balance",
    "timeout",
    "timed out",
    "expired",
    "blockhash",
    "network",
    "rpc",
    "rate limit",
    "rate_limit",
    "429",
    "transaction simulation",
    "retry",
)


def _close_state_path(state_dir: str) -> str:
    """Path to the close settlement state file."""
    return os.path.join(state_dir, CLOSE_STATE_FILENAME)


def load_close_state(state_dir: str) -> Dict[str, Dict[str, Any]]:
    """Load close settlement state from previous cycles.

    Legacy ``CLOSE_ACCEPTED`` entries are mapped to ``CLOSE_EXECUTED``.
    Non-dict values from very old files are wrapped as ``{"status": v}``.
    """
    path = _close_state_path(state_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            out: Dict[str, Dict[str, Any]] = {}
            for k, v in data.items():
                if not isinstance(v, dict):
                    v = {"status": v}
                if v.get("status") == "CLOSE_ACCEPTED":
                    v["status"] = "CLOSE_EXECUTED"
                    v.setdefault("legacy_alias", "CLOSE_ACCEPTED")
                out[k] = v
            return out
    except (OSError, ValueError):
        pass
    return {}


def save_close_state(state_dir: str, state: dict) -> None:
    """Persist close settlement state atomically. Best effort on OSError."""
    path = _close_state_path(state_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def classify_rejection(reason: str) -> str:
    """Classify a rejected close cause as ``retryable`` or ``review``.

    Unknown, empty or unparseable causes classify as review: we never retry
    a close we do not understand.
    """
    lowered = (reason or "").lower()
    if not lowered:
        return "review"
    for pattern in RETRYABLE_REASON_PATTERNS:
        if pattern in lowered:
            return "retryable"
    return "review"


def mark_emitted(state: Dict[str, Dict[str, Any]], signal_id: str,
                 pool_address: Optional[str], now_ts: float = None,
                 action: str = "close") -> None:
    """Record CLOSE_EMITTED for a freshly written close signal (Change G).

    A new emit for a pool supersedes that pool's earlier rejected attempts
    (CLOSE_REJECTED / CLOSE_RETRY_ALLOWED -> CLOSE_RESOLVED with
    ``superseded_by``): the retry is the resolution path. Review-required
    entries are never auto-resolved — they need a human.
    """
    now_ts = time.time() if now_ts is None else now_ts
    for sid, entry in state.items():
        if (not isinstance(entry, dict)
                or entry.get("manual")
                or sid == signal_id
                or not pool_address
                or entry.get("pool_address") != pool_address):
            continue
        if entry.get("status") in ("CLOSE_REJECTED", "CLOSE_RETRY_ALLOWED"):
            entry["status"] = "CLOSE_RESOLVED"
            entry["resolved_at"] = now_ts
            entry["superseded_by"] = signal_id
            entry.setdefault("note",
                             "superseded by a newer close emit for the "
                             "same pool")
    state[signal_id] = {
        "status": "CLOSE_EMITTED",
        "pool_address": pool_address,
        "action": action,
        "emit_time": now_ts,
    }


def _derive_rejected_status(entry: Dict[str, Any],
                            position_present: Optional[bool],
                            position_scan_current: bool,
                            pool_settled_before: bool) -> str:
    """Re-derive the actionable status of a rejected close.

    order matters:
      1. stale duplicate — the pool already settled before this close was
         even rejected, so the rejection is a duplicate emit racing the
         scan refresh: resolve it;
      2. position absent from a current scan — ambiguous (the close may
         have landed despite the failure): review;
      3. position still present + retryable cause + fresh evidence —
         a retry with a new signal id is allowed;
      4. anything else — review.
    """
    if pool_settled_before:
        return "CLOSE_RESOLVED"
    if position_present is False and position_scan_current:
        return "CLOSE_REVIEW_REQUIRED"
    if (position_present is True and position_scan_current
            and classify_rejection(entry.get("reason") or "") == "retryable"):
        return "CLOSE_RETRY_ALLOWED"
    return "CLOSE_REVIEW_REQUIRED"


def update_from_journal(state: Dict[str, Dict[str, Any]],
                        journal_resolutions: Dict[str, Dict[str, Any]],
                        position_presence: Dict[str, Optional[bool]],
                        position_scan_current: bool,
                        ) -> Dict[str, Dict[str, Any]]:
    """Fold George's journal decisions into the close state.

    Only signal ids already present in ``state`` are folded: Sheldon tracks
    exactly the closes it emitted (Change G). George's journal also contains
    opens, prep swaps, dust sweeps and manual signals — none of those are
    closes, and folding them would wedge the gate on every historical
    rejection. Close signals emitted before this state machine existed are
    untracked and stay untracked (production status quo).

    ``position_presence`` maps pool_address -> True/False/None (unknown —
    pool not seen in any scan) for pools referenced by state entries.
    Executed decisions store ``confirmed_at`` durably so settlement can
    still be verified after journal rotation.
    """
    for sid, res in journal_resolutions.items():
        if sid not in state:
            continue
        entry = state[sid]
        if not isinstance(entry, dict):
            entry = state[sid] = {"status": entry}
        decision = res.get("status") or res.get("decision")
        if decision not in ("executed",) and decision not in REJECTED_DECISIONS:
            continue
        if entry.get("manual"):
            continue
        ts = res.get("confirmed_at") or 0.0
        entry["pool_address"] = entry.get("pool_address") or res.get(
            "pool_address")
        if decision == "executed":
            entry["status"] = "CLOSE_EXECUTED"
            # Keep the first confirmation time; never regress it.
            if not entry.get("confirmed_at") and ts:
                entry["confirmed_at"] = ts
            entry["journal_decision"] = "executed"
            if res.get("reason"):
                entry["journal_reason"] = res["reason"]
            continue
        # Rejected-family decision: store raw facts; the actionable status
        # is derived in derive_rejected_statuses() AFTER the settlement
        # advance, so a pool that settles this same cycle already counts as
        # "settled before the rejection" evidence.
        entry["journal_decision"] = decision
        entry["reason"] = res.get("reason") or entry.get("reason") or ""
        if ts and (not entry.get("rejected_at") or ts > entry["rejected_at"]):
            entry["rejected_at"] = ts
    return state


def derive_rejected_statuses(state: Dict[str, Dict[str, Any]],
                             position_presence: Dict[str, Optional[bool]],
                             position_scan_current: bool,
                             ) -> None:
    """Re-derive actionable statuses for rejected closes (Change F).

    Runs every cycle after ``advance`` so the classification always
    reflects the current evidence — a rejection that looked ambiguous
    clears itself once the pool's settlement lands. Manual entries and
    already-terminal entries are never reclassified.
    """
    settled_by_pool: Dict[str, float] = {}
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") == "FUNDS_VERIFIED":
            pool = entry.get("pool_address")
            conf = entry.get("confirmed_at") or 0.0
            if pool and conf > settled_by_pool.get(pool, 0.0):
                settled_by_pool[pool] = conf
    for entry in state.values():
        if (not isinstance(entry, dict) or entry.get("manual")
                or entry.get("journal_decision") not in REJECTED_DECISIONS):
            continue
        if entry.get("status") in SETTLED_STATUSES:
            continue
        pool = entry.get("pool_address")
        present = position_presence.get(pool) if pool else None
        rejected_at = entry.get("rejected_at") or 0.0
        pool_settled_before = bool(
            pool and rejected_at > 0
            and settled_by_pool.get(pool, 0.0) >= rejected_at)
        entry["status"] = _derive_rejected_status(
            entry, present, position_scan_current, pool_settled_before)


def advance(state: Dict[str, Dict[str, Any]],
            active_positions: Dict[str, Any],
            wallet_mtime: float,
            position_mtime: float,
            now: float = None,
            policy: Optional[Dict[str, Any]] = None,
            max_scan_age_seconds: float = 900.0,
            ) -> Dict[str, Dict[str, Any]]:
    """Drive per-entry settlement transitions, in place.

    Transitions for an executed close, each individually policy-gated:

      CLOSE_EXECUTED -> CLOSE_CHAIN_VERIFIED
          fresh position scan (mtime > confirmed_at) shows the source
          position absent                [require_chain_close_verification]
      CLOSE_CHAIN_VERIFIED -> POSITION_SCAN_VERIFIED
          the verifying scan is within max_scan_age_seconds
                                         [require_position_scan_confirmation]
      POSITION_SCAN_VERIFIED -> FUNDS_VERIFIED
          wallet scan newer than confirmed_at — released capital visible
          or safely accounted for        [require_wallet_capital_confirmation]

    Unknown entries escalate to CLOSE_UNKNOWN_TIMEOUT after the TTL; they
    keep blocking. Manual entries are left untouched.
    """
    now = time.time() if now is None else now
    policy = policy or {}
    require_chain = policy.get("require_chain_close_verification", True)
    require_scan_confirm = policy.get("require_position_scan_confirmation",
                                      True)
    require_wallet = policy.get("require_wallet_capital_confirmation", True)
    for sid, entry in state.items():
        if not isinstance(entry, dict):
            entry = state[sid] = {"status": entry}
        status = entry.get("status")
        if entry.get("manual"):
            continue
        if status in SETTLED_STATUSES:
            continue
        pool = entry.get("pool_address")
        present = pool in active_positions if pool else None
        conf = entry.get("confirmed_at") or 0.0

        if entry.get("status") == "CLOSE_EXECUTED":
            if not conf:
                # Executed but never confirmable — cannot verify funds.
                # Escalates via the unknown TTL below rather than blocking
                # forever silently.
                entry["settlement_note"] = (
                    "executed without a confirmation timestamp; funds "
                    "unverified")
            elif (not require_chain
                  or (position_mtime > conf and present is False)):
                entry["status"] = "CLOSE_CHAIN_VERIFIED"
                entry["chain_verified_at"] = now

        if entry.get("status") == "CLOSE_CHAIN_VERIFIED":
            scan_age = now - position_mtime if position_mtime else None
            scan_current = (
                scan_age is not None
                and (not require_scan_confirm
                     or scan_age <= max_scan_age_seconds))
            if scan_current:
                entry["status"] = "POSITION_SCAN_VERIFIED"
                entry["position_scan_verified_at"] = now

        if entry.get("status") == "POSITION_SCAN_VERIFIED":
            if not require_wallet or wallet_mtime > conf:
                entry["status"] = "FUNDS_VERIFIED"
                entry["funds_verified_at"] = now
                entry["capital_evidence"] = (
                    "wallet scan newer than close confirmation")

        if status == "CLOSE_EMITTED":
            emit_ts = entry.get("emit_time") or 0.0
            if emit_ts and emit_ts < now - UNKNOWN_TTL_SECONDS:
                # Lost close: no journal record ever arrived. Escalate to
                # review; it keeps blocking (a timeout never resolves a
                # close by itself) but it is now visible on the review
                # surface instead of rotting silently.
                entry["status"] = "CLOSE_UNKNOWN_TIMEOUT"
                entry["timed_out_at"] = now

        if status == "CLOSE_UNKNOWN" and require_chain is not None:
            emit_ts = entry.get("emit_time") or 0.0
            if emit_ts and emit_ts < now - UNKNOWN_TTL_SECONDS:
                entry["status"] = "CLOSE_UNKNOWN_TIMEOUT"
                entry["timed_out_at"] = now
    return state


def resolve_entry(state_dir: str, signal_id: str, note: str = "") -> int:
    """Mark one close-settlement entry CLOSE_RESOLVED (manual:true).

    Owner-approved 2026-10-10 (plan question #19): supersedes the
    edit-the-JSON-only design. Returns a process exit code.
    """
    state = load_close_state(state_dir)
    entry = state.get(signal_id)
    if not isinstance(entry, dict):
        print(f"NO SETTLEMENT ENTRY for {signal_id}; nothing to resolve")
        return 1
    if entry.get("status") in ("FUNDS_VERIFIED", "CLOSE_RESOLVED"):
        print(f"{signal_id} already settled ({entry.get('status')})")
        return 0
    entry["status"] = "CLOSE_RESOLVED"
    entry["manual"] = True
    entry["note"] = note or "resolved via --resolve-close"
    # Store an epoch float like every other timestamp writer (mark_emitted,
    # advance). Writing an ISO string here made prune_settled's numeric max()
    # raise TypeError, so a CLI resolution left the state file unprunable and
    # crashed every later cycle. Keep the human-readable stamp alongside.
    entry["resolved_at"] = time.time()
    entry["resolved_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                             time.gmtime())
    save_close_state(state_dir, state)
    print(f"RESOLVED {signal_id} -> CLOSE_RESOLVED (manual)")
    return 0


def _epoch(value: Any) -> float:
    """Coerce a stored timestamp to epoch seconds.

    Writers have used both shapes: epoch floats (mark_emitted, advance) and
    ISO-8601 strings (resolve_entry before 2026-10-14). prune_settled takes
    a numeric max() over these fields, so normalize both; anything missing
    or unparseable becomes 0.0 (an entry is then never pruned).
    """
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.datetime.strptime(
                value, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            return 0.0
    return 0.0

def prune_settled(state: Dict[str, Dict[str, Any]], now: float = None,
                  max_age_seconds: float = 30 * 24 * 3600
                  ) -> Dict[str, Dict[str, Any]]:
    """Drop terminal entries older than ``max_age_seconds``.

    Keeps the state file bounded now that every emitted close is tracked.
    Only SETTLED_STATUSES entries with a terminal timestamp are pruned;
    anything blocking is kept no matter how old (a wedge is surfaced, not
    hidden).
    """
    now = time.time() if now is None else now
    for sid in list(state):
        entry = state.get(sid)
        if not isinstance(entry, dict):
            continue
        if entry.get("status") not in SETTLED_STATUSES:
            continue
        terminal_ts = max(
            _epoch(entry.get("funds_verified_at")),
            _epoch(entry.get("resolved_at")),
            _epoch(entry.get("confirmed_at")),
        )
        if terminal_ts and now - terminal_ts > max_age_seconds:
            del state[sid]
    return state


def summarize(state: Dict[str, Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """Return (blocking_reasons, review_items) for the current state."""
    blocking: List[str] = []
    review: List[str] = []
    for sid, entry in state.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") in SETTLED_STATUSES:
            continue
        pool = entry.get("pool_address")
        pool_note = f"pool {pool}: " if pool else ""
        blocking.append(f"{pool_note}close {sid} {entry.get('status')}")
        if entry.get("status") in ("CLOSE_REVIEW_REQUIRED",
                                   "CLOSE_UNKNOWN_TIMEOUT"):
            review.append({
                "signal_id": sid,
                "pool_address": pool,
                "status": entry.get("status"),
                "reason": (entry.get("reason")
                           or entry.get("settlement_note") or ""),
            })
    return blocking, review


def check_settlement(active_positions: Dict[str, Any],
                     wallet_mtime: float,
                     position_mtime: float,
                     close_state: Dict[str, Dict[str, Any]],
                     now: float = None,
                     policy: Optional[Dict[str, Any]] = None,
                     journal_dir: Optional[str] = None,
                     max_scan_age_seconds: float = 900.0,
                     ) -> Tuple[bool, str, Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Full settlement check; returns (allowed, reason, state, details).

    ``allowed`` is True only when every tracked close is settled
    (FUNDS_VERIFIED or CLOSE_RESOLVED). Only opens consult this verdict —
    closes, claims and removes are never gated here.
    """
    now = time.time() if now is None else now
    journal_resolutions = read_journal(journal_dir or DEFAULT_JOURNAL_DIR)

    # Which pools still appear in the current position scan?
    position_presence: Dict[str, Optional[bool]] = {}
    pools_in_state = {e.get("pool_address") for e in close_state.values()
                      if isinstance(e, dict) and e.get("pool_address")}
    for pool in pools_in_state:
        position_presence[pool] = pool in active_positions

    state = update_from_journal(close_state, journal_resolutions,
                                position_presence,
                                bool(position_mtime)
                                and (now - position_mtime)
                                <= max_scan_age_seconds)
    advance(state, active_positions, wallet_mtime,
            position_mtime, now=now, policy=policy,
            max_scan_age_seconds=max_scan_age_seconds)
    # Re-derive rejected classifications AFTER the advance so a pool that
    # settled this same cycle already counts as prior-settled evidence.
    derive_rejected_statuses(state, position_presence,
                             bool(position_mtime)
                             and (now - position_mtime)
                             <= max_scan_age_seconds)
    blocking, review = summarize(state)
    prune_settled(state, now)
    details = {
        "tracked": len(state),
        "blocking": blocking,
        "review": review,
        "settled": [sid for sid, e in state.items()
                    if isinstance(e, dict)
                    and e.get("status") in SETTLED_STATUSES],
    }
    if blocking:
        reason = ("Close settlement gate blocked open: "
                  + "; ".join(blocking[:2]))
        return False, reason, state, details
    return True, "Close settlement gate: all closes settled", state, details


# Backward-compatible aliases for the historical run_cycle helpers.
_load_close_state = load_close_state
_save_close_state = save_close_state


def record_emitted_closes(state_dir: str, signals_created: list) -> list:
    """Change G: persist CLOSE_EMITTED for every close signal just written.

    Reads each created signal file; close actions (CLOSE, REBALANCE and
    ROTATE all emit action == "close") are recorded in the close
    settlement state with their pool address and emit time, superseding
    that pool's earlier rejected attempts. claim_fees and prep/swap signals
    are not capital-removing and are not tracked.

    Returns the list of emitted close records for the report.
    """
    state = load_close_state(state_dir)
    emitted_closes = []
    changed = False
    for path in signals_created or []:
        try:
            with open(path, encoding="utf-8") as fh:
                emitted = json.load(fh)
        except (OSError, ValueError):
            continue
        if emitted.get("action") != "close":
            continue
        mark_emitted(
            state, emitted["signal_id"], emitted.get("pool_address"),
            now_ts=time.time(), action=emitted.get("action"))
        emitted_closes.append({
            "signal_id": emitted["signal_id"],
            "pool_address": emitted.get("pool_address"),
        })
        changed = True
    if changed:
        save_close_state(state_dir, state)
    return emitted_closes
