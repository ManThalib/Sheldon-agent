"""Read-only on-chain proof that a tracked position no longer exists.

Why this exists
---------------
Sheldon's settlement ladder is a *live* state machine: it advances on journal
decisions written by the executor after a real close. In ``dry_run`` the
executor consumes a close, writes a journal line with no decision, and the
ladder can never advance — so every emitted close blocks opens forever, and
the next cycle emits the same close again. By 2026-10-19 that produced 18
``CLOSE_EMITTED`` ghosts over 9 hourly cycles and the gate blocked every open.

The fix is NOT to relax the gate. It is to add a second, independent source of
truth: the chain itself. A position account either exists or it does not. If
the position is provably gone — and the *pool* account still resolves on the
same endpoint, so a broken/limited RPC cannot masquerade as absence — then the
close happened, whoever executed it.

Safety properties
-----------------
* Read-only. Only ``getMultipleAccounts``. Never signs, never sends, never
  loads a keypair, never needs the wallet secret.
* Fail-closed. Any RPC error, timeout, malformed reply, or unusable
  identifier returns ``status="unknown"`` and resolves nothing. Absence is
  only ever concluded from an explicit ``null`` account at *two* commitments
  plus a positive control.
* Never logs the RPC URL — not raw, not truncated. A truncated URL is still
  disclosure.
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Sequence

# Two commitments must agree, and the control account must resolve, before
# absence is accepted. One endpoint reply is not proof.
REQUIRED_COMMITMENTS: Sequence[str] = ("confirmed", "finalized")

DEFAULT_TIMEOUT_SECONDS = 20.0

MASK = "***"


class ChainTruthUnavailable(Exception):
    """RPC not configured or unusable; nothing may be concluded."""


def _rpc(endpoint: str, method: str, params: list,
         timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError) as exc:
        # Deliberately includes no URL and no response body.
        raise ChainTruthUnavailable(
            f"rpc {method} failed ({type(exc).__name__})") from None
    if not isinstance(payload, dict):
        raise ChainTruthUnavailable(f"rpc {method} returned non-object")
    if payload.get("error"):
        err = payload["error"]
        msg = err.get("message") if isinstance(err, dict) else None
        raise ChainTruthUnavailable(
            f"rpc {method} error: {str(msg)[:80] or 'unknown'}")
    return payload.get("result")


def _multiple(endpoint: str, pubkeys: list, commitment: str,
              timeout: float) -> Optional[list]:
    result = _rpc(endpoint, "getMultipleAccounts",
                  [pubkeys, {"encoding": "base64", "commitment": commitment}],
                  timeout=timeout)
    if not isinstance(result, dict):
        raise ChainTruthUnavailable("getMultipleAccounts shape")
    value = result.get("value")
    if not isinstance(value, list) or len(value) != len(pubkeys):
        raise ChainTruthUnavailable("getMultipleAccounts value")
    return value


def _looks_like_address(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    addr = value.strip()
    if not (32 <= len(addr) <= 44):
        return False
    alphabet = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
                   "abcdefghijkmnopqrstuvwxyz")
    return set(addr) <= alphabet


def check_position_absent(endpoint: Optional[str], position_id: Any,
                          pool_address: Any,
                          timeout: float = DEFAULT_TIMEOUT_SECONDS,
                          ) -> Dict[str, Any]:
    """Return an evidence dict; never raises for ordinary failures.

    ``absent`` is True only when:

    * both identifiers are plausible base58 addresses,
    * the *pool* account resolves at ``finalized`` (positive control — the
      endpoint is alive and serving account data), and
    * the position account is ``null`` at **confirmed** *and* ``finalized``.
    """
    evidence: Dict[str, Any] = {
        "checked_at": time.time(),
        "rpc": MASK,
        "absent": False,
        "status": "unknown",
        "reason": "",
        "commitments": list(REQUIRED_COMMITMENTS),
    }
    if not endpoint:
        evidence["reason"] = "no rpc endpoint configured"
        return evidence
    if not _looks_like_address(position_id):
        evidence["reason"] = "position_id missing or not a base58 address"
        return evidence
    if not _looks_like_address(pool_address):
        evidence["reason"] = "pool_address missing or not a base58 address"
        return evidence

    position = position_id.strip()
    pool = pool_address.strip()
    try:
        # Positive control + position at finalized, in one round trip.
        finalized = _multiple(endpoint, [pool, position], "finalized", timeout)
        pool_present = finalized[0] is not None
        position_finalized_null = finalized[1] is None
        evidence["pool_present"] = pool_present
        if not pool_present:
            # The endpoint cannot see a pool we know exists (scans still
            # report it), so its silence about the position proves nothing.
            evidence["reason"] = (
                "control failed: pool account not visible at finalized; "
                "absence not provable")
            return evidence
        if not position_finalized_null:
            evidence["status"] = "present"
            evidence["reason"] = "position account still exists on chain"
            return evidence

        confirmed = _multiple(endpoint, [position], "confirmed", timeout)
        if confirmed[0] is not None:
            evidence["status"] = "present"
            evidence["reason"] = "position account visible at confirmed"
            return evidence

        evidence["absent"] = True
        evidence["status"] = "absent"
        evidence["reason"] = (
            "position account null at confirmed and finalized; pool control "
            "resolved at finalized")
        return evidence
    except ChainTruthUnavailable as exc:
        evidence["reason"] = str(exc)
        return evidence
