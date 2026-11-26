"""Surfacing: the ``surfaced`` rows (docs/interface.md, Hooks; docs/design.md C-30, C-57).

A failure that the router surfaces becomes one ``surfaced`` row, inserted by the transaction that
records the failure (``send``, ``flush`` and ``prune`` only insert; the next ``flush`` routes the
alert and runs the hooks, :mod:`notice_route.alerts`). Every function here writes only through
the connection it is given, so a rolled-back transaction takes the row with it and a recording
that is run twice (``send.with_retry``) inserts at most one row.

Limits (a ``surfaced`` row is unique per destination, code and period):

- ``rate_capped``, ``env_missing``, ``overflow`` and ``expired`` (the hourly codes): at most one
  per destination per UTC hour, period ``YYYY-MM-DDThh``;
- every other code: at most one per destination per UTC day, period ``YYYY-MM-DD``; at most 8
  distinct such codes per destination per day, after which a further code is surfaced once that
  day as ``other`` (the hourly codes are not counted, and ``other`` itself is not counted).

Choices where the interface is silent (the narrower reading):

- A ``failed`` or ``unknown`` result whose code is one of the four hourly words (a notifier may
  send them verbatim, ``outcome.py``) or has no code is surfaced as ``other``: the hourly words
  are the router's own events, and a notifier must not be able to use up their hourly slot or
  make a failure look like one (the hook payload's ``outcome`` rule ties them to the outcome).
- The event is keyed on the outcome (``failed``/``unknown``, ``deferred``, ``dropped``,
  ``expired``), never on the code alone.
- An hourly row about a destination's queue (``overflow``, ``expired``, ``env_missing``) is
  keyed on the oldest row that is not a failure alert when there is one, since an alert's row
  only runs the hooks and would spend the hour's slot on no alert.
- The delivery of a new row is marked ``surfaced`` only when the row was inserted.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from .queue import Evicted

__all__ = [
    "ALERT_SOURCE",
    "DAILY_CODE_LIMIT",
    "HOURLY_CODES",
    "OTHER",
    "day_period",
    "hour_period",
    "surface_daily",
    "surface_evictions",
    "surface_hourly",
    "surface_outcome",
]

ALERT_SOURCE = "notice-route"  # a failure alert's own source: surfacing it only runs the hooks
HOURLY_CODES = ("rate_capped", "env_missing", "overflow", "expired")
DAILY_CODE_LIMIT = 8
OTHER = "other"
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def day_period(now: int) -> str:
    """The UTC day of ``now``: ``YYYY-MM-DD``."""
    return (_EPOCH + timedelta(seconds=now)).strftime("%Y-%m-%d")


def hour_period(now: int) -> str:
    """The UTC clock hour of ``now``: ``YYYY-MM-DDThh``."""
    return (_EPOCH + timedelta(seconds=now)).strftime("%Y-%m-%dT%H")


def _insert(
    conn: sqlite3.Connection, destination: str, code: str, period: str, delivery_id: int
) -> bool:
    cur = conn.execute(
        "INSERT INTO surfaced (destination, code, period, delivery_id) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (destination, code, period) DO NOTHING",
        (destination, code, period, delivery_id),
    )
    if cur.rowcount != 1:
        return False
    conn.execute("UPDATE deliveries SET surfaced = 1 WHERE id = ?", (delivery_id,))
    return True


def surface_hourly(
    conn: sqlite3.Connection, destination: str, code: str, delivery_id: int, now: int
) -> bool:
    """Insert the hourly row of one of :data:`HOURLY_CODES`; ``False`` when this hour already
    has one."""
    if code not in HOURLY_CODES:
        raise ValueError("not an hourly code")
    return _insert(conn, destination, code, hour_period(now), delivery_id)


def surface_daily(
    conn: sqlite3.Connection, destination: str, code: str | None, delivery_id: int, now: int
) -> bool:
    """Insert the daily row for ``code`` (``other`` when it is none, an hourly word or not of the
    shape of a code); the 9th distinct code of a day becomes ``other``. ``False`` when nothing
    was inserted."""
    if code is None or code in HOURLY_CODES or _CODE.fullmatch(code) is None:
        code = OTHER
    period = day_period(now)
    taken = conn.execute(
        "SELECT code FROM surfaced WHERE destination = ? AND period = ?", (destination, period)
    ).fetchall()
    if code != OTHER and all(c != code for (c,) in taken):
        distinct = sum(1 for (c,) in taken if c != OTHER)
        if distinct >= DAILY_CODE_LIMIT:
            code = OTHER
    return _insert(conn, destination, code, period, delivery_id)


def surface_outcome(
    conn: sqlite3.Connection,
    destination: str,
    outcome: str,
    code: str | None,
    delivery_id: int,
    now: int,
) -> bool:
    """Surface one event given as the outcome the router recorded (and its code). Outcomes that
    are not surfaced are ignored (``False``)."""
    if outcome in ("failed", "unknown"):
        return surface_daily(conn, destination, code, delivery_id, now)
    if outcome == "deferred" and code in ("rate_capped", "env_missing"):
        return surface_hourly(conn, destination, code, delivery_id, now)
    if outcome == "dropped" and code == "overflow":
        return surface_hourly(conn, destination, "overflow", delivery_id, now)
    if outcome == "expired" and code == "expired":
        return surface_hourly(conn, destination, "expired", delivery_id, now)
    return False


def surface_evicted(
    conn: sqlite3.Connection, destination: str, delivery_ids: Sequence[int], now: int
) -> bool:
    """The cap dropped rows of ``destination``: one ``overflow`` row for the hour, for the first
    of them that is not a failure alert (the first of all when each one is)."""
    if not delivery_ids:
        return False
    chosen = delivery_ids[0]
    for delivery_id in delivery_ids:
        found = conn.execute(
            "SELECT source FROM deliveries WHERE id = ?", (delivery_id,)
        ).fetchone()
        if found is not None and found[0] != ALERT_SOURCE:
            chosen = delivery_id
            break
    return surface_hourly(conn, destination, "overflow", chosen, now)


def surface_evictions(conn: sqlite3.Connection, evicted: Sequence[Evicted], now: int) -> None:
    """:func:`surface_evicted` for each destination that lost rows (a release of claims, which
    has no destination of its own)."""
    by_destination: dict[str, list[int]] = {}
    for e in evicted:
        by_destination.setdefault(e.destination, []).append(e.delivery_id)
    for destination, ids in by_destination.items():
        surface_evicted(conn, destination, ids, now)
