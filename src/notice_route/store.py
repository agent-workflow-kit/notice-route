"""The state database: a hardened SQLite connection, migrations and the bounded tables.

This module never touches the filesystem itself: the checks of the state directory and of
``state.db`` with its side files happen in ``Gateway.prepare_state_db`` (descriptor based,
``O_NOFOLLOW``, owner and mode), and SQLite is handed the path only afterwards.

Rules every caller gets for free:

- autocommit connection; the only way to write is :meth:`Store.write`, which runs
  ``BEGIN IMMEDIATE`` (a deferred ``BEGIN`` that later wants the write lock can fail with
  ``SQLITE_BUSY`` and cannot wait; ``query_only`` makes any other write fail loudly);
- every value reaches SQL as a bound parameter; the SQL text here is constant;
- any failure to open or use the file is a :class:`StoreError` whose message is a fixed sentence
  for its ``reason``: nothing of the file, the path or SQLite's own text is carried (exit 1
  ``internal``, nothing sent).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Literal
from urllib.parse import quote

from .gateway import Gateway, StateFiles, UnsafePathError
from .schema import APPLICATION_ID, MIGRATIONS, TABLES, Migration

__all__ = [
    "DEFAULT_BUSY_S",
    "DELIVERIES_CAP",
    "EVENT_IDS_CAP",
    "EVICT_BATCH",
    "MESSAGES",
    "MIN_SQLITE",
    "SEND_BUSY_S",
    "Store",
    "StoreError",
    "open_readonly",
    "open_store",
    "trim_deliveries",
    "TRIM_EVENT_IDS_SQL",
    "trim_event_ids",
]

SEND_BUSY_S = 0.5  # each of ``send``'s two transactions
DEFAULT_BUSY_S = 5.0  # every other command
DELIVERIES_CAP = 100_000
EVENT_IDS_CAP = 100_000
EVICT_BATCH = 16

Reason = Literal[
    "unsafe",
    "corrupt",
    "foreign",
    "newer",
    "older",
    "readonly",
    "busy",
    "migration",
    "internal",
    "sqlite_old",
]

MESSAGES: dict[str, str] = {
    "unsafe": "state database path is unsafe",
    "corrupt": "state database is corrupt",
    "foreign": "state database is not a notice-route database",
    "newer": "state database has a newer schema than this version supports",
    "older": "state database needs a migration and cannot be migrated read-only",
    "readonly": "state database is read-only",
    "busy": "state database is busy",
    "migration": "state database migration failed",
    "internal": "state database cannot be used",
    "sqlite_old": "SQLite 3.35 or newer is required",
}
MIN_SQLITE = (3, 35, 0)  # RETURNING (the dedup upserts)

# SQLite primary result codes
_BUSY, _LOCKED, _READONLY, _PERM, _CANTOPEN = 5, 6, 8, 3, 14
_CORRUPT, _NOTADB = 11, 26


class StoreError(Exception):
    """``state.db`` cannot be used. ``str`` is a fixed sentence per ``reason``."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(MESSAGES[reason])
        self.reason: Reason = reason


def _classify(exc: sqlite3.Error) -> Reason | None:
    """The reason for an SQLite failure that means "the database cannot be used", or ``None``
    for a failure that is the caller's (a constraint, a bad statement)."""
    code = getattr(exc, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return None if isinstance(exc, sqlite3.IntegrityError) else "internal"
    primary = code & 0xFF
    if primary in (_BUSY, _LOCKED):
        return "busy"
    if primary in (_READONLY, _PERM, _CANTOPEN):
        return "readonly"
    if primary in (_CORRUPT, _NOTADB):
        return "corrupt"
    if isinstance(exc, sqlite3.IntegrityError):
        return None
    if primary in (10, 13):  # IOERR, FULL
        return "internal"
    return None


def _fail(exc: sqlite3.Error) -> StoreError:
    return StoreError(_classify(exc) or "internal")


class Store:
    """An open ``state.db``. ``conn`` reads freely; writes go through :meth:`write`."""

    def __init__(self, conn: sqlite3.Connection, *, readonly: bool) -> None:
        self.conn = conn
        self.readonly = readonly
        self._writing = False

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction: ``BEGIN IMMEDIATE``, then commit, or roll back on any
        exception. Raises :class:`StoreError` (``busy`` after the busy timeout, ``readonly``,
        ``corrupt``) when the database cannot be written; a constraint failure of the caller's
        statement is the original ``sqlite3.IntegrityError``."""
        if self.readonly:
            raise RuntimeError("the store was opened read-only")
        if self._writing:
            raise RuntimeError("write transactions do not nest")
        try:
            self.conn.execute("PRAGMA query_only=OFF")
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            self._read_only_again()
            raise _fail(exc) from None
        self._writing = True
        try:
            try:
                yield self.conn
            except BaseException as exc:
                self._rollback()
                if isinstance(exc, sqlite3.Error) and (reason := _classify(exc)):
                    raise StoreError(reason) from None
                raise
            try:
                self.conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback()
                raise _fail(exc) from None
        finally:
            self._writing = False
            self._read_only_again()

    def _read_only_again(self) -> None:
        try:
            self.conn.execute("PRAGMA query_only=ON")
        except sqlite3.Error:
            pass

    def _rollback(self) -> None:
        if self.conn.in_transaction:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass

    def read(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        """Run a read-only statement with bound ``params`` and return all rows. A failure that
        means the database cannot be used (corrupt page, busy, ...) is a :class:`StoreError`,
        as for :meth:`write`; a bad statement is ``internal``."""
        try:
            return self.conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise _fail(exc) from None

    def check_integrity(self) -> None:
        """Run SQLite's ``quick_check``; raise ``StoreError("corrupt")`` unless it says ``ok``."""
        try:
            rows = self.conn.execute("PRAGMA quick_check(1)").fetchall()
        except sqlite3.Error as exc:
            raise _fail(exc) from None
        if rows != [("ok",)]:
            raise StoreError("corrupt")


def open_store(
    gateway: Gateway,
    *,
    busy_timeout: float = DEFAULT_BUSY_S,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> Store:
    """Open ``state.db`` for reading and writing, creating and migrating it.

    Creates the state directory (0700) and the file (0600) when absent, switches the file to WAL
    and migrates it to the newest version in one ``BEGIN IMMEDIATE`` transaction.
    ``HomeError`` propagates (the caller reports it as it reports a bad home for the config).
    Everything else is a :class:`StoreError`.
    """
    store = _open(gateway, busy_timeout, False, migrations)
    assert store is not None
    return store


def open_readonly(
    gateway: Gateway,
    *,
    busy_timeout: float = DEFAULT_BUSY_S,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> Store | None:
    """Open ``state.db`` read-only: nothing is created, written or migrated.

    Returns ``None`` when there is no database; refuses (``older``, ``newer``, ``foreign``) one
    that is not at this build's version. A database without a ``-wal`` file is opened
    ``immutable`` (SQLite would otherwise create ``-wal`` and ``-shm``); a writer that starts in
    between may be missed, which is fine for advisory reads (``test``, ``send --dry-run``,
    ``validate``). ``test`` and ``send --dry-run`` read through this (``decide.preview``).

    When a ``-wal`` file exists SQLite may create or touch the shared-memory index ``state.db-shm``
    (reader marks); ``state.db`` and ``state.db-wal`` are never changed.
    """
    return _open(gateway, busy_timeout, True, migrations)


def _open(
    gateway: Gateway,
    busy_timeout: float,
    readonly: bool,
    migrations: tuple[Migration, ...],
) -> Store | None:
    if not readonly and sqlite3.sqlite_version_info < MIN_SQLITE:  # RETURNING is a write need
        raise StoreError("sqlite_old")
    if not (
        isinstance(busy_timeout, (int, float))
        and not isinstance(busy_timeout, bool)
        and 0 < busy_timeout <= 60
    ):
        raise ValueError("busy_timeout must be in (0, 60] seconds")
    try:
        files = gateway.prepare_state_db(create=not readonly)
    # todo(#22): tests/test_queue.py::test_concurrent_enqueuers_at_the_cap_lose_no_row_and_keep_
    # exactly_100 saw this branch once (Python 3.13, 8 processes opening a new state directory
    # together). It did not reproduce in 70+ later runs (40 rounds of 8 concurrent openers and 30
    # reruns of the test). The cause is unknown; the reason is dropped here (``from None``), so the
    # next sighting should log the UnsafePathError reason and path before it is raised. Deferred:
    # no reproduction, no clearly correct fix, and the #22 pipeline owns the send-side retry.
    except (UnsafePathError, ValueError):
        raise StoreError("unsafe") from None
    except (PermissionError, FileNotFoundError):
        raise StoreError("readonly" if not readonly else "unsafe") from None
    except OSError:
        raise StoreError("internal") from None
    if files is None:
        if readonly:
            return None
        raise StoreError("internal")
    deadline = gateway.monotonic() + busy_timeout
    conn = _connect(files, busy_timeout, readonly)
    try:
        _harden(conn)
        absent = _identify_and_migrate(gateway, conn, migrations, deadline, readonly)
        if absent:
            conn.close()
            return None
        _check_tables(conn)
        conn.execute("PRAGMA query_only=ON")
        conn.execute(f"PRAGMA busy_timeout={max(1, round(busy_timeout * 1000))}")
    except sqlite3.Error as exc:
        conn.close()
        raise _fail(exc) from None
    except BaseException:
        conn.close()
        raise
    return Store(conn, readonly=readonly)


def _connect(files: StateFiles, busy_timeout: float, readonly: bool) -> sqlite3.Connection:
    try:
        if readonly:
            uri = "file:" + quote(files.path, safe="/") + "?mode=ro"
            if not files.wal_present:
                uri += "&immutable=1"
            return sqlite3.connect(uri, uri=True, timeout=busy_timeout, isolation_level=None)
        return sqlite3.connect(files.path, timeout=busy_timeout, isolation_level=None)
    except sqlite3.Error as exc:
        raise _fail(exc) from None


def _harden(conn: sqlite3.Connection) -> None:
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
    for limit, value in (
        (sqlite3.SQLITE_LIMIT_LENGTH, 1 << 20),
        (sqlite3.SQLITE_LIMIT_SQL_LENGTH, 1 << 16),
        (sqlite3.SQLITE_LIMIT_COLUMN, 128),
        (sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100),
        (sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 8),
        (sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64),
        (sqlite3.SQLITE_LIMIT_ATTACHED, 0),
        (sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, 256),
        (sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH, 8),
        (sqlite3.SQLITE_LIMIT_FUNCTION_ARG, 8),
    ):
        conn.setlimit(limit, value)
    for pragma in (
        "PRAGMA trusted_schema=OFF",
        "PRAGMA foreign_keys=ON",
        "PRAGMA cell_size_check=ON",
        "PRAGMA secure_delete=ON",  # a deleted row's message text is overwritten
        "PRAGMA synchronous=NORMAL",
        "PRAGMA temp_store=MEMORY",
        "PRAGMA query_only=ON",
    ):
        conn.execute(pragma)


def _identity(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """``(application_id, user_version, number of schema objects)``, read in one snapshot: a
    concurrent creator commits all three together, and three separate reads could see a mix."""
    if conn.in_transaction:
        return _identity_rows(conn)
    conn.execute("BEGIN")
    try:
        return _identity_rows(conn)
    finally:
        conn.execute("ROLLBACK")


def _identity_rows(conn: sqlite3.Connection) -> tuple[int, int, int]:
    app = conn.execute("PRAGMA application_id").fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    objects = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    return int(app), int(version), int(objects)


def _validate_identity(
    app: int, version: int, objects: int, target: int
) -> Literal["fresh", "current", "older"]:
    if version == 0 and objects == 0 and app in (0, APPLICATION_ID):
        return "fresh"
    if app != APPLICATION_ID or version == 0:
        raise StoreError("foreign")
    if version > target:
        raise StoreError("newer")
    if version < target:
        return "older"
    return "current"


def _identify_and_migrate(
    gateway: Gateway,
    conn: sqlite3.Connection,
    migrations: tuple[Migration, ...],
    deadline: float,
    readonly: bool,
) -> bool:
    """Returns ``True`` only for a read-only open of a file that is not yet a database (empty:
    ``prepare_state_db`` creates the file before the first migration, and a crash can leave it)."""
    target = len(migrations)
    _set_busy(gateway, conn, deadline)
    state = _validate_identity(*_identity(conn), target)
    if readonly:
        if state == "fresh":
            return True
        if state == "older":
            raise StoreError("older")
        return False
    _ensure_wal(gateway, conn, deadline)
    if state == "current":
        return False
    _set_busy(gateway, conn, deadline)
    _migrate(conn, migrations)
    return False


def _set_busy(gateway: Gateway, conn: sqlite3.Connection, deadline: float) -> None:
    """Let the next stage wait only for what is left of the open's one deadline."""
    left = deadline - gateway.monotonic()
    if left <= 0:
        raise StoreError("busy")
    conn.execute(f"PRAGMA busy_timeout={max(1, int(left * 1000))}")


def _ensure_wal(gateway: Gateway, conn: sqlite3.Connection, deadline: float) -> None:
    """Switch to WAL. The switch needs the file to itself for an instant and SQLite may report
    ``busy`` without waiting, so retry until the open's deadline has passed (two processes creating
    the database at once)."""
    while True:
        try:
            if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                return
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            if str(row[0]).lower() == "wal":
                return
            raise StoreError("internal")
        except sqlite3.Error as exc:
            if _classify(exc) != "busy" or gateway.monotonic() >= deadline:
                raise
            gateway.clock.sleep(0.01)
            _set_busy(gateway, conn, deadline)


def _migrate(conn: sqlite3.Connection, migrations: tuple[Migration, ...]) -> None:
    """Apply the missing migrations in one transaction. The version is read again inside it:
    a concurrent process may have migrated (or created) the database while this one waited."""
    try:
        conn.execute("PRAGMA query_only=OFF")
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise _fail(exc) from None
    try:
        app, version, objects = _identity(conn)
        state = _validate_identity(app, version, objects, len(migrations))
        if state != "current":
            if state == "older":
                _quick_check(conn)
            for number in range(version, len(migrations)):
                for step in migrations[number]:
                    if isinstance(step, str):
                        conn.execute(step)
                    else:
                        step(conn)
            conn.execute(f"PRAGMA application_id={int(APPLICATION_ID)}")
            conn.execute(f"PRAGMA user_version={int(len(migrations))}")
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        if isinstance(exc, StoreError):
            raise
        if isinstance(exc, sqlite3.Error) and _classify(exc) in ("busy", "corrupt", "readonly"):
            raise _fail(exc) from None
        if isinstance(exc, Exception):
            raise StoreError("migration") from None
        raise
    finally:
        try:
            conn.execute("PRAGMA query_only=ON")
        except sqlite3.Error:
            pass


def _quick_check(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]:
        raise StoreError("corrupt")


def _check_tables(conn: sqlite3.Connection) -> None:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not set(TABLES) <= names:
        raise StoreError("corrupt")


# --- bounded tables ------------------------------------------------------------------------

_ELIGIBLE = (
    "NOT EXISTS (SELECT 1 FROM queue q WHERE q.delivery_id = d.id)"
    " AND NOT EXISTS (SELECT 1 FROM surfaced s WHERE s.delivery_id = d.id AND s.processed = 0)"
)
_OLDEST_DROPPED = (
    "SELECT d.id FROM deliveries d WHERE d.id IS NOT ? AND d.outcome = 'dropped' AND "
    + _ELIGIBLE
    + " ORDER BY d.id LIMIT ?"
)
_OLDEST_OTHER = (
    "SELECT d.id FROM deliveries d WHERE d.id IS NOT ? AND d.outcome <> 'dropped' AND "
    + _ELIGIBLE
    + " ORDER BY d.id LIMIT ?"
)


def _excess(conn: sqlite3.Connection, table: str, cap: int, batch: int) -> int:
    if type(cap) is not int or cap < 0 or type(batch) is not int or batch < 1:
        raise ValueError("cap must be a non-negative int and batch a positive int")
    if table == "deliveries":
        count = conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
    else:
        count = conn.execute("SELECT COUNT(*) FROM event_ids").fetchone()[0]
    return min(max(0, count - cap), batch)


def trim_deliveries(
    conn: sqlite3.Connection,
    *,
    cap: int = DELIVERIES_CAP,
    batch: int = EVICT_BATCH,
    keep_id: int | None = None,
) -> int:
    """Call inside the write transaction that inserted a ``deliveries`` row: delete the oldest
    rows beyond ``cap``, at most ``batch``, ``dropped`` rows first, never a row that has a queue
    row or an unprocessed ``surfaced`` row (the table may then stay above the cap). Returns the
    number of rows deleted. ``keep_id`` (the row the caller just inserted) is never deleted.
    ``queue.insert_delivery`` calls it (#16); the decision transaction inserts through that."""
    want = _excess(conn, "deliveries", cap, batch)
    if want == 0:
        return 0
    ids = [r[0] for r in conn.execute(_OLDEST_DROPPED, (keep_id, want))]
    if len(ids) < want:
        ids += [r[0] for r in conn.execute(_OLDEST_OTHER, (keep_id, want - len(ids)))]
    conn.executemany("DELETE FROM deliveries WHERE id = ?", [(i,) for i in ids])
    return len(ids)


# Oldest first by row id (indexed: no sort). dedup.record_event_id gives a replaced record a
# fresh id, so the id order is the order of the last write.
TRIM_EVENT_IDS_SQL = (
    "DELETE FROM event_ids WHERE id IN"
    " (SELECT id FROM event_ids WHERE id IS NOT ? ORDER BY id LIMIT ?)"
)


def trim_event_ids(
    conn: sqlite3.Connection,
    *,
    cap: int = EVENT_IDS_CAP,
    batch: int = EVICT_BATCH,
    keep_id: int | None = None,
) -> int:
    """As :func:`trim_deliveries` for ``event_ids`` (nothing protects an event id row
    but ``keep_id``, the row the caller just inserted). ``dedup.record_event_id`` calls it."""
    want = _excess(conn, "event_ids", cap, batch)
    if want == 0:
        return 0
    cur = conn.execute(TRIM_EVENT_IDS_SQL, (keep_id, want))
    return cur.rowcount
