"""The ``post-fail`` outbox (#37, docs/design.md C-62): the statements of the ``hook_outbox`` table.

A synchronous hook is at least once: the transaction that takes a ``surfaced`` row writes one row
per synchronous entry of ``hooks.post-fail``, and every ``flush`` runs the rows that are due until
a hook exits 0 (the row is deleted) or a limit ends the row. Every function writes only through
the connection of a :meth:`notice_route.store.Store.write` transaction it is given, with the
clock passed in as an integer (the wall seconds, floored).

Limits (documented in docs/interface.md, Hooks): at most :data:`MAX_ATTEMPTS` attempts, the delay
after attempt ``n`` is ``DELAYS[n - 1]``, and no attempt for a row :data:`MAX_AGE_S` old. The
attempt count rises when an attempt is *claimed*, so a run that dies during a hook still uses the
attempt up. A claim is a lease (``claimed_by`` is a random token, ``claim_expires`` its end); a
lapsed claim counts as unclaimed, and a claim is finished only by the token that made it.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from .validate import Hook

__all__ = [
    "DELAYS",
    "LEASE_GRACE_S",
    "MAX_AGE_S",
    "MAX_ATTEMPTS",
    "MAX_PAYLOAD",
    "RUN_LIMIT",
    "Claimed",
    "Row",
    "claim",
    "delete",
    "enqueue",
    "end_lapsed",
    "fail",
    "hook_keys",
    "succeed",
    "due",
]

DELAYS = (60, 300, 1800, 7200, 21600)  # seconds after attempt 1 .. 5
MAX_ATTEMPTS = len(DELAYS) + 1
MAX_AGE_S = 24 * 3600  # as a queue row: EXPIRY_S
RUN_LIMIT = 100  # rows one flush runs
LEASE_GRACE_S = 60  # added to the entry's timeout
MAX_PAYLOAD = 8192  # characters of a stored payload


@dataclass(frozen=True)
class Row:
    id: int
    destination: str
    hook_key: str
    payload: str


@dataclass(frozen=True)
class Claimed:
    attempts: int
    payload: str


def hook_keys(hooks: Sequence[Hook]) -> list[str | None]:
    """The identity of each entry, parallel to ``hooks``: the first 16 hex characters of the
    SHA-256 of the ``cmd`` and the entry's number among the entries with the same ``cmd``;
    ``None`` for an ``async`` entry (not outboxed)."""
    seen: dict[str, int] = {}
    keys: list[str | None] = []
    for hook in hooks:
        digest = hashlib.sha256(hook.cmd.encode("utf-8", "replace")).hexdigest()[:16]
        n = seen.get(digest, 0)
        seen[digest] = n + 1
        keys.append(None if hook.is_async else f"{digest}.{n}")
    return keys


def enqueue(
    conn: sqlite3.Connection,
    surfaced_id: int,
    destination: str,
    hook_key: str,
    event_id: int,
    payload: str,
    now: int,
) -> None:
    """One row, due now; a repeat of the same (surfaced row, entry) is ignored."""
    conn.execute(
        "INSERT OR IGNORE INTO hook_outbox"
        " (surfaced_id, destination, hook_key, event_id, payload, created_at, next_attempt)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (surfaced_id, destination, hook_key, event_id, payload, now, now),
    )


_UNCLAIMED = "(claim_expires IS NULL OR claim_expires <= ?)"


def end_lapsed(conn: sqlite3.Connection, now: int) -> list[tuple[int, str, str]]:
    """Delete the unclaimed rows that reached a limit without a run: ``expired`` (a day old) or
    ``exhausted`` (all attempts claimed, the last never finished). Returns (id, destination,
    word) of each."""
    found = conn.execute(
        "SELECT id, destination, CASE WHEN created_at <= ? THEN 'expired' ELSE 'exhausted' END"
        f" FROM hook_outbox WHERE {_UNCLAIMED} AND (created_at <= ? OR attempts >= ?)"
        " ORDER BY id",
        (now - MAX_AGE_S, now, now - MAX_AGE_S, MAX_ATTEMPTS),
    ).fetchall()
    for row_id, _, _ in found:
        conn.execute("DELETE FROM hook_outbox WHERE id = ?", (row_id,))
    return [(int(i), str(d), str(w)) for i, d, w in found]


def due(conn: sqlite3.Connection, now: int, limit: int = RUN_LIMIT) -> list[Row]:
    """The unclaimed rows whose next attempt is not after ``now``, oldest first."""
    found = conn.execute(
        "SELECT id, destination, hook_key, payload FROM hook_outbox"
        f" WHERE next_attempt <= ? AND {_UNCLAIMED} ORDER BY id LIMIT ?",
        (now, now, limit),
    ).fetchall()
    return [Row(int(i), str(d), str(k), str(p)) for i, d, k, p in found]


def claim(
    conn: sqlite3.Connection, row_id: int, now: int, lease_s: int, token: str
) -> Claimed | None:
    """Claim one due row for an attempt (count it and lease it); ``None`` when another run got
    it first, it is not due, or it reached a limit."""
    cur = conn.execute(
        "UPDATE hook_outbox SET attempts = attempts + 1, claimed_by = ?, claim_expires = ?"
        f" WHERE id = ? AND next_attempt <= ? AND {_UNCLAIMED}"
        " AND attempts < ? AND created_at > ?",
        (token, now + lease_s, row_id, now, now, MAX_ATTEMPTS, now - MAX_AGE_S),
    )
    if cur.rowcount != 1:
        return None
    found = conn.execute(
        "SELECT attempts, payload FROM hook_outbox WHERE id = ?", (row_id,)
    ).fetchone()
    return Claimed(int(found[0]), str(found[1]))


def succeed(conn: sqlite3.Connection, row_id: int, token: str) -> None:
    conn.execute("DELETE FROM hook_outbox WHERE id = ? AND claimed_by = ?", (row_id, token))


def fail(conn: sqlite3.Connection, row_id: int, token: str, now: int) -> bool:
    """End a failed attempt: release the claim and set the next attempt, or delete the row when
    that was the last one. ``True`` when the row was deleted (gave up)."""
    found = conn.execute(
        "SELECT attempts FROM hook_outbox WHERE id = ? AND claimed_by = ?", (row_id, token)
    ).fetchone()
    if found is None:
        return False  # the claim lapsed and another run owns the row now
    attempts = int(found[0])
    if attempts >= MAX_ATTEMPTS:
        conn.execute("DELETE FROM hook_outbox WHERE id = ?", (row_id,))
        return True
    conn.execute(
        "UPDATE hook_outbox SET next_attempt = ?, claimed_by = NULL, claim_expires = NULL"
        " WHERE id = ?",
        (now + DELAYS[attempts - 1], row_id),
    )
    return False


def delete(conn: sqlite3.Connection, row_id: int) -> None:
    conn.execute("DELETE FROM hook_outbox WHERE id = ?", (row_id,))
