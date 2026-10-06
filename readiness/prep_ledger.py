#!/usr/bin/env python3
"""Prep swap ledger — state-based dedupe for capital prep swaps.

The rescan-wait / hourly-cap guards in ``prep_swap_gates`` are time-based:
they compare a *signal's* ``created_at`` against the wallet-scan mtime, so a
prep that already executed still re-emits once a fresh scan lands. That is
the buy/sell oscillator: an unmet open re-emits the same buy every cycle,
and the token it buys is then sold back as "surplus" the next cycle.

This module keeps a small persisted record of every prep swap Sheldon
emitted and how George resolved it (executed / rejected), so gating can
suppress duplicates by *state* instead of by clock:

  - ``pending``  — emitted, not yet resolved. Suppress until TTL.
  - ``executed`` — confirmed on-chain at ``confirmed_at``. Suppress until a
                   wallet scan newer than the confirmation (+ grace) exists.
  - ``rejected`` — George refused it. Allow a retry after a cooldown.
  - ``expired``  — pending entry older than TTL; treated as not-there.

The ledger is advisory state: a missing or corrupt file fails open (no
suppression), never closed.
"""

import glob
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Set

# George's executor root — where the journal that resolves prep outcomes lives.
GEORGE_AGENT_DIR = "/data/.openclaw/workspace-agents/george/agents/meteora-dlmm"
DEFAULT_JOURNAL_DIR = os.path.join(GEORGE_AGENT_DIR, "journal")

STATE_FILENAME = "prep_ledger.json"

# A pending prep older than this is presumed lost (crashed cycle, signal file
# never picked up) and stops suppressing.
PREP_LEDGER_TTL_SECONDS = 1800.0

# A prep must be confirmed before the wallet rescan that re-checks it.
PREP_CONFIRM_GRACE_SECONDS = 60.0

# A rejected prep may be retried only after this cooldown.
PREP_REJECT_COOLDOWN_SECONDS = 900.0

# A mint bought within this window is not sold back as surplus
# (anti-oscillation: the buy exists to fund an open, not to round-trip).
PREP_OSCILLATION_WINDOW_SECONDS = 3600.0

_AMOUNT_RE = re.compile(r"(inAmount|outAmount)=(\d+)")


def _created_epoch(created_at: str) -> float:
    """Parse an ISO-8601 timestamp to a Unix epoch (0.0 on failure)."""
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError, TypeError):
        return 0.0


def state_path(state_dir: Optional[str] = None) -> str:
    """Path to the ledger file for ``state_dir``."""
    if state_dir is None:
        state_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    return os.path.join(state_dir, STATE_FILENAME)


def load_ledger(state_dir: Optional[str] = None) -> Dict[str, Any]:
    """Load the ledger. Missing or malformed fails open to empty state."""
    try:
        with open(state_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"entries": []}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return {"entries": []}
    return data


def save_ledger(ledger: Dict[str, Any], state_dir: Optional[str] = None) -> bool:
    """Persist the ledger atomically (tmp + rename). Returns True on success."""
    try:
        path = state_path(state_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def ledger_key(spec: Dict[str, Any]) -> str:
    """Stable key for a prep spec: mint pair + direction + target pool.

    The pool is part of the key so a buy for pool A does not suppress an
    otherwise-identical buy for pool B.
    """
    return "|".join([
        str(spec.get("input_mint") or ""),
        str(spec.get("output_mint") or ""),
        str(spec.get("direction") or ""),
        str(spec.get("for_pool") or ""),
    ])


def newest_entry(ledger: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    """Newest ledger entry for ``key``, or None."""
    matches = [e for e in ledger.get("entries", [])
               if e.get("key") == key and e.get("emitted_at")]
    if not matches:
        return None
    return max(matches, key=lambda e: e.get("emitted_at") or 0.0)


def record_emitted(spec: Dict[str, Any], signal_id: str,
                   state_dir: Optional[str] = None,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """Append a ``pending`` entry for an emitted prep spec and persist."""
    now = now if now is not None else time.time()
    ledger = load_ledger(state_dir)
    entry = {
        "key": ledger_key(spec),
        "input_mint": spec.get("input_mint"),
        "output_mint": spec.get("output_mint"),
        "direction": spec.get("direction"),
        "prep_for_pool": spec.get("for_pool"),
        "signal_id": signal_id,
        "emitted_at": now,
        "status": "pending",
        "confirmed_at": None,
        "in_amount": None,
        "out_amount": None,
    }
    ledger.setdefault("entries", []).append(entry)
    save_ledger(ledger, state_dir)
    return entry


def _parse_journal_amounts(details: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Pull inAmount/outAmount out of a journal ``result.notes`` string."""
    result = (details or {}).get("result") or {}
    notes = result.get("notes") or ""
    found = dict(_AMOUNT_RE.findall(str(notes)))
    return {
        "in_amount": found.get("inAmount"),
        "out_amount": found.get("outAmount"),
    }


def read_journal(journal_dir: Optional[str] = None,
                 signal_ids: Optional[Set[str]] = None) -> Dict[str, Dict[str, Any]]:
    """Map signal_id -> resolution from George's journal.

    Only lines whose signal_id is in ``signal_ids`` (when given) are kept,
    so a large journal history is not fully materialized.
    """
    journal_dir = journal_dir or DEFAULT_JOURNAL_DIR
    out: Dict[str, Dict[str, Any]] = {}
    for path in sorted(glob.glob(os.path.join(journal_dir, "*.jsonl"))):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    sid = rec.get("signal_id")
                    if not sid:
                        continue
                    if signal_ids is not None and sid not in signal_ids:
                        continue
                    decision = rec.get("decision")
                    if decision not in ("executed", "rejected"):
                        continue
                    amounts = _parse_journal_amounts(rec.get("details") or {})
                    # Keep the newest record per signal_id.
                    prev = out.get(sid)
                    ts = _created_epoch(rec.get("timestamp") or "")
                    if prev and (prev.get("confirmed_at") or 0.0) > ts:
                        continue
                    out[sid] = {
                        "status": decision,
                        "confirmed_at": ts,
                        "in_amount": amounts["in_amount"],
                        "out_amount": amounts["out_amount"],
                    }
        except OSError:
            continue
    return out


def resolve(ledger: Dict[str, Any],
            journal_dir: Optional[str] = None) -> bool:
    """Apply journal outcomes to pending ledger entries. Returns True if changed."""
    pending_ids = {e.get("signal_id") for e in ledger.get("entries", [])
                   if e.get("status") == "pending" and e.get("signal_id")}
    if not pending_ids:
        return False
    resolutions = read_journal(journal_dir, pending_ids)
    changed = False
    for entry in ledger.get("entries", []):
        if entry.get("status") != "pending":
            continue
        res = resolutions.get(entry.get("signal_id"))
        if not res:
            continue
        entry["status"] = res["status"]
        entry["confirmed_at"] = res["confirmed_at"]
        entry["in_amount"] = res["in_amount"]
        entry["out_amount"] = res["out_amount"]
        changed = True
    return changed


def load_and_resolve(state_dir: Optional[str] = None,
                     journal_dir: Optional[str] = None) -> Dict[str, Any]:
    """Load the ledger, resolve pending entries from the journal, persist if changed."""
    ledger = load_ledger(state_dir)
    if resolve(ledger, journal_dir):
        save_ledger(ledger, state_dir)
    return ledger


def suppression_reason(entry: Optional[Dict[str, Any]], wallet_mtime: float,
                       now: Optional[float] = None) -> Optional[str]:
    """Why ``entry`` should suppress a fresh prep spec, or None to allow.

    Mutates a stale ``pending`` entry to ``expired`` as a side effect.
    """
    if entry is None:
        return None
    now = now if now is not None else time.time()
    status = entry.get("status")
    sid = entry.get("signal_id")

    if status == "pending":
        emitted = entry.get("emitted_at") or 0.0
        if now - emitted <= PREP_LEDGER_TTL_SECONDS:
            return (f"prep {sid} still pending (emitted {int(now - emitted)}s ago, "
                    f"TTL {int(PREP_LEDGER_TTL_SECONDS)}s)")
        entry["status"] = "expired"
        return None

    if status == "executed":
        confirmed = entry.get("confirmed_at") or entry.get("emitted_at") or 0.0
        if wallet_mtime and wallet_mtime < confirmed + PREP_CONFIRM_GRACE_SECONDS:
            return (f"confirmation grace: wallet rescan must be taken "
                    f">={int(PREP_CONFIRM_GRACE_SECONDS)}s after prep {sid} "
                    f"confirmed")
        return None

    if status == "rejected":
        resolved = entry.get("confirmed_at") or entry.get("emitted_at") or 0.0
        if now - resolved < PREP_REJECT_COOLDOWN_SECONDS:
            return (f"prep {sid} rejected {int(now - resolved)}s ago; retry after "
                    f"{int(PREP_REJECT_COOLDOWN_SECONDS)}s cooldown")
        return None

    # expired / unknown -> allow
    return None


def recent_buy_mints(ledger: Dict[str, Any], now: Optional[float] = None,
                     window: float = PREP_OSCILLATION_WINDOW_SECONDS) -> Set[str]:
    """Mints bought (executed) within ``window`` — anti-oscillation targets.

    A token bought to fund an open must not be sold straight back to USDC as
    'surplus'; that round trip is the churn this ledger exists to stop.
    """
    now = now if now is not None else time.time()
    out: Set[str] = set()
    for e in ledger.get("entries", []):
        if e.get("direction") != "buy" or e.get("status") != "executed":
            continue
        confirmed = e.get("confirmed_at") or e.get("emitted_at") or 0.0
        if now - confirmed <= window:
            mint = e.get("output_mint")
            if mint:
                out.add(mint)
    return out
