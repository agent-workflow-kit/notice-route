"""The redelivery and repeat checks and the lifetime of their records (#17).

Data-layer operations only, on the connection of a :meth:`Store.write` transaction
(``BEGIN IMMEDIATE``), so a check and the record it leads to are atomic with the rest of the
decision of ``send`` (docs/interface.md: Dedup, ``send`` step 7). The functions read no clock: the
caller passes ``now`` (floored UTC epoch seconds from ``gateway.wall()``), and no function commits.
Every statement is a constant string and every value is a bound parameter.

The repeat digest is :func:`notice_route.dryrun.dedupe_digest`; callers pass its hex text.

Rules:

- **Repeat window**: ``[start, start + length)`` in epoch seconds, fixed (never extended), with the
  opening rule's length stored and never changed. :func:`check_repeat` is one upsert statement:
  it opens a window when there is none or the stored one is not active, and otherwise leaves it
  alone and adds 1 to ``count`` of the delivery that opened it. A window is active only when
  ``start <= now < start + length``: after a backward clock step a window that would last longer
  than its length from now is not trusted (a duplicate, never a lost notification).
- A window opened by :func:`check_repeat` has no ``delivery_id`` until :func:`bind_window` (the
  delivery row is inserted after the decision). Bind it before queueing the row, because
  queueing may evict a row and eviction calls :func:`release`.
- **Redelivery**: ``event_id`` as ``json.dumps`` text (so 17 and "17" differ), only for an
  integer (never a bool) or a plain string; the router's own alerts (source ``notice-route``)
  are never checked or recorded. A record is live while ``now - retention <= seen_at <= now`` (a
  record "older than retention" is what ``prune`` deletes; a record from the future, after a
  backward step, is not trusted). Recording never extends a live record.
- **Release** by ``delivery_id`` deletes both records (``failed``, ``dropped``, ``expired``,
  notifier-side ``disabled`` and cap eviction); records stay after ``sent`` and ``unknown``.

Durations are integer seconds: the caller converts a configured duration once, with
``math.ceil``, before calling (a float is a ``ValueError``).

Seams:

- The decision order (``decide.py``, #18) calls these in one transaction: :func:`check_repeat`,
  ``insert_delivery``, :func:`bind_window`, :func:`record_event_id`, ``enqueue``.
- ``send.py`` (#22) is the pipeline that calls them (through ``decide.py``).
- Every delivery end except ``sent`` and ``unknown`` (``failed``, ``dropped``, ``expired`` and a
  notifier-side ``disabled``) calls :func:`release` in the same transaction: ``outcome.py`` does so
  for a call; TODO(#23) (flush step 4 expiry) and TODO(#26) (``prune`` expiry of queue rows) must.
- TODO(#26) ``prune`` calls :func:`prune_windows` and :func:`prune_event_ids` in loops of one
  transaction each; its dry run uses :func:`count_prunable_windows` and
  :func:`count_prunable_event_ids`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from .store import EVENT_IDS_CAP, trim_event_ids

__all__ = [
    "PRUNE_BATCH",
    "Repeat",
    "Window",
    "active_window",
    "bind_window",
    "check_repeat",
    "count_prunable_event_ids",
    "count_prunable_windows",
    "event_id_text",
    "is_redelivered",
    "prune_event_ids",
    "prune_windows",
    "record_event_id",
    "release",
]

PRUNE_BATCH = 1000  # rows per prune transaction (docs/interface.md: prune)
ROUTER_SOURCE = "notice-route"
_MAX = 1 << 40  # bound of an instant, a length and a retention: far from an int64 overflow
_INT64_MAX = (1 << 63) - 1
_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#-]{0,255}")  # as payload._PLAIN
_DIGEST = re.compile(r"[0-9a-f]{64}")

_Conn = sqlite3.Connection
_RELEASING = frozenset({"failed", "dropped", "expired", "disabled"})  # outcomes that release


def _bounded(value: object, what: str, low: int) -> int:
    if type(value) is not int or not low <= value <= _MAX:
        raise ValueError(f"{what} must be an integer from {low} to 2**40")
    return value


def _now(value: object) -> int:
    return _bounded(value, "now", -_MAX)


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("digest must be 64 lowercase hex characters")
    return value


def _delivery_id(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _INT64_MAX:
        raise ValueError("delivery_id must be an integer from 1 to int64")
    return value


def _text(value: object, what: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{what} must be a string")
    return value


def _hook(value: object) -> str:
    if value is None:
        return ""
    return _text(value, "hook")


def event_id_text(value: object) -> str | None:
    """The JSON text of a plain ``event_id`` (an integer, never a bool, or a plain string), else
    ``None`` (ignored)."""
    if type(value) is int:
        try:
            return json.dumps(value)
        except ValueError:  # past Python's integer-to-text limit
            return None
    if type(value) is str and _PLAIN.fullmatch(value) is not None:
        return json.dumps(value)
    return None


# --- the repeat check ----------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    start: int
    length: int
    delivery_id: int | None


@dataclass(frozen=True)
class Repeat:
    """``opened``: this notification opened a window and goes on. Otherwise it is ``suppressed``,
    reason ``repeat``, and ``opener`` is the delivery of the window it fell in (its ``count`` was
    raised by 1), or ``None`` when that window is not bound to a delivery."""

    opened: bool
    opener: int | None = None


# One statement: open the window, or replace one that is not active; a conflict with an active
# window changes nothing and returns no row.
_OPEN = (
    "INSERT INTO dedup (destination, key_digest, window_start, window_length, delivery_id)"
    " VALUES (?, ?, ?, ?, NULL)"
    " ON CONFLICT (destination, key_digest) DO UPDATE SET"
    " window_start = excluded.window_start, window_length = excluded.window_length,"
    " delivery_id = NULL"
    " WHERE NOT (dedup.window_start <= excluded.window_start"
    " AND excluded.window_start < dedup.window_start + dedup.window_length)"
    " RETURNING id"
)


def check_repeat(
    conn: _Conn, *, destination: str, digest: str, now: int, window_length: int
) -> Repeat:
    """The repeat check of one destination, in one statement (no read-then-write gap)."""
    _text(destination, "destination")
    _digest(digest)
    _now(now)
    _bounded(window_length, "window_length", 1)
    if conn.execute(_OPEN, (destination, digest, now, window_length)).fetchone() is not None:
        return Repeat(True)
    found = conn.execute(
        "SELECT delivery_id FROM dedup WHERE destination = ? AND key_digest = ?",
        (destination, digest),
    ).fetchone()
    opener = None if found is None else found[0]
    if opener is not None:
        conn.execute(
            "UPDATE deliveries SET count = count + 1 WHERE id = ? AND count < ?",
            (opener, _INT64_MAX),
        )
    return Repeat(False, opener)


def active_window(conn: _Conn, *, destination: str, digest: str, now: int) -> Window | None:
    """The window a notification at ``now`` falls in, without writing (``test`` and
    ``send --dry-run`` read it on a read-only connection)."""
    found = conn.execute(
        "SELECT window_start, window_length, delivery_id FROM dedup"
        " WHERE destination = ? AND key_digest = ?"
        " AND window_start <= ? AND ? < window_start + window_length",
        (_text(destination, "destination"), _digest(digest), _now(now), now),
    ).fetchone()
    return None if found is None else Window(*found)


def bind_window(conn: _Conn, *, destination: str, digest: str, delivery_id: int) -> bool:
    """Attach the delivery to the window :func:`check_repeat` just opened (only an unbound one).

    ``False`` and nothing bound when the delivery row is missing or already ended in an outcome
    that releases its records (``failed``, ``dropped``, ``expired``, ``disabled``; for example
    evicted by the cap while it was queued): the still-unbound window is deleted, so repeats are
    never suppressed against a dead delivery."""
    _delivery_id(delivery_id)
    _text(destination, "destination")
    _digest(digest)
    row = conn.execute("SELECT outcome FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
    if row is None or row[0] in _RELEASING:
        conn.execute(
            "DELETE FROM dedup WHERE destination = ? AND key_digest = ? AND delivery_id IS NULL",
            (destination, digest),
        )
        return False
    cur = conn.execute(
        "UPDATE dedup SET delivery_id = ? WHERE destination = ? AND key_digest = ?"
        " AND delivery_id IS NULL",
        (delivery_id, destination, digest),
    )
    return cur.rowcount == 1


# --- the redelivery check ------------------------------------------------------------------


def is_redelivered(
    conn: _Conn,
    *,
    destination: str,
    source: str,
    hook: str | None,
    event_id: object,
    digest: str,
    now: int,
    retention: int,
) -> bool:
    """Both the event id and the repeat digest were recorded for this destination, source and
    hook, and the record is live. Never for a non-plain id or the router's own source."""
    _text(destination, "destination")
    _text(source, "source")
    hook_text = _hook(hook)
    _digest(digest)
    _now(now)
    _bounded(retention, "retention", 1)
    text = event_id_text(event_id)
    if text is None or source == ROUTER_SOURCE:
        return False
    found = conn.execute(
        "SELECT 1 FROM event_ids WHERE destination = ? AND source = ? AND hook = ?"
        " AND event_id = ? AND repeat_digest = ? AND seen_at >= ? AND seen_at <= ?",
        (destination, source, hook_text, text, digest, now - retention, now),
    ).fetchone()
    return found is not None


# A conflict with a live record changes nothing (its lifetime is never extended) and returns no
# row; an expired or untrusted one is replaced and takes a fresh row id, so the cap (which evicts
# the lowest id) never evicts a replaced record before older ones.
_RECORD = (
    "INSERT INTO event_ids (destination, source, hook, event_id, repeat_digest, seen_at,"
    " delivery_id) VALUES (?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT (destination, source, hook, event_id, repeat_digest) DO UPDATE SET"
    " id = (SELECT MAX(id) + 1 FROM event_ids),"
    " seen_at = excluded.seen_at, delivery_id = excluded.delivery_id"
    " WHERE NOT (event_ids.seen_at >= excluded.seen_at - ?"
    " AND event_ids.seen_at <= excluded.seen_at)"
    " RETURNING id"
)


def record_event_id(
    conn: _Conn,
    *,
    destination: str,
    source: str,
    hook: str | None,
    event_id: object,
    digest: str,
    now: int,
    retention: int,
    delivery_id: int,
    cap: int = EVENT_IDS_CAP,
) -> bool:
    """Record the id and trim the table to ``cap`` (never the row just written). ``False`` when
    nothing was written: an ignored id, the router's own source, or a live record already there
    (the caller should have suppressed; this is never an error under a race). ``delivery_id`` is
    required: a record nothing releases would outlive a failed delivery."""
    _text(destination, "destination")
    _text(source, "source")
    hook_text = _hook(hook)
    owner = _delivery_id(delivery_id)
    _digest(digest)
    _now(now)
    _bounded(retention, "retention", 1)
    text = event_id_text(event_id)
    if text is None or source == ROUTER_SOURCE:
        return False
    row = conn.execute(
        _RECORD, (destination, source, hook_text, text, digest, now, owner, retention)
    ).fetchone()
    if row is None:
        return False
    trim_event_ids(conn, cap=cap, keep_id=row[0])
    return True


# --- lifetime --------------------------------------------------------------------------------


def release(conn: _Conn, delivery_id: int) -> int:
    """Delete the window and event-id records written for ``delivery_id``; returns the count.
    A window that a later one replaced no longer carries the id and stays."""
    _delivery_id(delivery_id)
    n = conn.execute("DELETE FROM dedup WHERE delivery_id = ?", (delivery_id,)).rowcount
    n += conn.execute("DELETE FROM event_ids WHERE delivery_id = ?", (delivery_id,)).rowcount
    return n


_ENDED_WINDOWS = "window_start + window_length <= ?"
_OLD_EVENT_IDS = "seen_at < ?"


def count_prunable_windows(conn: _Conn, *, now: int) -> int:
    """How many windows :func:`prune_windows` would delete in total (for ``prune --dry-run``)."""
    return int(
        conn.execute("SELECT COUNT(*) FROM dedup WHERE " + _ENDED_WINDOWS, (_now(now),)).fetchone()[
            0
        ]
    )


def count_prunable_event_ids(conn: _Conn, *, now: int, retention: int) -> int:
    """How many event ids :func:`prune_event_ids` would delete in total."""
    _now(now)
    _bounded(retention, "retention", 1)
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM event_ids WHERE " + _OLD_EVENT_IDS, (now - retention,)
        ).fetchone()[0]
    )


def prune_windows(conn: _Conn, *, now: int, batch: int = PRUNE_BATCH) -> int:
    """Delete up to ``batch`` ended windows (``start + length <= now``), oldest first."""
    _now(now)
    _bounded(batch, "batch", 1)
    return conn.execute(
        "DELETE FROM dedup WHERE id IN (SELECT id FROM dedup WHERE "
        + _ENDED_WINDOWS
        + " ORDER BY id LIMIT ?)",
        (now, batch),
    ).rowcount


def prune_event_ids(conn: _Conn, *, now: int, retention: int, batch: int = PRUNE_BATCH) -> int:
    """Delete up to ``batch`` event ids older than ``retention`` (``seen_at < now - retention``)."""
    _now(now)
    _bounded(retention, "retention", 1)
    _bounded(batch, "batch", 1)
    return conn.execute(
        "DELETE FROM event_ids WHERE id IN (SELECT id FROM event_ids WHERE "
        + _OLD_EVENT_IDS
        + " ORDER BY id LIMIT ?)",
        (now - retention, batch),
    ).rowcount
