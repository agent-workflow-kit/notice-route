"""``prune`` (docs/interface.md, prune): delete what is old, in short transactions, never a row
somebody still needs.

Choices where the interface is silent, all the narrower reading (delete less):

- One reading of the wall clock, ``now``, decides every age cut-off of a run. The queue is the
  exception: its lease must be built from the clock inside each write transaction (a claim made
  by a concurrent ``flush`` after ``now`` would otherwise read as lapsed), so each batch of queue
  rows reads the clock again. A clock that stepped back makes every cut-off earlier (less is
  deleted, nothing stalls); one that stepped forward ends old rows early, which the interface
  accepts (design.md C-56).
- ``history``: ``deliveries.ts < now - retention.history``, and only rows the cap may also delete
  (no queue row, no unprocessed ``surfaced`` row: :func:`notice_route.store.prune_deliveries`).
  ``event_ids`` and ``dedup`` use the predicates of :mod:`notice_route.dedup`.
- ``surfaced``: only a *processed* row whose period lies before the UTC day of ``now`` minus 2
  days. A pending alert is never deleted (it is also what pins its ``deliveries`` row).
- ``rate``: a counter whose hour ended more than 2 days before ``now``. Only ``prune`` deletes
  counters (``decide`` and ``flush`` never do), so a clock that steps forward and back cannot
  reset a cap; the current and the previous hour are never old enough.
- ``queue``: through :func:`notice_route.queue.expire_unclaimed` (the one lease predicate):
  every unclaimed row (a lapsed claim counts as unclaimed) created 24 hours ago or earlier ends
  ``expired`` whatever its destination or the destination's state (``env_missing`` rows too).
  A row with a live claim is never touched.
- The queue is expired last, so that a dry run, which cannot expire anything, counts exactly what
  the real run deletes: a delivery that this very run expires is a history row only for the next
  run, and its dedup records are deleted by the release, after the windows have been counted.
- With no config the retention defaults apply (30 days, 14 days) and nothing is logged.

Every batch is one ``BEGIN IMMEDIATE`` transaction of at most :data:`BATCH` rows; between two
batches the run waits :data:`PAUSE_S` so a ``send`` that waits for the write lock gets it.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from . import dedup
from . import log as logmod
from .decide import instant
from .gateway import Gateway, UnsafePathError
from .history import DESTINATION, LEVELS, SOURCE
from .queue import EXPIRY_S, Expired, Lease, count_expirable, expire_unclaimed
from .report import worst_case_s
from .send import Seams, SurfaceEvent
from .store import (
    Store,
    StoreError,
    count_prunable_deliveries,
    open_readonly,
    open_store,
    prune_deliveries,
)
from .validate import Config

__all__ = ["BATCH", "PAUSE_S", "PruneOptions", "PruneResult", "run_prune"]

BATCH = 1000  # rows per transaction
PAUSE_S = 0.01  # between two batches
BUSY_TRIES = 3  # a batch whose write lock times out is tried this often before the run fails
DEFAULT_HISTORY_S = 30 * 86400
DEFAULT_EVENT_IDS_S = 14 * 86400
SURFACED_KEEP_S = 2 * 86400
RATE_KEEP_S = 2 * 86400
# no config: the longest lease there is, so that the most claims read as live
_NO_CONFIG_WORST_CASE_S = float(1 << 20)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_Conn = sqlite3.Connection
_on_batch: Callable[[int], None] = lambda n: None  # noqa: E731 - a test hook: rows per batch


@dataclass(frozen=True)
class PruneOptions:
    actor: str
    dry_run: bool = False


@dataclass
class PruneResult:
    exit_code: int
    document: dict[str, Any] | None = None
    error: tuple[str, str] | None = None
    warnings: list[str] = field(default_factory=list[str])


def _day(now: int, back_s: int) -> str | None:
    """The UTC day ``YYYY-MM-DD`` that contains ``now - back_s``; ``None`` out of range."""
    try:
        stamp = _EPOCH + timedelta(seconds=now - back_s)
    except OverflowError:
        return None
    return f"{stamp.year:04d}-{stamp.month:02d}-{stamp.day:02d}"


_SURFACED_OLD = "processed = 1 AND substr(period, 1, 10) < ?"
_RATE_OLD = "hour + 3600 < ?"


def _count_surfaced(conn: _Conn, day: str | None) -> int:
    if day is None:
        return 0
    return int(
        conn.execute("SELECT COUNT(*) FROM surfaced WHERE " + _SURFACED_OLD, (day,)).fetchone()[0]
    )


def _prune_surfaced(conn: _Conn, day: str | None, batch: int) -> int:
    if day is None:
        return 0
    return conn.execute(
        "DELETE FROM surfaced WHERE id IN"
        " (SELECT id FROM surfaced WHERE " + _SURFACED_OLD + " ORDER BY id LIMIT ?)",
        (day, batch),
    ).rowcount


def _count_rate(conn: _Conn, cutoff: int) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM rate WHERE " + _RATE_OLD, (cutoff,)).fetchone()[0]
    )


def _prune_rate(conn: _Conn, cutoff: int, batch: int) -> int:
    return conn.execute(
        "DELETE FROM rate WHERE (destination, hour, class) IN"
        " (SELECT destination, hour, class FROM rate WHERE " + _RATE_OLD + " LIMIT ?)",
        (cutoff, batch),
    ).rowcount


def _lease(gateway: Gateway, config: Config | None) -> Lease:
    worst = _NO_CONFIG_WORST_CASE_S if config is None else worst_case_s(config)
    return Lease.at(gateway.wall(), worst)


def _retention(config: Config | None) -> tuple[int, int]:
    if config is None:
        return DEFAULT_HISTORY_S, DEFAULT_EVENT_IDS_S
    return math.ceil(config.retention_history), math.ceil(config.retention_event_ids)


def _zero() -> dict[str, int]:
    return {"history": 0, "event_ids": 0, "dedup": 0, "surfaced": 0, "rate": 0, "queue": 0}


def run_prune(
    gateway: Gateway,
    config: Config | None,
    opts: PruneOptions,
    seams: Seams = Seams(),  # noqa: B008 - frozen, shared on purpose
) -> PruneResult:
    """Everything after the flags and the config. ``StoreError`` and ``HomeError`` propagate (exit
    1 and 2 in the cli)."""
    started = gateway.monotonic()
    warnings: list[str] = []
    if opts.dry_run:
        deleted = _dry_run(gateway, config)
    else:
        deleted = _run(gateway, config, opts, seams, started, warnings)
    return PruneResult(0, {"dry_run": opts.dry_run, "deleted": deleted}, warnings=warnings)


def _dry_run(gateway: Gateway, config: Config | None) -> dict[str, int]:
    history_s, event_ids_s = _retention(config)
    now = instant(gateway.wall())
    out = _zero()
    store = open_readonly(gateway)
    if store is None:
        return out
    with store:
        conn = store.conn
        try:
            conn.execute("BEGIN")  # one snapshot for every count
            try:
                out["history"] = count_prunable_deliveries(conn, cutoff=now - history_s)
                out["event_ids"] = dedup.count_prunable_event_ids(
                    conn, now=now, retention=event_ids_s
                )
                out["dedup"] = dedup.count_prunable_windows(conn, now=now)
                out["surfaced"] = _count_surfaced(conn, _day(now, SURFACED_KEEP_S))
                out["rate"] = _count_rate(conn, now - RATE_KEEP_S)
                out["queue"] = count_expirable(conn, None, _lease(gateway, config))
            finally:
                conn.execute("ROLLBACK")
        except sqlite3.Error:
            raise StoreError("internal") from None
    return out


def _run(
    gateway: Gateway,
    config: Config | None,
    opts: PruneOptions,
    seams: Seams,
    started: float,
    warnings: list[str],
) -> dict[str, int]:
    out = _zero()
    try:
        files = gateway.prepare_state_db(create=False)
    except (UnsafePathError, ValueError):
        raise StoreError("unsafe") from None
    except OSError:
        raise StoreError("internal") from None
    if files is None or _is_empty(gateway):
        return out
    history_s, event_ids_s = _retention(config)
    now = instant(gateway.wall())
    surfaced_day = _day(now, SURFACED_KEEP_S)
    with open_store(gateway) as store:
        out["event_ids"] = _loop(
            gateway,
            store,
            lambda c: dedup.prune_event_ids(c, now=now, retention=event_ids_s, batch=BATCH),
        )
        out["dedup"] = _loop(gateway, store, lambda c: dedup.prune_windows(c, now=now, batch=BATCH))
        out["surfaced"] = _loop(gateway, store, lambda c: _prune_surfaced(c, surfaced_day, BATCH))
        out["rate"] = _loop(gateway, store, lambda c: _prune_rate(c, now - RATE_KEEP_S, BATCH))
        out["history"] = _loop(
            gateway, store, lambda c: prune_deliveries(c, cutoff=now - history_s, batch=BATCH)
        )
        out["queue"] = _expire(gateway, store, config, opts, seams, started, warnings)
    return out


def _is_empty(gateway: Gateway) -> bool:
    """A ``state.db`` that was created and never migrated is no database (as for the read-only
    commands): nothing is migrated or created for it. An older schema is not empty: ``prune``
    migrates it like every command that writes."""
    store = None
    for attempt in range(BUSY_TRIES):
        try:
            store = open_readonly(gateway)
        except StoreError as exc:
            if exc.reason == "older":
                return False
            if exc.reason != "busy" or attempt == BUSY_TRIES - 1:
                raise
            gateway.clock.sleep(PAUSE_S * 10)
            continue
        break
    if store is None:
        return True
    store.close()
    return False


def _loop(gateway: Gateway, store: Store, step: Callable[[_Conn], int]) -> int:
    """Run ``step`` (which deletes at most :data:`BATCH` rows) in one transaction after another
    until a batch comes back short."""
    total = 0
    while True:
        n = _batch(gateway, store, step)
        total += n
        if n < BATCH:
            return total
        gateway.clock.sleep(PAUSE_S)


def _batch(gateway: Gateway, store: Store, step: Callable[[_Conn], int]) -> int:
    for attempt in range(BUSY_TRIES):
        try:
            with store.write() as conn:
                n = step(conn)
        except StoreError as exc:
            if exc.reason != "busy" or attempt == BUSY_TRIES - 1:
                raise
            gateway.clock.sleep(PAUSE_S * 10)
            continue
        _on_batch(n)
        return n
    raise AssertionError("unreachable")  # pragma: no cover


def _expire(
    gateway: Gateway,
    store: Store,
    config: Config | None,
    opts: PruneOptions,
    seams: Seams,
    started: float,
    warnings: list[str],
) -> int:
    writer: logmod.LogWriter | None = None
    total = 0
    state = None
    try:
        if config is not None and config.log:
            state = gateway.open_state_dir()
        writer = logmod.LogWriter(gateway, state)
        while True:
            expired = _expire_batch(gateway, store, config, seams)
            for e in expired:
                _log_expired(gateway, writer, opts, e, started)
            total += len(expired)
            if len(expired) < BATCH:
                return total
            gateway.clock.sleep(PAUSE_S)
    finally:
        if state is not None:
            state.close()
        if writer is not None and writer.problems:
            warnings.append("the log could not be written")


def _log_expired(
    gateway: Gateway, writer: logmod.LogWriter, opts: PruneOptions, e: Expired, started: float
) -> None:
    """One ``delivery`` line for an expired row, or none: the columns are a file's text, so a
    line is built only from values inside the grammar of ``schemas/log.json`` (the schema
    requires all three), and a line that cannot be built is a problem, never an exception (the
    rows are already expired and committed)."""
    if not (
        SOURCE.fullmatch(e.source)
        and DESTINATION.fullmatch(e.destination)
        and e.level in LEVELS
        and type(e.attempts) is int
        and e.attempts >= 0
    ):
        return  # the row is expired; its line would break the grammar of the log
    try:
        rec = logmod.record(
            "delivery",
            wall=gateway.wall(),
            actor=opts.actor,
            decision_id=None,
            source=e.source,
            destination=e.destination,
            level=e.level,
            outcome="expired",
            code="expired",
            attempts=e.attempts,
            duration_ms=max(0, math.floor((gateway.monotonic() - started) * 1000)),
        )
    except (ValueError, TypeError, OverflowError):
        writer.problems += 1
        return
    writer.write(rec)


def _text(value: object) -> str:
    """A text column as ``str``: bytes (a BLOB, or text that is not UTF-8) are decoded with
    replacement; anything else (a number) is empty, so it never matches a grammar."""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return ""


def _clean(e: Expired) -> Expired:
    return replace(
        e,
        destination=_text(e.destination),
        kind=_text(e.kind),
        level=_text(e.level),
        source=_text(e.source),
    )


def _expire_batch(
    gateway: Gateway, store: Store, config: Config | None, seams: Seams
) -> tuple[Expired, ...]:
    for attempt in range(BUSY_TRIES):
        try:
            with store.write() as conn:
                # the lease is built after the write lock is held (queue.Lease)
                lease = _lease(gateway, config)
                # a column may hold bytes that are not UTF-8: decode with replacement, so that
                # the row still expires
                conn.text_factory = lambda raw: raw.decode("utf-8", "replace")
                try:
                    found = expire_unclaimed(conn, lease, limit=BATCH, max_age=EXPIRY_S)
                finally:
                    conn.text_factory = str
                expired = tuple(_clean(e) for e in found)
                for e in expired:
                    # surfaced only for a destination that is configured and enabled (Hooks).
                    # TODO(#27): the default seam does nothing until surfacing exists.
                    dest = None if config is None else config.destinations.get(e.destination)
                    if dest is not None and dest.enabled:
                        seams.surface(
                            conn,
                            SurfaceEvent(
                                e.delivery_id,
                                dest.name,
                                dest.kind,
                                e.source,
                                e.level,
                                "expired",
                                "expired",
                                e.attempts,
                            ),
                        )
        except StoreError as exc:
            if exc.reason != "busy" or attempt == BUSY_TRIES - 1:
                raise
            gateway.clock.sleep(PAUSE_S * 10)
            continue
        _on_batch(len(expired))
        return expired
    raise AssertionError("unreachable")  # pragma: no cover
