"""Single-row lease for the JKG worker, stored in ``system_settings`` (P0-2).

Why a lease at all
-----------------
``JkgWorker`` is **pull-based**: no thread, no timer, no asyncio task. That
makes it trivially safe to call, and it also means two callers can call it at
the same time: the HTTP admin route, a cron job, and a developer running it by
hand are three independent drivers. Two concurrent lifecycle passes are not
merely wasteful — they both advance the same ``jkg.sync.cursor``, so one pass
can move the cursor past rows the other has not processed yet.

The lease answers exactly one question: *is some other run already in
progress?* It is not a distributed lock and it does not serialise SQLite — the
database does that. It is the admission ticket for the worker slot.

Zero DDL
--------
A new table would require a schema change, and the core line is frozen at v6.
The lease therefore lives in the existing ``system_settings`` key/value table
under ``jkg.worker.lease``, exactly like ``jmr.scheduler.*`` and
``jkg.sync.*`` before it. One row, one JSON document::

    {"owner": "<hex>", "acquired_at": "...", "expires_at": "...",
     "heartbeat_at": "...", "ttl_seconds": 300, "stolen_from": null}

``stolen_from`` is part of the **new** holder's record: it names the owner
whose expired lease was displaced. Without it a takeover is indistinguishable
from a first acquisition, which is the one fact an operator needs after a
crash.

TOCTOU
------
Read-then-write is a race in general. Two things close it here:

1. both the read and the write happen inside one ``manager.session()``, so
   they share one transaction on one connection;
2. the write is itself a compare-and-swap on the *exact string* that was read
   (``UPDATE ... WHERE key = :k AND value = :expected`` for an existing row,
   ``INSERT ... ON CONFLICT DO NOTHING`` for the empty case). ``rowcount``
   says whether the swap won.

So even if two processes interleave between read and write, only one of them
can change the row, and the loser learns it lost (``reason="RACE_LOST"``)
instead of silently believing it holds the lease. That matters more than
elegance: a double-run that thinks it won would corrupt the cursor.

Expiry is cooperative
---------------------
Nothing kills a crashed process; the lease simply expires. An expired lease may
be taken by the next caller, which records ``stolen_from``. ``expires_at``
rather than a wall-clock age is what is compared, so a clock that jumps does
not silently extend a dead run.

A lease record that cannot be decoded, or whose ``expires_at`` cannot be
parsed, is treated as **expired**. That is the deliberate fail-open direction:
a corrupt lease must never wedge the worker forever. The cost of being wrong is
one duplicated pass guarded by the cursor's overlap window; the cost of
fail-closed would be a permanently stuck worker that only a human could clear.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from sqlalchemy import text

from ..core.timeutils import utc_now_iso

#: The one key this module owns. ``jkg.sync.*`` belongs to the lifecycle and
#: ``jkg.worker.last_run`` / ``last_success`` / ``last_failed`` to the worker.
LEASE_KEY = "jkg.worker.lease"

#: Default time-to-live for one acquisition, in seconds.
DEFAULT_TTL_SECONDS = 300

#: ``acquire`` outcomes. ``ACQUIRED`` is the only success.
ACQUIRED = "ACQUIRED"
#: Another owner holds an unexpired lease.
HELD = "HELD"
#: Our compare-and-swap lost: the row changed between the read and the write.
RACE_LOST = "RACE_LOST"
#: The caller passed an empty owner, or the store itself failed.
INVALID = "INVALID"

_SELECT = "SELECT value FROM system_settings WHERE key = :k"
_INSERT_IF_ABSENT = (
    "INSERT INTO system_settings(key, value, updated_at) VALUES (:k, :v, :u) "
    "ON CONFLICT(key) DO NOTHING"
)
_COMPARE_AND_SET = (
    "UPDATE system_settings SET value = :v, updated_at = :u "
    "WHERE key = :k AND value = :expected"
)
_COMPARE_AND_DELETE = "DELETE FROM system_settings WHERE key = :k AND value = :expected"


def parse_iso(value: Any) -> Optional[datetime]:
    """Parse a stored timestamp into an aware UTC datetime; ``None`` on failure.

    A naive value (no offset) is read as UTC: every timestamp this project
    writes comes from ``app.core.timeutils``, which is UTC-aware, and mixing
    naive and aware datetimes raises ``TypeError`` on comparison — a health
    probe must not be able to raise.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def shift_iso(value: str, seconds: int) -> Optional[str]:
    """Return ``value`` moved forward by ``seconds``; ``None`` if unparseable."""
    parsed = parse_iso(value)
    if parsed is None:
        return None
    try:
        return (parsed + timedelta(seconds=int(seconds))).isoformat()
    except (OverflowError, ValueError):
        return None


def decode_lease(raw: Any) -> Optional[Dict[str, Any]]:
    """Decode a stored lease document; ``None`` if it is absent or unusable."""
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if not str(payload.get("owner") or ""):
        return None
    return payload


def lease_expired(payload: Dict[str, Any], now_iso: str) -> bool:
    """Is this lease expired at ``now_iso``?

    Unparseable ``expires_at`` counts as expired (see the module docstring).
    """
    expires = parse_iso(payload.get("expires_at"))
    now = parse_iso(now_iso)
    if expires is None or now is None:
        return True
    return expires <= now


class JkgLeaseStore:
    """Read/acquire/renew/release the single ``jkg.worker.lease`` row.

    ``clock`` is injectable so a test can move time forward without sleeping;
    production uses ``utc_now_iso``.
    """

    def __init__(self, manager, *, clock: Optional[Callable[[], str]] = None) -> None:
        self.manager = manager
        self._clock = clock or utc_now_iso

    # ------------------------------------------------------------------ helpers
    def now(self) -> str:
        """Current timestamp in the store's clock."""
        return str(self._clock())

    def _read_raw(self, session) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        raw = session.execute(text(_SELECT), {"k": LEASE_KEY}).scalar()
        if raw is None:
            return None, None
        return str(raw), decode_lease(raw)

    @staticmethod
    def _encode(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    # ---------------------------------------------------------------- read-only
    def read(self) -> Optional[Dict[str, Any]]:
        """The stored lease document, decoded, or ``None`. Never raises."""
        try:
            with self.manager.session() as session:
                _, payload = self._read_raw(session)
        except Exception:  # noqa: BLE001 - a status probe must not raise
            return None
        return payload

    def is_expired(self, payload: Dict[str, Any], *, now_iso: str = "") -> bool:
        """Whether ``payload`` is expired according to this store's clock."""
        return lease_expired(payload, now_iso or self.now())

    # ------------------------------------------------------------------ acquire
    def acquire(
        self, owner: str, ttl_seconds: int = DEFAULT_TTL_SECONDS
    ) -> Dict[str, Any]:
        """Try to become the lease holder.

        Returns a dict, never raises::

            {"acquired": bool, "reason": ACQUIRED|HELD|RACE_LOST|INVALID,
             "owner": str, "holder": str, "stolen_from": str,
             "expires_at": str, "lease": dict|None}

        ``holder`` is the current owner when the attempt failed. ``stolen_from``
        is the displaced owner when an expired lease was taken over, else ``""``.
        """
        owner = str(owner or "").strip()
        outcome: Dict[str, Any] = {
            "acquired": False,
            "reason": INVALID,
            "owner": owner,
            "holder": "",
            "stolen_from": "",
            "expires_at": "",
            "lease": None,
        }
        if not owner:
            return outcome
        try:
            ttl = int(ttl_seconds)
        except (TypeError, ValueError):
            ttl = DEFAULT_TTL_SECONDS
        ttl = max(1, ttl)

        try:
            with self.manager.session() as session:
                raw, payload = self._read_raw(session)
                now_iso = self.now()
                if payload is not None and not lease_expired(payload, now_iso):
                    outcome["reason"] = HELD
                    outcome["holder"] = str(payload.get("owner") or "")
                    outcome["expires_at"] = str(payload.get("expires_at") or "")
                    outcome["lease"] = payload
                    return outcome

                stolen = str((payload or {}).get("owner") or "")
                expires_at = shift_iso(now_iso, ttl) or now_iso
                record = {
                    "owner": owner,
                    "acquired_at": now_iso,
                    "expires_at": expires_at,
                    "heartbeat_at": now_iso,
                    "ttl_seconds": ttl,
                    "stolen_from": stolen or None,
                }
                encoded = self._encode(record)
                if raw is None:
                    result = session.execute(
                        text(_INSERT_IF_ABSENT),
                        {"k": LEASE_KEY, "v": encoded, "u": now_iso},
                    )
                else:
                    result = session.execute(
                        text(_COMPARE_AND_SET),
                        {"k": LEASE_KEY, "v": encoded, "u": now_iso, "expected": raw},
                    )
                won = int(getattr(result, "rowcount", 0) or 0) == 1
                outcome.update(
                    {
                        "acquired": won,
                        "reason": ACQUIRED if won else RACE_LOST,
                        "stolen_from": stolen if won else "",
                        "expires_at": expires_at if won else "",
                        "lease": record if won else payload,
                    }
                )
                return outcome
        except Exception:  # noqa: BLE001 - admission control must not raise
            outcome["reason"] = INVALID
            return outcome

    # ---------------------------------------------------------------- heartbeat
    def heartbeat(
        self, owner: str, ttl_seconds: Optional[int] = None
    ) -> bool:
        """Renew the lease for ``owner``; ``False`` if it is not the live holder.

        Renewal moves both ``heartbeat_at`` and ``expires_at``. A holder that
        lets its own lease expire loses it: the next caller may already have
        taken it over, so renewing a lapsed lease would hand out a second
        admission ticket.
        """
        owner = str(owner or "").strip()
        if not owner:
            return False
        try:
            with self.manager.session() as session:
                raw, payload = self._read_raw(session)
                if raw is None or payload is None:
                    return False
                if str(payload.get("owner") or "") != owner:
                    return False
                now_iso = self.now()
                if lease_expired(payload, now_iso):
                    return False
                try:
                    ttl = int(
                        ttl_seconds
                        if ttl_seconds is not None
                        else payload.get("ttl_seconds") or DEFAULT_TTL_SECONDS
                    )
                except (TypeError, ValueError):
                    ttl = DEFAULT_TTL_SECONDS
                ttl = max(1, ttl)
                renewed = dict(payload)
                renewed["heartbeat_at"] = now_iso
                renewed["expires_at"] = shift_iso(now_iso, ttl) or now_iso
                renewed["ttl_seconds"] = ttl
                result = session.execute(
                    text(_COMPARE_AND_SET),
                    {
                        "k": LEASE_KEY,
                        "v": self._encode(renewed),
                        "u": now_iso,
                        "expected": raw,
                    },
                )
                return int(getattr(result, "rowcount", 0) or 0) == 1
        except Exception:  # noqa: BLE001 - renewal must not raise
            return False

    # ------------------------------------------------------------------ release
    def release(self, owner: str) -> bool:
        """Drop the lease if ``owner`` holds it; ``False`` otherwise.

        Released even when the lease has already expired: the caller is the
        owner and is telling us its run is over, so leaving a dead row behind
        would only delay the next acquisition.
        """
        owner = str(owner or "").strip()
        if not owner:
            return False
        try:
            with self.manager.session() as session:
                raw, payload = self._read_raw(session)
                if raw is None or payload is None:
                    return False
                if str(payload.get("owner") or "") != owner:
                    return False
                result = session.execute(
                    text(_COMPARE_AND_DELETE), {"k": LEASE_KEY, "expected": raw}
                )
                return int(getattr(result, "rowcount", 0) or 0) == 1
        except Exception:  # noqa: BLE001 - release must not raise
            return False

    # -------------------------------------------------------------- clear stale
    def clear_expired(self) -> Optional[Dict[str, Any]]:
        """Delete an expired lease and return it; ``None`` if nothing was cleared.

        A live lease is never touched. The delete is a compare-and-swap on the
        exact stored string, so a lease taken over between the read and this
        delete is left alone.
        """
        try:
            with self.manager.session() as session:
                raw, payload = self._read_raw(session)
                if raw is None or payload is None:
                    return None
                if not lease_expired(payload, self.now()):
                    return None
                result = session.execute(
                    text(_COMPARE_AND_DELETE), {"k": LEASE_KEY, "expected": raw}
                )
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    return None
                return payload
        except Exception:  # noqa: BLE001 - recovery must not raise
            return None


__all__ = [
    "LEASE_KEY",
    "DEFAULT_TTL_SECONDS",
    "ACQUIRED",
    "HELD",
    "RACE_LOST",
    "INVALID",
    "JkgLeaseStore",
    "decode_lease",
    "lease_expired",
    "parse_iso",
    "shift_iso",
]
