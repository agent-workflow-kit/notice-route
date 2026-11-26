"""Operator overrides of a destination (docs/interface.md: ``destination``; design C-61).

The router never writes its config, so ``destination disable`` records a row in
``destination_overrides`` and ``destination enable`` deletes it. A row means *disabled*; there is
no way to enable what the config disables, so :func:`disabled_among` is only ever added to the
config's own ``enabled: false``.

Rules every caller gets:

- A row of the table disables, whatever its ``value`` (the CHECK admits one word; a hand-edited
  file fails closed).
- The lookup is one primary-key probe per name, never a scan of a table that anything with write
  access to the state directory can fill. Callers pass the configured destinations (at most 32).
- Nothing here commits: the functions take the connection of the caller's transaction (or, for
  reads, a read-only connection) and every value reaches SQL as a bound parameter. A read failure
  that means the database cannot be used is a :class:`StoreError`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from .store import StoreError

__all__ = ["Override", "clear", "disabled_among", "get", "set_disabled"]

_Conn = sqlite3.Connection

Override = tuple[object, object, object, object]
"""``(value, actor, decision_id, set_at)`` exactly as stored: the caller checks the grammar."""


def _fail() -> StoreError:
    return StoreError("internal")


def get(conn: _Conn, destination: str) -> Override | None:
    """The stored override of ``destination``, or ``None``."""
    try:
        found = conn.execute(
            "SELECT value, actor, decision_id, set_at FROM destination_overrides"
            " WHERE destination = ?",
            (destination,),
        ).fetchone()
    except sqlite3.Error:
        raise _fail() from None
    return None if found is None else (found[0], found[1], found[2], found[3])


def disabled_among(conn: _Conn, names: Iterable[str]) -> frozenset[str]:
    """The names, of those given, that carry an override."""
    out: set[str] = set()
    for name in dict.fromkeys(names):
        try:
            found = conn.execute(
                "SELECT 1 FROM destination_overrides WHERE destination = ?", (name,)
            ).fetchone()
        except sqlite3.Error:
            raise _fail() from None
        if found is not None:
            out.add(name)
    return frozenset(out)


def set_disabled(
    conn: _Conn, destination: str, *, actor: str, decision_id: str | None, now: int
) -> bool:
    """Write the override inside the caller's write transaction. ``False`` (nothing changed) when
    one exists already."""
    cur = conn.execute(
        "INSERT INTO destination_overrides (destination, value, actor, decision_id, set_at)"
        " VALUES (?, 'disabled', ?, ?, ?) ON CONFLICT (destination) DO NOTHING",
        (destination, actor, decision_id, now),
    )
    return cur.rowcount == 1


def clear(conn: _Conn, destination: str) -> bool:
    """Delete the override inside the caller's write transaction; ``False`` when there is none."""
    cur = conn.execute("DELETE FROM destination_overrides WHERE destination = ?", (destination,))
    return cur.rowcount == 1
