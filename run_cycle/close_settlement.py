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

# States at or beyond CLOSE_EXECUTED in the forward settlement ladder.
# Re-folding an `executed` journal decision must never move an entry that
# already reached one of these backwards: the journal is replayed every
# cycle, so an unconditional fold re-opens settled closes forever. That
# wedged opens on pools re-opened after a close — the new position makes
# absence unverifiable, so the rewound CLOSE_EXECUTED can never advance
# again (verified 2026-10-14: Oct-12 closes reached FUNDS_VERIFIED at
# 23:45, were rewound by the next replay, and blocked every open).
NON_REGRESSING_STATUSES: Set[str] = SETTLED_STATUSES | {
    "CLOSE_EXECUTED",
    "CLOSE_CHAIN_VERIFIED",
    "POSITION_SCAN_VERIFIED",
}

# Journal decisions that mean the close did not go through.
REJECTED_DECISIONS: Set[str] = {"rejected", "failed", "failed_verify"}

UNKNOWN_TTL_SECONDS = 7 * 24 * 3600

# Journal reason fragments classified as retryable. Anything else (including
# an empty or unknown reason) requires review — fail closed.
# Reasons that prove the transaction was never submitted. A retry may only be
# emitted for these (owner ruling 2026-10-19: "retry only when the failure is
# classifiably pre-submission — definitely never submitted").
PRE_SUBMISSION_PATTERNS = (
    "not set",
    "not configured",
    "missing env",
    "no signer",
    "signer",
    "wallet not",
    "validation",
    "invalid signal",
    "unsupported",
    "refused",
    "refusing",
    "preflight",
    "insufficient sol",
    "insufficient balance",
    "insufficient funds",
    "slippage",
    "price impact",
    "price_impact",
    "transaction simulation",
    "simulation failed",
    "bin_step",
    "outside allowed range",
    "below owner minimum",
)

# Reasons where the transaction MAY already have been submitted. These used to
# be classified retryable, which is the defect: a timeout after a send can be
# a landed close, and retrying it would close an already-closed position.
AMBIGUOUS_SUBMISSION_PATTERNS = (
    "timeout",
    "timed out",
    "blockhash",
    "expired",
    "network",
    "rpc",
    "rate limit",
    "rate_limit",
    "429",
    "fetch failed",
    "connection",
    "econnreset",
    "socket",
    "unknown",
    "internal error",
    "already processed",
    "block height exceeded",
)

# Anti-churn delay for a retry; never evidence of safety on its own.
RETRY_COOLDOWN_SECONDS = 900.0


def classify_submission_state(reason: str) -> str:
    """Classify a rejection by whether the send could have happened.

    Returns ``pre_submission`` (definitely never submitted),
    ``ambiguous`` (may have been submitted) or ``unknown``. Only
    ``pre_submission`` is retryable.
    """
    lowered = (reason or "").lower()
    if not lowered:
        return "unknown"
    for pattern in PRE_SUBMISSION_PATTERNS:
        if pattern in lowered:
            return "pre_submission"
    for pattern in AMBIGUOUS_SUBMISSION_PATTERNS:
        if pattern in lowered:
            return "ambiguous"
    return "unknown"


def classify_rejection(reason: str) -> str:
    """Classify a rejected close cause as ``retryable`` or ``review``.

    Retryable means *provably pre-submission*. Ambiguous and unknown causes
    are never retryable: an ambiguous failure stays in review regardless of
    what the position scan shows, because the close may have landed.
    """
    return ("retryable" if classify_submission_state(reason) == "pre_submission"
            else "review")


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


def mark_emitted(state: Dict[str, Dict[str, Any]], signal_id: str,
                 pool_address: Optional[str], now_ts: float = None,
                 action: str = "close", position_id: str = None) -> None:
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
        "position_id": position_id or None,
        "action": action,
        "emit_time": now_ts,
    }


def _derive_rejected_status(entry: Dict[str, Any],
                            position_present: Optional[bool],
                            position_scan_current: bool,
                            pool_settled_before: bool,
                            retry_ready: bool = False,
                            ) -> str:
    """Re-derive the actionable status of a rejected close.

    order matters:
      1. stale duplicate — the pool already settled before this close was
         even rejected, so the rejection is a duplicate emit racing the
         scan refresh: resolve it;
      2. position absent from a current scan — ambiguous (the close may
         have landed despite the failure): review;
      3. position still present + provably pre-submission failure + fresh
         evidence + retry cooldown elapsed: retry with a new signal id;
      4. anything else — review.

    Owner ruling 2026-10-19: retry requires proof the transaction was never
    submitted. An ambiguous cause (timeout, RPC/network, blockhash expiry)
    is NOT retryable regardless of what the scan shows, because the close
    may already have landed. ``retry_ready`` only encodes the anti-churn
    cooldown; it never authorises a retry on its own.
    """
    submission_state = classify_submission_state(entry.get("reason") or "")
    entry["submission_state"] = submission_state
    if pool_settled_before:
        return "CLOSE_RESOLVED"
    if position_present is False and position_scan_current:
        return "CLOSE_REVIEW_REQUIRED"
    if submission_state != "pre_submission":
        # Ambiguous or unknown: may have been submitted. Stay in review.
        return "CLOSE_REVIEW_REQUIRED"
    if position_present is True and position_scan_current and retry_ready:
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
            # Fold facts only; never regress status. A journal replay of an
            # already-executed close is not news. Rewinding a settled or
            # advancing entry here wedges the gate permanently when the pool
            # gained a new position after the close (absence can never be
            # re-verified), so guard every state at or beyond CLOSE_EXECUTED.
            if entry.get("status") not in NON_REGRESSING_STATUSES:
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
                             now: float = None,
                             retry_cooldown_seconds: float = None,
                             ) -> None:
    """Re-derive actionable statuses for rejected closes (Change F).

    Runs every cycle after ``advance`` so the classification always
    reflects the current evidence — a rejection that looked ambiguous
    clears itself once the pool's settlement lands. Manual entries and
    already-terminal entries are never reclassified.
    """
    settled_by_pool: Dict[str, float] = {}
    now = time.time() if now is None else now
    cooldown = (RETRY_COOLDOWN_SECONDS if retry_cooldown_seconds is None
                else retry_cooldown_seconds)
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
            entry, present, position_scan_current, pool_settled_before,
            retry_ready=bool(rejected_at > 0
                             and (now - rejected_at) >= cooldown))


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
        if entry.get("funds_verified_at"):
            # Self-heal a rewound entry: funds_verified_at is only ever
            # stamped by this ladder reaching FUNDS_VERIFIED, so restore
            # that status instead of re-walking a ladder whose absence
            # check can never re-fire once the pool holds a new position.
            entry["status"] = "FUNDS_VERIFIED"
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

def _latest_settlement_ts(entry: Dict[str, Any]) -> float:
    """Latest settlement-progress timestamp stored on one entry.

    Covers every ladder stamp plus manual resolutions: any of these means
    "a close event was processed for this pool at this time".
    """
    return max(
        _epoch(entry.get("funds_verified_at")),
        _epoch(entry.get("resolved_at")),
        _epoch(entry.get("chain_verified_at")),
        _epoch(entry.get("confirmed_at")),
    )


def close_emit_suppressions(close_state: Dict[str, Dict[str, Any]],
                            position_mtime: float,
                            now: float = None,
                            policy: Optional[Dict[str, Any]] = None,
                            ) -> Tuple[Optional[str], Dict[str, str]]:
    """Suppression reasons for emitting NEW close signals (2026-10-16).

    The 2026-10-14/15 ghost families were all the same race: a close
    executed on-chain, the next cycle read a position scan taken BEFORE the
    execution, and re-emitted a close for the already-closed position.
    George's failed "Position not found" dispatch then wedged the
    settlement gate until a human resolved it.

    Returns ``(global_reason, per_pool)``:

    * ``global_reason`` — set when the position scan is stale (older than
      ``close_emission.max_scan_age_seconds``) or missing: no scan, no
      proof any position still exists, so no automated close emission.
    * ``per_pool`` — pool_address -> reason when the pool's latest close
      settlement event is NEWER than the position scan: that scan cannot
      prove anything about a position closed after it was taken.

    These rails defer SHELDON's automated emission only (the verdict
    surfaces as a review item and a fresh scan re-enables it). They never
    block George-side or owner closes — closing is always allowed.
    """
    now = time.time() if now is None else now
    ce = (policy or {}).get("close_emission") or {}
    global_reason: Optional[str] = None
    if ce.get("require_fresh_scan", True):
        max_age = float(ce.get("max_scan_age_seconds", 900.0))
        if not position_mtime:
            global_reason = ("position scan missing; refusing to close "
                             "on no evidence (fail-closed)")
        elif (now - position_mtime) > max_age:
            global_reason = (f"position scan stale for close emission "
                             f"(age {now - position_mtime:.0f}s > "
                             f"{max_age:.0f}s)")
    per_pool: Dict[str, str] = {}
    if ce.get("require_scan_newer_than_last_close", True) and position_mtime:
        for sid, entry in close_state.items():
            if not isinstance(entry, dict):
                continue
            pool = entry.get("pool_address")
            if not pool or pool in per_pool:
                continue
            ts = _latest_settlement_ts(entry)
            if ts and position_mtime < ts:
                per_pool[pool] = (f"pool has a close-settlement event newer "
                                  f"than the position scan (signal {sid}); "
                                  f"scan cannot prove the position still "
                                  f"exists — duplicate-emit guard")
    return global_reason, per_pool


def position_close_pending(close_state: Dict[str, Dict[str, Any]],
                           pool_address: Optional[str],
                           position_id: Optional[str],
                           ) -> Optional[str]:
    """Signal id of a non-settled close already in flight for one position.

    Position-level idempotency: a pool may legitimately receive a new
    position after an old one closes, so matching is by position_id (with
    a pool consistency check), never by pool alone. Settled entries never
    match — a settled close plus a fresh scan listing the position again
    means a NEW position, which must stay closeable.

    CLOSE_RETRY_ALLOWED deliberately does NOT match: that status *is* the
    authorization to re-emit, and treating it as "in flight" made the
    evidence-gated retry unreachable (the retry was suppressed by the very
    state that approved it). A retry always carries a new signal_id, so
    position-level idempotency is preserved by the new entry superseding
    the old one in mark_emitted().
    """
    if not position_id:
        return None
    for sid, entry in close_state.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") in SETTLED_STATUSES:
            continue
        if entry.get("status") == "CLOSE_RETRY_ALLOWED":
            continue
        if entry.get("position_id") != position_id:
            continue
        if pool_address and entry.get("pool_address") not in (
                None, pool_address):
            continue
        return sid
    return None


def close_cooldown_pools(close_state: Dict[str, Dict[str, Any]],
                         now: float = None,
                         cooldown_seconds: float = 2700.0,
                         ) -> Dict[str, float]:
    """Map pool_address -> earliest reopen timestamp after a settled close.

    Anti-churn rail (2026-10-16): a pool whose close settled moments ago
    should not re-open in the same cycle burst — the immediate re-open was
    half of every churn family (close -> ghost duplicate -> re-open ->
    repeat). Only settled entries count; unsettled entries already block
    opens via the settlement gate. ``cooldown_seconds <= 0`` disables the
    rail. Read-only: this never blocks closes, claims or removes.
    """
    if not cooldown_seconds or cooldown_seconds <= 0:
        return {}
    out: Dict[str, float] = {}
    for entry in close_state.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") not in SETTLED_STATUSES:
            continue
        pool = entry.get("pool_address")
        if not pool:
            continue
        ts = _latest_settlement_ts(entry)
        if ts:
            out[pool] = max(out.get(pool, 0.0),
                            ts + float(cooldown_seconds))
    return out


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


# Statuses that mean "a close was emitted and no execution proof exists".
# These are the entries a dry_run cycle can produce but never clear, because
# clearing them requires a live journal decision that dry_run never writes.
CHAIN_TRUTH_CANDIDATE_STATUSES: Set[str] = {
    "CLOSE_EMITTED",
    "CLOSE_UNKNOWN",
    "CLOSE_UNKNOWN_TIMEOUT",
    "CLOSE_RETRY_ALLOWED",
    "CLOSE_REVIEW_REQUIRED",
}


def reconcile_chain_truth(state: Dict[str, Dict[str, Any]],
                          chain_check: Any = None,
                          rpc_url: Optional[str] = None,
                          mode: Optional[str] = None,
                          now: float = None,
                          policy: Optional[Dict[str, Any]] = None,
                          pending_position_ids: Optional[Set[str]] = None,
                          ) -> Dict[str, Any]:
    """Resolve emitted-but-never-executed closes against the chain.

    Owner-approved 2026-10-19 for ``dry_run`` only. In a live mode the
    journal ladder is the authority and this must stay OFF: resolving a
    close on chain absence there would hide a real failed settlement.

    An entry is resolved only when every one of these holds:

    * mode is explicitly ``dry_run`` (never inferred, never defaulted);
    * policy does not disable it;
    * the entry is not ``manual`` and not already settled;
    * the entry has both a ``position_id`` and a ``pool_address``;
    * the position is not in flight (no pending signal for that position);
    * :func:`chain_truth.check_position_absent` returns ``absent`` — pool
      control resolved, position null at confirmed *and* finalized.

    Resolved entries are stamped ``manual: False``, ``resolution:
    "chain_truth"`` and carry the RPC evidence, so the audit trail says
    exactly how the close was proven and that no human vouched for it.
    """
    now = time.time() if now is None else now
    policy = policy or {}
    report: Dict[str, Any] = {
        "enabled": False, "mode": mode, "resolved": [], "skipped": [],
        "reason": "",
    }
    if (mode or "").strip() != "dry_run":
        report["reason"] = ("chain-truth reconciliation runs only in "
                            "dry_run")
        return report
    if not policy.get("dry_run_chain_truth_reconciliation", True):
        report["reason"] = "disabled by policy"
        return report
    if chain_check is None:
        from . import chain_truth as _ct
        chain_check = _ct.check_position_absent
    report["enabled"] = True

    pending = pending_position_ids or set()
    seen: Dict[str, Dict[str, Any]] = {}
    for sid in sorted(state):
        entry = state.get(sid)
        if not isinstance(entry, dict):
            continue
        if entry.get("manual") or entry.get("status") in SETTLED_STATUSES:
            continue
        status = entry.get("status")
        if status not in CHAIN_TRUTH_CANDIDATE_STATUSES:
            continue
        position_id = entry.get("position_id")
        pool_address = entry.get("pool_address")
        if not position_id or not pool_address:
            report["skipped"].append({
                "signal_id": sid,
                "reason": "entry has no position_id/pool_address; absence "
                          "cannot be proven and it stays blocking"})
            continue
        if position_id in pending:
            report["skipped"].append({
                "signal_id": sid,
                "reason": "close still pending in the signals queue"})
            continue
        # One RPC verdict per position per cycle, however many signals
        # reference it (the 18 ghosts were 9 cycles x 2 positions).
        key = str(position_id)
        if key not in seen:
            seen[key] = chain_check(rpc_url, position_id, pool_address)
        evidence = seen[key]
        if not evidence.get("absent"):
            report["skipped"].append({
                "signal_id": sid,
                "position_id": position_id,
                "reason": evidence.get("reason") or "not proven absent",
            })
            continue
        entry["status"] = "CLOSE_RESOLVED"
        entry["manual"] = False
        entry["resolution"] = "chain_truth"
        entry["resolved_at"] = now
        entry["resolved_at_iso"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        entry["chain_evidence"] = evidence
        entry["note"] = (
            "dry_run chain-truth: position account absent at confirmed and "
            "finalized with pool control resolving; signal was consumed "
            "without an execution decision")
        report["resolved"].append({
            "signal_id": sid,
            "position_id": position_id,
            "previous_status": status,
            "pool_address": pool_address,
        })
    return report


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
                     mode: Optional[str] = None,
                     chain_rpc_url: Optional[str] = None,
                     chain_check: Any = None,
                     pending_position_ids: Optional[Set[str]] = None,
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
    # dry_run only: a close that was emitted but never executed can never
    # advance on the journal ladder, so prove it on chain instead of
    # re-emitting it forever. Live modes skip this entirely.
    chain_report = reconcile_chain_truth(
        state, chain_check=chain_check, rpc_url=chain_rpc_url, mode=mode,
        now=now, policy=policy, pending_position_ids=pending_position_ids)
    blocking, review = summarize(state)
    prune_settled(state, now)
    details = {
        "tracked": len(state),
        "blocking": blocking,
        "review": review,
        "settled": [sid for sid, e in state.items()
                    if isinstance(e, dict)
                    and e.get("status") in SETTLED_STATUSES],
        "chain_truth": chain_report,
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
            now_ts=time.time(), action=emitted.get("action"),
            position_id=emitted.get("position_id"))
        emitted_closes.append({
            "signal_id": emitted["signal_id"],
            "pool_address": emitted.get("pool_address"),
            "position_id": emitted.get("position_id"),
        })
        changed = True
    if changed:
        save_close_state(state_dir, state)
    return emitted_closes
