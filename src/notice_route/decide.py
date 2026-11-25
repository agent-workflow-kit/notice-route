"""The per-destination decision order and the hourly counters (#18).

``send`` decides every selected destination in one ``BEGIN IMMEDIATE`` transaction; ``test`` and
``send --dry-run`` run the same classification against a read-only database and write nothing.
The first of these that applies decides (docs/interface.md: ``send`` step 7):

1. disabled; 2. below the destination's ``floor``; 3. ``info`` in the destination's quiet window;
4. redelivery; 5. repeat; 6. ``warn`` or ``action`` in quiet (a ``held`` row); 7. a variable named
in ``env`` unset (a ``single`` row due now, ``env_missing``); 8. the class counter at
``max_per_hour`` (a ``held`` row due at the next UTC hour); 9. send now (a ``single`` row claimed
by this process, the counter incremented).

Write order per destination that reaches (6) to (9) (docs/design.md C-47): ``check_repeat`` (step
5, so a suppressed repeat writes only its ``count``), ``insert_delivery``, ``bind_window`` (must be
``True``; the row was inserted a moment ago and nothing ended it), ``record_event_id``, ``enqueue``
(which may evict, even the new row: that destination then ends ``dropped``, ``overflow``). A
destination dropped or disabled gets a ``deliveries`` row and no dedup record; a suppressed one
gets no row.

Counters (docs/interface.md: Rate limit): ``rate`` holds one count per destination, UTC clock hour
and class (``error``, or ``other`` for every other level). :func:`claim_call` checks the cap and
increments in one statement, so concurrent senders cannot pass the cap together even without the
write lock; :func:`count_call` increments without a cap (a digest is never held by the cap but
still counts); :func:`give_back` returns a count when a call was never made (``lock_busy``).
A destination with ``max_per_hour: 0`` has no cap and is still counted (a later cap then sees an
honest count). Rows older than a day, or more than a day ahead (a clock that stepped), are
deleted whenever a counter row is created, so the table stays small.

Choices where the interface is silent (the narrower reading):

- ``deliveries.labels`` is a JSON array of ``key=value`` strings, sorted, at most 16, cut so the
  JSON text stays within 1024 characters (schemas/cli/history.json).
- ``object_id`` outside int64 is stored as null (SQLite cannot hold it); an ``event_id`` is
  compared and recorded as its JSON text.
- A send-now row is a ``deliveries`` row in outcome ``sending`` with ``attempts`` 0 and no
  ``deliver_after``; the call (#19) and its recorder (#20) own what happens next.
- An unset variable is a missing or empty one (the predicate of ``validate``'s ``env_unset``).
- ``test`` cannot predict an ``overflow`` eviction (it never queues), so it shows ``defer``.

Nothing here reads a clock or the environment: ``send`` passes the gateway's, so tests are
deterministic. Every value reaches SQL as a bound parameter and every statement is a constant.

Seams (owned by later issues):
TODO(#19) the notifier call for each ``send`` result; TODO(#20) its recorded outcome, the
``attempts`` count and ``give_back`` for a call never made; TODO(#21) ``lock_busy``;
TODO(#22) the pipeline that renders, calls :func:`decide_and_record`, prints and logs;
TODO(#23) ``flush`` claims a due row with :func:`claim_call` (single rows) or :func:`count_call`
(a digest); TODO(#26) ``prune`` may drop old ``rate`` rows too; TODO(#27) surfacing ``env_missing``
and routing the alert (alerts skip the redelivery check, which :func:`dedup.is_redelivered`
already does for source ``notice-route``).
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from . import dedup
from .gateway import Gateway
from .queue import Lease, enqueue, insert_delivery, quiet_deliver_after
from .report import worst_case_s
from .store import Store
from .validate import LEVELS, Config, Destination
from .window import in_window

__all__ = [
    "CLASSES",
    "HOUR",
    "MAX_LABELS",
    "DestinationDecision",
    "Notice",
    "claim_call",
    "count_call",
    "decide_and_record",
    "env_unset",
    "give_back",
    "hour_start",
    "instant",
    "preview",
    "rate_class",
    "rate_count",
]

HOUR = 3600
CLASSES = ("error", "other")
MAX_LABELS = 16
_LABELS_CHARS = 1024
_TITLE_CHARS = 100
_INT64_MAX = (1 << 63) - 1
_MAX_CAP = 1 << 31
_MAX_INSTANT = 1 << 40

_Conn = sqlite3.Connection
Decision = Literal["send", "defer", "drop", "suppress", "disabled"]
Outcome = Literal["sending", "deferred", "dropped", "suppressed", "disabled"]
Kind = Literal["held", "single"]
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_RANK = {level: rank for rank, level in enumerate(LEVELS)}


@dataclass(frozen=True)
class Notice:
    """The rendered notification every destination is decided for (built once per ``send``).

    ``link`` is empty when there is none. ``dedupe`` is :func:`notice_route.dryrun.dedupe_digest`.
    ``dedup_window`` is the opening rule's window in whole seconds (``math.ceil`` of the
    configured duration). ``pass_through`` rows keep no title in ``deliveries``."""

    source: str
    hook: str | None
    level: str
    rule: str | None
    title: str
    message: str
    link: str
    dedupe: str
    dedup_window: int
    event_id: int | str | None = None
    object_id: int | str | None = None
    labels: tuple[tuple[str, str], ...] = ()
    pass_through: bool = False


@dataclass(frozen=True)
class DestinationDecision:
    """What one destination gets. ``decision`` is the ``test`` vocabulary and ``outcome`` the
    ``deliveries`` one. ``deliver_after`` is set for ``defer`` only. The ids and the claim are
    set only for a recorded decision: ``delivery_id`` for everything but ``suppress``,
    ``queue_id`` for a queued row, and for ``send`` the ``claim_token`` and ``claim_expires`` of
    its row."""

    name: str
    decision: Decision
    outcome: Outcome
    reason: str | None = None
    deliver_after: int | None = None
    delivery_id: int | None = None
    queue_id: int | None = None
    claim_token: str | None = None
    claim_expires: int | None = None


@dataclass(frozen=True)
class _Plan:
    decision: Decision
    outcome: Outcome
    reason: str | None = None
    kind: Kind | None = None
    deliver_after: int | None = None
    claim: bool = False
    surface: bool = False


# --- instants and counters -----------------------------------------------------------------


def instant(wall: float) -> int:
    """The decision time: ``wall`` floored to whole epoch seconds. A bool, a non-finite or an
    out-of-range reading is a ``ValueError`` (it never reaches a window or a counter)."""
    return Lease.at(wall, 1.0).now


def hour_start(now: int) -> int:
    """The UTC clock hour that contains ``now``, as the epoch second it starts."""
    return now - now % HOUR


def rate_class(level: str) -> str:
    """``error`` for the ``error`` level, ``other`` for every other (a digest passes the highest
    level of its rows)."""
    if level not in LEVELS:
        raise ValueError("level must be one of info, warn, action, error")
    return "error" if level == "error" else "other"


def _now(value: object) -> int:
    if type(value) is not int or not -_MAX_INSTANT <= value <= _MAX_INSTANT:
        raise ValueError("now must be an integer within +-2**40")
    return value


def _name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("destination must be a string")
    return value


def _cap(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_CAP:
        raise ValueError("cap must be an integer from 0 to 2**31")
    return value


# One statement: create the hour's counter at 1 or add 1, unless the cap (0: none) is reached.
# A conflict whose WHERE fails changes nothing and returns no row.
_CLAIM = (
    "INSERT INTO rate (destination, hour, class, count) VALUES (?, ?, ?, 1)"
    " ON CONFLICT (destination, hour, class) DO UPDATE SET count = rate.count + 1"
    " WHERE (? = 0 OR rate.count < ?) AND rate.count < ?"
    " RETURNING count"
)
_COUNT = (
    "INSERT INTO rate (destination, hour, class, count) VALUES (?, ?, ?, 1)"
    " ON CONFLICT (destination, hour, class) DO UPDATE SET"
    " count = CASE WHEN rate.count < ? THEN rate.count + 1 ELSE rate.count END"
)
_PRUNE = "DELETE FROM rate WHERE hour < ? OR hour > ?"


def _prune(conn: _Conn, hour: int) -> None:
    conn.execute(_PRUNE, (hour - 24 * HOUR, hour + 24 * HOUR))


def rate_count(conn: _Conn, destination: str, level: str, now: int) -> int:
    """The count of ``destination``'s class of ``level`` in the hour of ``now`` (0 if none)."""
    row = conn.execute(
        "SELECT count FROM rate WHERE destination = ? AND hour = ? AND class = ?",
        (_name(destination), hour_start(_now(now)), rate_class(level)),
    ).fetchone()
    return 0 if row is None else int(row[0])


def claim_call(conn: _Conn, *, destination: str, level: str, now: int, cap: int) -> bool:
    """Count one call in the class of ``level`` for this hour unless the counter has reached
    ``cap`` (``max_per_hour``; 0 is no cap). One statement: parallel senders cannot pass the cap
    together. ``False`` changes nothing. Call inside a ``Store.write`` block."""
    hour = hour_start(_now(now))
    row = conn.execute(
        _CLAIM,
        (_name(destination), hour, rate_class(level), _cap(cap), cap, _INT64_MAX),
    ).fetchone()
    if row is None:
        return False
    if row[0] == 1:  # a counter was created (or came back from 0): keep the table small
        _prune(conn, hour)
    return True


def count_call(conn: _Conn, *, destination: str, level: str, now: int) -> None:
    """Count one call without a cap check: a digest is never held by the cap but still counts."""
    hour = hour_start(_now(now))
    conn.execute(_COUNT, (_name(destination), hour, rate_class(level), _INT64_MAX))
    _prune(conn, hour)


def give_back(conn: _Conn, *, destination: str, level: str, hour: int) -> bool:
    """Return one count to ``hour``'s counter (a call that was never made); never below 0, and
    ``False`` when there is nothing to give back (the row was pruned or is 0)."""
    cur = conn.execute(
        "UPDATE rate SET count = count - 1 WHERE destination = ? AND hour = ? AND class = ?"
        " AND count > 0",
        (_name(destination), _now(hour), rate_class(level)),
    )
    return cur.rowcount == 1


# --- the decision order --------------------------------------------------------------------


def env_unset(names: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    """The variables of ``names`` that are missing or empty in ``environ``."""
    return [n for n in names if environ.get(n, "") == ""]


def _surface_period(hour: int) -> str:
    return (_EPOCH + timedelta(seconds=hour)).strftime("%Y-%m-%dT%H")


def _classify(
    state: Callable[[], _Conn | None],
    *,
    write: bool,
    config: Config,
    dest: Destination,
    notice: Notice,
    now: int,
    environ: Mapping[str, str],
    retention: int,
) -> _Plan:
    """Steps 1 to 9. ``state`` yields the connection (``None``: no database, so no records);
    with ``write`` the repeat check and the counter write, otherwise they only read."""
    level = notice.level
    if level not in LEVELS:
        raise ValueError("level must be one of info, warn, action, error")
    if not dest.enabled:
        return _Plan("disabled", "disabled")
    if _RANK[level] < _RANK[dest.floor]:
        return _Plan("drop", "dropped", "below_floor")
    window = config.quiet_hours[dest.quiet] if dest.quiet is not None else None
    quiet = window is not None and in_window(window, now)
    if quiet and level == "info":
        return _Plan("drop", "dropped", "quiet")
    conn = state()
    if conn is not None:
        if dedup.is_redelivered(
            conn,
            destination=dest.name,
            source=notice.source,
            hook=notice.hook,
            event_id=notice.event_id,
            digest=notice.dedupe,
            now=now,
            retention=retention,
        ):
            return _Plan("suppress", "suppressed", "redelivered")
        if write:
            opened = dedup.check_repeat(
                conn,
                destination=dest.name,
                digest=notice.dedupe,
                now=now,
                window_length=notice.dedup_window,
            ).opened
            repeated = not opened
        else:
            repeated = (
                dedup.active_window(conn, destination=dest.name, digest=notice.dedupe, now=now)
                is not None
            )
        if repeated:
            return _Plan("suppress", "suppressed", "repeat")
    if quiet and level in ("warn", "action"):
        assert window is not None
        return _Plan("defer", "deferred", "quiet", "held", quiet_deliver_after(window, now))
    if env_unset(dest.env, environ):
        return _Plan("defer", "deferred", "env_missing", "single", now)
    next_hour = hour_start(now) + HOUR
    if write:
        assert conn is not None
        if claim_call(conn, destination=dest.name, level=level, now=now, cap=dest.max_per_hour):
            return _Plan("send", "sending", kind="single", deliver_after=now, claim=True)
    else:
        counted = 0 if conn is None else rate_count(conn, dest.name, level, now)
        if dest.max_per_hour == 0 or counted < dest.max_per_hour:
            return _Plan("send", "sending")
    return _Plan("defer", "deferred", "rate_capped", "held", next_hour, surface=True)


def preview(
    open_state: Callable[[], Store | None],
    config: Config,
    notice: Notice,
    destinations: Sequence[str],
    *,
    now: int,
    environ: Mapping[str, str],
) -> list[DestinationDecision]:
    """The decision of every destination, writing nothing: ``test`` and ``send --dry-run``.

    ``open_state`` opens the database read-only (``None``: there is none, read as empty) and is
    called at most once, the first time a destination needs state (steps 4 to 8), so a config
    that decides before then never touches ``state.db``. The caller closes the store."""
    _now(now)
    retention = math.ceil(config.retention_event_ids)
    opened: list[Store | None] = []

    def state() -> _Conn | None:
        if not opened:
            opened.append(open_state())
        store = opened[0]
        return None if store is None else store.conn

    out: list[DestinationDecision] = []
    for name in dict.fromkeys(destinations):
        plan = _classify(
            state,
            write=False,
            config=config,
            dest=config.destinations[name],
            notice=notice,
            now=now,
            environ=environ,
            retention=retention,
        )
        out.append(
            DestinationDecision(
                name,
                plan.decision,
                plan.outcome,
                plan.reason,
                plan.deliver_after if plan.decision == "defer" else None,
            )
        )
    return out


def labels_text(labels: Sequence[tuple[str, str]]) -> str | None:
    """The stored form of labels: a JSON array of sorted ``key=value`` strings, at most
    :data:`MAX_LABELS`, cut to stay within 1024 characters; ``None`` when there are none."""
    texts = sorted({f"{k}={v}" for k, v in labels})[:MAX_LABELS]
    while texts and len(json.dumps(texts)) > _LABELS_CHARS:
        texts.pop()
    return json.dumps(texts) if texts else None


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _object_id(value: int | str | None) -> int | str | None:
    if type(value) is int and not -(1 << 63) <= value <= _INT64_MAX:
        return None
    return value


def decide_and_record(
    store: Store,
    gateway: Gateway,
    config: Config,
    notice: Notice,
    destinations: Sequence[str],
) -> list[DestinationDecision]:
    """``send`` step 7: decide every destination and write its rows in one transaction.

    The clock is read inside the write block, after the lock is held, and the lease is built
    there (docs/design.md C-46). Any exception rolls the whole transaction back: nothing is
    half decided. A send-now result carries a fresh claim token; the notifier call (#19) must
    use it to record its outcome."""
    retention = math.ceil(config.retention_event_ids)
    with store.write() as conn:
        lease = Lease.at(gateway.wall(), worst_case_s(config))
        now = lease.now
        environ = dict(gateway.environ())
        out: list[DestinationDecision] = []
        for name in dict.fromkeys(destinations):
            dest = config.destinations[name]
            plan = _classify(
                lambda: conn,
                write=True,
                config=config,
                dest=dest,
                notice=notice,
                now=now,
                environ=environ,
                retention=retention,
            )
            if plan.decision == "suppress":
                out.append(DestinationDecision(name, "suppress", "suppressed", plan.reason))
                continue
            out.append(_record(conn, gateway, plan, dest, notice, lease, retention))
        return out


def _record(
    conn: _Conn,
    gateway: Gateway,
    plan: _Plan,
    dest: Destination,
    notice: Notice,
    lease: Lease,
    retention: int,
) -> DestinationDecision:
    now = lease.now
    event_text = dedup.event_id_text(notice.event_id)
    delivery_id = insert_delivery(
        conn,
        ts=now,
        source=notice.source,
        level=notice.level,
        outcome=plan.outcome,
        destination=dest.name,
        hook=notice.hook,
        object_id=_object_id(notice.object_id),
        rule=notice.rule,
        reason=plan.reason,
        event_id=event_text,
        dedupe=notice.dedupe,
        labels=labels_text(notice.labels),
        deliver_after=plan.deliver_after if plan.decision == "defer" else None,
        title=None if notice.pass_through else notice.title[:_TITLE_CHARS],
        title_sha256=_sha16(notice.title),
        message_sha256=_sha16(notice.message),
    )
    assert plan.kind is not None or plan.decision in ("disabled", "drop")
    if plan.kind is None:  # disabled or dropped: a row, and no dedup record
        return DestinationDecision(
            dest.name, plan.decision, plan.outcome, plan.reason, None, delivery_id
        )
    if not dedup.bind_window(
        conn, destination=dest.name, digest=notice.dedupe, delivery_id=delivery_id
    ):
        raise RuntimeError("the repeat window could not be bound to its new delivery")
    dedup.record_event_id(
        conn,
        destination=dest.name,
        source=notice.source,
        hook=notice.hook,
        event_id=notice.event_id,
        digest=notice.dedupe,
        now=now,
        retention=retention,
        delivery_id=delivery_id,
    )
    if plan.surface and _surface(conn, dest.name, "rate_capped", hour_start(now), delivery_id):
        conn.execute("UPDATE deliveries SET surfaced = 1 WHERE id = ?", (delivery_id,))
    token = gateway.new_token() if plan.claim else None
    queued = enqueue(
        conn,
        delivery_id=delivery_id,
        kind=plan.kind,
        message=notice.message,
        level=notice.level,
        source=notice.source,
        deliver_after=now if plan.deliver_after is None else plan.deliver_after,
        created_at=now,
        lease=lease,
        title=notice.title or None,
        link=notice.link or None,
        rule=notice.rule,
        dedupe=notice.dedupe,
        claim_token=token,
    )
    if queued.row_evicted:  # the cap dropped the row just queued
        return DestinationDecision(
            dest.name, "drop", "dropped", "overflow", None, delivery_id, queued.row_id
        )
    return DestinationDecision(
        dest.name,
        plan.decision,
        plan.outcome,
        plan.reason,
        plan.deliver_after if plan.decision == "defer" else None,
        delivery_id,
        queued.row_id,
        token,
        lease.expires if token is not None else None,
    )


def _surface(conn: _Conn, destination: str, code: str, hour: int, delivery_id: int) -> bool:
    """Insert the once-per-hour ``surfaced`` row; ``False`` when this hour already has one."""
    cur = conn.execute(
        "INSERT INTO surfaced (destination, code, period, delivery_id) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (destination, code, period) DO NOTHING",
        (destination, code, _surface_period(hour), delivery_id),
    )
    return cur.rowcount == 1
