"""The fallback chain (docs/interface.md, Fallback chain; docs/design.md C-63).

When a delivery to a destination that has a ``fallback`` ends ``failed``, the transaction that
records the end hands the same rendered notification to the fallback destination: one new
``deliveries`` row (``fallback_of`` is the failed row's id) and, when the destination will be
called, an unclaimed queue row that ``flush`` delivers. The seam (:func:`make_seam`) is installed
on :class:`notice_route.send.Seams` only for a config with at least one ``fallback`` key, so a
config without one runs exactly the code it ran before.

The seam never calls a notifier, never claims a row and never raises into the recording of the
failure: a fault inside it is rolled back to a savepoint and reported as ``decision`` ``fault``
(the failure is then recorded and surfaced without a fallback, and the caller warns).

Run-time loop guard (the config is validated acyclic, but it can change between two hops): the
chain of ``fallback_of`` ids of the failed row is read (at most :data:`MAX_FALLBACK_HOPS` steps).
No further delivery is made when the chain already has that many hops, when the fallback
destination is on it, or when a row of it is missing (the narrower reading: unknown history means
no more delivery).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Callable
from typing import Any

from .decide import Kind, Notice, decide_fallback
from .gateway import Gateway
from .outcome import FallbackRoute
from .queue import Lease, QueueRow
from .validate import LEVELS, MAX_FALLBACK_HOPS, Config, Destination

__all__ = ["FallbackSeam", "chain_allows", "make_seam", "route_failed"]

FallbackSeam = Callable[[sqlite3.Connection, Destination, QueueRow, Lease], FallbackRoute | None]

_DIGEST = re.compile(r"[0-9a-f]{64}")
_LABEL_COUNT = 16


def chain_allows(conn: sqlite3.Connection, delivery_id: int, target: str) -> bool:
    """Whether the chain of ``delivery_id`` (the failed row, then each ``fallback_of``) may take
    one more hop to ``target``."""
    seen: list[str] = []
    current: int | None = delivery_id
    for _ in range(MAX_FALLBACK_HOPS + 1):
        if current is None:
            break
        found = conn.execute(
            "SELECT destination, fallback_of FROM deliveries WHERE id = ?", (current,)
        ).fetchone()
        if found is None or not isinstance(found[0], str):
            return False  # a row of the chain is gone: no further delivery
        seen.append(found[0])
        parent = found[1]
        if parent is not None and (type(parent) is not int or parent < 1):
            return False
        current = parent
    else:
        return False  # more parents than a chain may have
    return len(seen) - 1 < MAX_FALLBACK_HOPS and target not in seen


def _labels(text: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(text, str):
        return ()
    try:
        decoded: Any = json.loads(text)
    except ValueError:
        return ()
    if not isinstance(decoded, list) or len(decoded) > _LABEL_COUNT:  # pyright: ignore[reportUnknownArgumentType]
        return ()
    out: list[tuple[str, str]] = []
    for item in decoded:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            out.append((key, value))
    return tuple(out)


def _event_id(text: object) -> int | str | None:
    if not isinstance(text, str):
        return None
    try:
        decoded: Any = json.loads(text)
    except ValueError:
        return None
    if type(decoded) is int or isinstance(decoded, str):
        return decoded  # pyright: ignore[reportUnknownVariableType]
    return None


def _notice(conn: sqlite3.Connection, config: Config, row: QueueRow) -> Notice | None:
    """The notification of a failed row, rebuilt from the stored queue row and ``deliveries`` row
    (nothing is rendered again), or ``None`` when what is stored cannot be used."""
    found = conn.execute(
        "SELECT hook, object_id, event_id, labels, title, dedupe FROM deliveries WHERE id = ?",
        (row.delivery_id,),
    ).fetchone()
    if found is None or row.level not in LEVELS:
        return None
    hook, object_id, event_id, labels, title, stored_digest = found
    digest = row.dedupe if row.dedupe is not None else stored_digest
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        return None
    rule = next((r for r in config.rules if r.name == row.rule), None)
    window = config.default_dedup_window if rule is None else rule.dedup_window
    return Notice(
        source=row.source,
        hook=hook if isinstance(hook, str) else None,
        level=row.level,
        rule=row.rule,
        title=row.title or "",
        message=row.message,
        link=row.link or "",
        dedupe=digest,
        dedup_window=math.ceil(window),
        event_id=_event_id(event_id),
        object_id=object_id if type(object_id) in (int, str) else None,
        labels=_labels(labels),
        pass_through=title is None,
    )


def route_failed(
    conn: sqlite3.Connection,
    gateway: Gateway,
    config: Config,
    dest: Destination,
    row: QueueRow,
    lease: Lease,
) -> FallbackRoute | None:
    """The fallback of ``row``, which just ended ``failed`` at ``dest``: ``None`` when there is
    nothing to do (no fallback, the chain may not go on, nothing usable stored)."""
    target = dest.fallback
    if target is None or target not in config.destinations:
        return None
    if not chain_allows(conn, row.delivery_id, target):
        return None
    notice = _notice(conn, config, row)
    if notice is None:
        return None
    kind: Kind = "held" if row.kind == "held" else "single"
    conn.execute("SAVEPOINT fallback")
    try:
        d = decide_fallback(
            conn, gateway, config, notice, target, lease, kind=kind, fallback_of=row.delivery_id
        )
    except Exception:  # isolate: the failure itself must still be recorded (the text is dropped)
        conn.execute("ROLLBACK TO fallback")
        conn.execute("RELEASE fallback")
        return FallbackRoute(target, "fault", None, None, None)
    conn.execute("RELEASE fallback")
    return FallbackRoute(target, d.decision, d.outcome, d.reason, d.delivery_id)


def make_seam(gateway: Gateway, config: Config) -> FallbackSeam | None:
    """The seam for ``config``, or ``None`` when no destination has a ``fallback``."""
    if not any(d.fallback is not None for d in config.destinations.values()):
        return None

    def seam(
        conn: sqlite3.Connection, dest: Destination, row: QueueRow, lease: Lease
    ) -> FallbackRoute | None:
        return route_failed(conn, gateway, config, dest, row, lease)

    return seam
