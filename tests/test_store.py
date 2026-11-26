"""The state database (#14): creation, hardening, migrations, caps and concurrency."""

from __future__ import annotations

import os
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from notice_route import store as store_module
from notice_route.gateway import FakeClock, Gateway
from notice_route.schema import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION, TABLES
from notice_route.store import (
    DELIVERIES_CAP,
    Store,
    StoreError,
    open_readonly,
    open_store,
    trim_deliveries,
    trim_event_ids,
)


@pytest.fixture
def gw(home: Path) -> Gateway:
    return Gateway(passwd_home=lambda: str(home), clock=FakeClock())


@pytest.fixture
def state(home: Path) -> Path:
    return home / ".local" / "state" / "notice-route"


@pytest.fixture
def db(state: Path) -> Path:
    return state / "state.db"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def pragma(conn: sqlite3.Connection, name: str) -> object:
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def raw(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(db)


def ro(gw: Gateway) -> Store:
    store = open_readonly(gw)
    assert store is not None
    return store


def add_delivery(store: Store, outcome: str = "sent", destination: str = "d") -> int:
    with store.write() as conn:
        cur = conn.execute(
            "INSERT INTO deliveries (ts, source, level, outcome, destination)"
            " VALUES (?, ?, ?, ?, ?)",
            (1, "s", "info", outcome, destination),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid


# --- creation ------------------------------------------------------------------------------


def test_first_open_creates_a_private_versioned_wal_database(
    gw: Gateway, state: Path, db: Path
) -> None:
    with open_store(gw) as store:
        assert store is not None
        assert pragma(store.conn, "user_version") == SCHEMA_VERSION == 3
        assert pragma(store.conn, "application_id") == APPLICATION_ID
        assert str(pragma(store.conn, "journal_mode")).lower() == "wal"
    assert mode(state) == 0o700
    assert mode(db) == 0o600
    c = raw(db)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLES) <= names
    assert {r[0] for r in c.execute("SELECT key FROM meta")} == {"flush_last", "flush_env_missing"}
    c.close()


def test_modes_hold_under_a_permissive_umask(gw: Gateway, state: Path, db: Path) -> None:
    old = os.umask(0o022)
    try:
        open_store(gw).close()
    finally:
        os.umask(old)
    assert mode(state) == 0o700 and mode(db) == 0o600
    for suffix in ("-wal", "-shm"):
        side = state / f"state.db{suffix}"
        if side.exists():
            assert mode(side) == 0o600


def test_reopening_changes_nothing(gw: Gateway, db: Path) -> None:
    with open_store(gw) as s:
        assert s is not None
        add_delivery(s)
    with open_store(gw) as s:
        assert s is not None
        assert s.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1
        assert pragma(s.conn, "user_version") == SCHEMA_VERSION


def test_connection_is_hardened(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        c = store.conn
        assert pragma(c, "foreign_keys") == 1
        assert pragma(c, "synchronous") == 1  # NORMAL
        assert pragma(c, "trusted_schema") == 0
        assert pragma(c, "cell_size_check") == 1
        assert pragma(c, "secure_delete") == 1
        assert pragma(c, "busy_timeout") == 5000
        assert pragma(c, "query_only") == 1
        assert c.isolation_level is None
        assert c.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED) == 0
        with pytest.raises(sqlite3.Error):
            c.execute("ATTACH DATABASE ':memory:' AS other")
        with pytest.raises(sqlite3.Error):
            c.execute("PRAGMA writable_schema=ON; UPDATE sqlite_master SET name=name")


def test_busy_timeout_is_a_parameter(gw: Gateway) -> None:
    with open_store(gw, busy_timeout=0.5) as store:
        assert store is not None
        assert pragma(store.conn, "busy_timeout") == 500


def test_foreign_keys_hold(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        with pytest.raises(sqlite3.IntegrityError), store.write() as conn:
            conn.execute(
                "INSERT INTO queue (delivery_id, kind, message, level, source, deliver_after,"
                " created_at) VALUES (999, 'single', 'm', 'info', 's', 1, 1)"
            )


# --- writes ----------------------------------------------------------------------------------


def test_every_write_is_begin_immediate(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        seen: list[str] = []
        store.conn.set_trace_callback(seen.append)
        add_delivery(store)
        store.conn.set_trace_callback(None)
        first_write = next(i for i, q in enumerate(seen) if q.startswith("INSERT"))
        assert seen.index("BEGIN IMMEDIATE") < first_write
        assert not [s for s in seen if s.strip().upper() in ("BEGIN", "BEGIN DEFERRED")]


def test_a_write_outside_the_write_context_is_refused(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        with pytest.raises(sqlite3.OperationalError):
            store.conn.execute(
                "INSERT INTO deliveries (ts, source, level, outcome) VALUES (1,'s','i','sent')"
            )
        with pytest.raises(sqlite3.OperationalError):  # a plain BEGIN cannot be upgraded either
            store.conn.execute("BEGIN")
            try:
                store.conn.execute(
                    "INSERT INTO deliveries (ts, source, level, outcome) VALUES (1,'s','i','x')"
                )
            finally:
                store.conn.execute("ROLLBACK")


def test_a_failed_write_rolls_back_and_restores_read_only(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        with pytest.raises(RuntimeError), store.write() as conn:
            conn.execute(
                "INSERT INTO deliveries (ts, source, level, outcome) VALUES (1,'s','i','sent')"
            )
            raise RuntimeError("boom")
        assert store.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
        assert pragma(store.conn, "query_only") == 1
        assert not store.conn.in_transaction


def test_write_contexts_do_not_nest(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        with store.write(), pytest.raises(RuntimeError), store.write():
            pass


def test_injection_shaped_values_are_data(gw: Gateway) -> None:
    nasty = "x'); DROP TABLE deliveries; --"
    with open_store(gw) as store:
        assert store is not None
        with store.write() as conn:
            conn.execute(
                "INSERT INTO deliveries (ts, source, level, outcome, destination)"
                " VALUES (1, ?, 'info', 'sent', ?)",
                (nasty, nasty),
            )
        row = store.conn.execute("SELECT source, destination FROM deliveries").fetchone()
        assert row == (nasty, nasty)


def test_row_ids_are_not_reused(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        first = add_delivery(store)
        with store.write() as conn:
            conn.execute("DELETE FROM deliveries")
        assert add_delivery(store) > first


# --- caps ------------------------------------------------------------------------------------


def _fill(store: Store, n: int, outcome: str = "sent") -> list[int]:
    return [add_delivery(store, outcome) for _ in range(n)]


def test_cap_constant_is_the_spec_value() -> None:
    assert DELIVERIES_CAP == 100000


def test_trim_removes_oldest_beyond_the_cap_at_most_16_per_call(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        ids = _fill(store, 50)
        with store.write() as conn:
            assert trim_deliveries(conn, cap=30) == 16
        left = [r[0] for r in store.conn.execute("SELECT id FROM deliveries ORDER BY id")]
        assert left == ids[16:]
        with store.write() as conn:
            assert trim_deliveries(conn, cap=30) == 4
            assert trim_deliveries(conn, cap=30) == 0
        assert store.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 30


def test_trim_removes_dropped_rows_first(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        a = add_delivery(store, "sent")
        b = add_delivery(store, "dropped")
        c = add_delivery(store, "sent")
        d = add_delivery(store, "dropped")
        with store.write() as conn:
            assert trim_deliveries(conn, cap=2) == 2
        left = {r[0] for r in store.conn.execute("SELECT id FROM deliveries")}
        assert left == {a, c}
        assert b not in left and d not in left


def test_trim_never_removes_a_protected_row(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        queued = add_delivery(store, "dropped")
        pending = add_delivery(store, "dropped")
        done = add_delivery(store, "dropped")
        free = add_delivery(store, "sent")
        with store.write() as conn:
            conn.execute(
                "INSERT INTO queue (delivery_id, kind, message, level, source, deliver_after,"
                " created_at) VALUES (?, 'held', 'm', 'info', 's', 1, 1)",
                (queued,),
            )
            conn.execute(
                "INSERT INTO surfaced (destination, code, period, delivery_id, processed)"
                " VALUES ('d', 'failed', '2023-11-14', ?, 0)",
                (pending,),
            )
            conn.execute(
                "INSERT INTO surfaced (destination, code, period, delivery_id, processed)"
                " VALUES ('d', 'killed', '2023-11-14', ?, 1)",
                (done,),
            )
            assert trim_deliveries(conn, cap=0) == 2
        left = {r[0] for r in store.conn.execute("SELECT id FROM deliveries")}
        assert left == {queued, pending}
        assert free not in left and done not in left


def test_trim_event_ids(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        with store.write() as conn:
            for i in range(10):
                conn.execute(
                    "INSERT INTO event_ids (destination, source, event_id, repeat_digest, seen_at)"
                    " VALUES ('d', 's', ?, 'x', 1)",
                    (str(i),),
                )
            assert trim_event_ids(conn, cap=7) == 3
        ids = [r[0] for r in store.conn.execute("SELECT event_id FROM event_ids ORDER BY id")]
        assert ids == [str(i) for i in range(3, 10)]


def test_trim_rejects_a_bad_cap(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        with store.write() as conn:
            with pytest.raises(ValueError):
                trim_deliveries(conn, cap=-1)
            with pytest.raises(ValueError):
                trim_event_ids(conn, cap=True)  # type: ignore[arg-type]


# --- migrations ------------------------------------------------------------------------------

V2 = (*MIGRATIONS, ("ALTER TABLE deliveries ADD COLUMN extra INTEGER NOT NULL DEFAULT 7",))


def test_a_migration_adds_a_column_with_a_default_and_old_rows_read(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        old = add_delivery(store)
    with open_store(gw, migrations=V2) as store:
        assert store is not None
        assert pragma(store.conn, "user_version") == SCHEMA_VERSION + 1
        row = store.conn.execute("SELECT id, extra FROM deliveries").fetchone()
        assert row == (old, 7)


def test_a_failing_migration_leaves_the_old_version_usable(gw: Gateway) -> None:
    broken = (*MIGRATIONS, ("ALTER TABLE deliveries ADD COLUMN a INTEGER", "NOT SQL AT ALL"))
    with open_store(gw) as store:
        assert store is not None
        add_delivery(store)
    with pytest.raises(StoreError) as err:
        open_store(gw, migrations=broken)
    assert err.value.reason == "migration"
    with open_store(gw) as store:
        assert store is not None
        assert pragma(store.conn, "user_version") == SCHEMA_VERSION
        cols = [r[1] for r in store.conn.execute("PRAGMA table_info(deliveries)")]
        assert "a" not in cols
        assert store.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1


def test_a_newer_schema_is_refused_and_untouched(gw: Gateway, db: Path) -> None:
    open_store(gw).close()
    c = raw(db)
    c.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    c.close()
    before = db.read_bytes()
    with pytest.raises(StoreError) as err:
        open_store(gw)
    assert err.value.reason == "newer"
    assert db.read_bytes() == before


def test_a_newer_schema_is_refused_read_only_too(gw: Gateway, db: Path) -> None:
    open_store(gw).close()
    c = raw(db)
    c.execute("PRAGMA user_version=99")
    c.close()
    with pytest.raises(StoreError) as err:
        open_readonly(gw)
    assert err.value.reason == "newer"


def test_an_older_database_is_not_migrated_read_only(gw: Gateway, state: Path) -> None:
    open_store(gw).close()
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    with pytest.raises(StoreError) as err:
        open_readonly(gw, migrations=V2)
    assert err.value.reason == "older"
    assert {p.name: p.read_bytes() for p in state.iterdir()} == before  # nothing migrated


# --- refused databases -----------------------------------------------------------------------


def _assert_fixed(err: StoreError) -> None:
    text = str(err)
    assert text == store_module.MESSAGES[err.reason]
    assert "SQLite" not in text and "/" not in text


@pytest.mark.parametrize(
    "content",
    [
        b"not a database at all " * 50,
        b"SQLite format 3\x00" + b"\xff" * 4000,  # right magic, garbage after it
        b"\x00" * 8192,
    ],
)
def test_garbage_is_refused_with_a_fixed_error(gw: Gateway, state: Path, content: bytes) -> None:
    state.mkdir(parents=True)
    state.chmod(0o700)
    (state / "state.db").write_bytes(content)
    (state / "state.db").chmod(0o600)
    with pytest.raises(StoreError) as err:
        open_store(gw)
    assert err.value.reason in ("corrupt", "foreign")
    _assert_fixed(err.value)
    assert (state / "state.db").read_bytes() == content


def test_a_truncated_database_is_refused(gw: Gateway, db: Path) -> None:
    with open_store(gw) as store:
        assert store is not None
        for _ in range(200):
            add_delivery(store)
    c = raw(db)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    size = db.stat().st_size
    with db.open("r+b") as f:
        f.truncate(size // 2)
    with pytest.raises(StoreError) as err:
        open_store(gw)
    _assert_fixed(err.value)


def test_a_flipped_page_is_refused(gw: Gateway, db: Path) -> None:
    with open_store(gw) as store:
        assert store is not None
        for _ in range(300):
            add_delivery(store)
    c = raw(db)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    data = bytearray(db.read_bytes())
    for i in range(4096 * 2, len(data), 97):
        data[i] ^= 0xFF
    db.write_bytes(bytes(data))
    try:
        with pytest.raises(StoreError) as err:
            with open_store(gw) as store:
                assert store is not None
                store.check_integrity()
        _assert_fixed(err.value)
    finally:
        pass


def test_another_programs_sqlite_database_is_refused_and_untouched(
    gw: Gateway, state: Path, db: Path
) -> None:
    state.mkdir(parents=True)
    state.chmod(0o700)
    c = raw(db)
    c.execute("CREATE TABLE other (x)")
    c.commit()
    c.close()
    db.chmod(0o600)
    before = db.read_bytes()
    with pytest.raises(StoreError) as err:
        open_store(gw)
    assert err.value.reason == "foreign"
    assert db.read_bytes() == before


def test_a_database_missing_a_table_is_refused(gw: Gateway, db: Path) -> None:
    open_store(gw).close()
    c = raw(db)
    c.execute("DROP TABLE rate")
    c.close()
    with pytest.raises(StoreError) as err:
        open_store(gw)
    assert err.value.reason == "corrupt"


def test_a_read_only_file_is_refused(gw: Gateway, db: Path) -> None:
    open_store(gw).close()
    db.chmod(0o400)
    try:
        with pytest.raises(StoreError) as err:
            open_store(gw)
        assert err.value.reason == "readonly"
    finally:
        db.chmod(0o600)


def test_a_read_only_directory_is_refused(gw: Gateway, state: Path) -> None:
    open_store(gw).close()
    for name in ("state.db-wal", "state.db-shm"):
        (state / name).unlink(missing_ok=True)
    state.chmod(0o500)
    try:
        with pytest.raises(StoreError) as err:
            open_store(gw)
        assert err.value.reason in ("readonly", "unsafe")
    finally:
        state.chmod(0o700)


def test_a_busy_database_is_refused_after_the_timeout(gw: Gateway, db: Path) -> None:
    open_store(gw).close()
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with open_store(gw, busy_timeout=0.2) as store:
            assert store is not None
            t0 = time.monotonic()
            with pytest.raises(StoreError) as err, store.write():
                pass
            assert err.value.reason == "busy"
            assert time.monotonic() - t0 < 3
            _assert_fixed(err.value)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


# --- unsafe paths ----------------------------------------------------------------------------


def _mkstate(state: Path) -> None:
    state.mkdir(parents=True)
    state.chmod(0o700)


def test_a_symlinked_db_is_refused_and_the_target_is_untouched(
    gw: Gateway, state: Path, tmp_path: Path
) -> None:
    _mkstate(state)
    target = tmp_path / "victim.db"
    (state / "state.db").symlink_to(target)
    with pytest.raises(StoreError) as err:
        open_store(gw)
    assert err.value.reason == "unsafe"
    assert not target.exists()  # nothing created through the link
    real = tmp_path / "real.db"
    sqlite3.connect(real).close()
    (state / "state.db").unlink()
    (state / "state.db").symlink_to(real)
    size = real.stat().st_size
    with pytest.raises(StoreError):
        open_store(gw)
    assert real.stat().st_size == size


def test_a_symlinked_db_is_refused_read_only_too(gw: Gateway, state: Path, tmp_path: Path) -> None:
    _mkstate(state)
    real = tmp_path / "real.db"
    sqlite3.connect(real).close()
    (state / "state.db").symlink_to(real)
    with pytest.raises(StoreError):
        open_readonly(gw)


def test_a_fifo_in_place_of_the_db_is_refused(gw: Gateway, state: Path) -> None:
    _mkstate(state)
    os.mkfifo(state / "state.db")
    with pytest.raises(StoreError) as err:
        open_store(gw)
    assert err.value.reason == "unsafe"


def test_a_directory_in_place_of_the_db_is_refused(gw: Gateway, state: Path) -> None:
    _mkstate(state)
    (state / "state.db").mkdir()
    with pytest.raises(StoreError) as err:
        open_store(gw)
    assert err.value.reason == "unsafe"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_a_symlink_or_fifo_sidecar_is_refused(
    gw: Gateway, state: Path, tmp_path: Path, suffix: str
) -> None:
    open_store(gw).close()
    side = state / f"state.db{suffix}"
    side.unlink(missing_ok=True)
    victim = tmp_path / "victim"
    side.symlink_to(victim)
    with pytest.raises(StoreError) as err:
        open_store(gw)
    assert err.value.reason == "unsafe"
    assert not victim.exists()
    side.unlink()
    os.mkfifo(side)
    with pytest.raises(StoreError) as err:
        open_store(gw)
    assert err.value.reason == "unsafe"


def test_a_hard_linked_db_is_refused(gw: Gateway, state: Path, db: Path, tmp_path: Path) -> None:
    open_store(gw).close()
    os.link(db, tmp_path / "second-name")
    with pytest.raises(StoreError) as err:
        open_store(gw)
    assert err.value.reason == "unsafe"


def test_a_db_with_group_or_world_access_is_refused(gw: Gateway, db: Path) -> None:
    open_store(gw).close()
    for bad in (0o640, 0o604, 0o660):
        db.chmod(bad)
        with pytest.raises(StoreError) as err:
            open_store(gw)
        assert err.value.reason == "unsafe"
    db.chmod(0o600)


def test_a_wide_state_directory_is_refused(gw: Gateway, state: Path) -> None:
    open_store(gw).close()
    for bad in (0o750, 0o705, 0o770):
        state.chmod(bad)
        with pytest.raises(StoreError) as err:
            open_store(gw)
        assert err.value.reason == "unsafe"
    state.chmod(0o700)


def test_a_symlinked_state_directory_is_refused(gw: Gateway, state: Path, tmp_path: Path) -> None:
    real = tmp_path / "real-state"
    real.mkdir(mode=0o700)
    state.parent.mkdir(parents=True)
    state.symlink_to(real)
    with pytest.raises(StoreError) as err:
        open_store(gw)
    assert err.value.reason == "unsafe"
    assert not (real / "state.db").exists()


def test_a_state_directory_that_is_a_file_is_refused(gw: Gateway, state: Path) -> None:
    state.parent.mkdir(parents=True)
    state.write_text("x")
    with pytest.raises(StoreError) as err:
        open_store(gw)
    assert err.value.reason == "unsafe"


def test_a_path_with_uri_characters_works(tmp_path: Path) -> None:
    odd = tmp_path / "h?me#1%20 x"
    odd.mkdir()
    g = Gateway(passwd_home=lambda: str(odd), clock=FakeClock())
    open_store(g).close()
    with ro(g) as store:
        assert store is not None
        assert pragma(store.conn, "user_version") == SCHEMA_VERSION


# --- read-only open --------------------------------------------------------------------------


def test_read_only_open_of_a_missing_database_creates_nothing(gw: Gateway, home: Path) -> None:
    assert open_readonly(gw) is None
    assert not (home / ".local").exists()


def test_read_only_open_reads_and_cannot_write(gw: Gateway, state: Path) -> None:
    with open_store(gw) as s:
        assert s is not None
        add_delivery(s)
    for name in ("state.db-wal", "state.db-shm"):
        (state / name).unlink(missing_ok=True)
    before = sorted(p.name for p in state.iterdir())
    with ro(gw) as store:
        assert store is not None
        assert store.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            store.conn.execute("DELETE FROM deliveries")
        with pytest.raises(RuntimeError), store.write():
            pass
    assert sorted(p.name for p in state.iterdir()) == before


def test_read_only_open_sees_uncheckpointed_rows(gw: Gateway) -> None:
    with open_store(gw) as writer:
        assert writer is not None
        add_delivery(writer)
        with ro(gw) as store:
            assert store is not None
            assert store.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1


# --- processes -------------------------------------------------------------------------------

CHILD_OPEN = """
import sys, time, pathlib
from notice_route.gateway import Gateway
from notice_route.store import open_store
home, go = sys.argv[1], pathlib.Path(sys.argv[2])
gw = Gateway(passwd_home=lambda: home)
deadline = time.monotonic() + 20
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.005)
with open_store(gw, busy_timeout=10.0) as s:
    print(s.conn.execute("PRAGMA user_version").fetchone()[0])
"""

CHILD_MIGRATE = """
import sys, time, pathlib
from notice_route.gateway import Gateway
from notice_route.schema import MIGRATIONS
from notice_route.store import open_store
home, marker = sys.argv[1], pathlib.Path(sys.argv[2])
def stall(conn):
    marker.write_text("in transaction")
    time.sleep(60)
v2 = (*MIGRATIONS, ("ALTER TABLE deliveries ADD COLUMN extra INTEGER NOT NULL DEFAULT 7", stall))
open_store(Gateway(passwd_home=lambda: home), migrations=v2)
"""


def _env(home: Path) -> dict[str, str]:
    return {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": "/usr/bin:/bin"}


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen[str]]]:
    started: list[subprocess.Popen[str]] = []
    yield started
    for p in started:  # exactly the processes this test started, by their handles
        if p.poll() is None:
            os.kill(p.pid, signal.SIGKILL)
        p.wait()
        for stream in (p.stdout, p.stderr):
            if stream:
                stream.close()


def test_concurrent_first_run_creation_is_safe(
    home: Path, tmp_path: Path, children: list[subprocess.Popen[str]]
) -> None:
    go = tmp_path / "go"
    for _ in range(6):
        children.append(
            subprocess.Popen(
                [sys.executable, "-c", CHILD_OPEN, str(home), str(go)],
                env=_env(home),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    time.sleep(0.5)  # every child is polling for the start file
    go.write_text("go")
    results = [(p.pid, *p.communicate(timeout=60), p.returncode) for p in children]
    for pid, out, err, code in results:
        assert code == 0, (pid, err)
        assert out.strip() == str(SCHEMA_VERSION)
    db = home / ".local" / "state" / "notice-route" / "state.db"
    c = raw(db)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 2
    c.close()
    assert mode(db) == 0o600


def test_a_crash_in_the_middle_of_a_migration_leaves_a_usable_database(
    gw: Gateway, home: Path, tmp_path: Path, children: list[subprocess.Popen[str]]
) -> None:
    with open_store(gw) as store:
        assert store is not None
        add_delivery(store)
    marker = tmp_path / "marker"
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD_MIGRATE, str(home), str(marker)],
        env=_env(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    children.append(child)
    deadline = time.monotonic() + 30
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists()
    os.kill(child.pid, signal.SIGKILL)
    child.wait(timeout=10)
    with open_store(gw) as store:  # the half-done migration is gone
        assert store is not None
        assert pragma(store.conn, "user_version") == SCHEMA_VERSION
        assert "extra" not in [r[1] for r in store.conn.execute("PRAGMA table_info(deliveries)")]
        assert store.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1
        assert store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with open_store(gw, migrations=V2) as store:  # and a later run migrates it
        assert store is not None
        assert store.conn.execute("SELECT extra FROM deliveries").fetchone()[0] == 7


# --- review round 1 (R1, R2, R4, R5, R6, R7) -----------------------------------------------


def _queue_row(conn: sqlite3.Connection, delivery_id: int) -> None:
    conn.execute(
        "INSERT INTO queue (delivery_id, kind, message, level, source, deliver_after,"
        " created_at) VALUES (?, 'held', 'm', 'info', 's', 1, 1)",
        (delivery_id,),
    )


def test_trim_keeps_the_row_the_caller_just_inserted_when_it_is_the_only_dropped_one(
    gw: Gateway,
) -> None:
    with open_store(gw) as store:
        assert store is not None
        old = add_delivery(store, "sent")
        with store.write() as conn:
            _queue_row(conn, old)
        new = add_delivery(store, "dropped")
        with store.write() as conn:
            assert trim_deliveries(conn, cap=0, keep_id=new) == 0
        assert {r[0] for r in store.conn.execute("SELECT id FROM deliveries")} == {old, new}


def test_trim_keeps_the_new_sent_row_when_every_older_row_is_protected(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        olds = _fill(store, 3)
        with store.write() as conn:
            for i in olds:
                _queue_row(conn, i)
        new = add_delivery(store, "sent")
        with store.write() as conn:
            assert trim_deliveries(conn, cap=0, keep_id=new) == 0
        assert store.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 4


def test_trim_event_ids_keeps_the_given_row(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        with store.write() as conn:
            for i in range(3):
                conn.execute(
                    "INSERT INTO event_ids (destination, source, event_id, repeat_digest, seen_at)"
                    " VALUES ('d', 's', ?, 'x', 1)",
                    (str(i),),
                )
            newest = conn.execute("SELECT MAX(id) FROM event_ids").fetchone()[0]
            assert trim_event_ids(conn, cap=0, keep_id=newest) == 2
        assert [r[0] for r in store.conn.execute("SELECT id FROM event_ids")] == [newest]


def test_protected_non_dropped_rows_survive_a_cap_of_zero(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        queued = add_delivery(store, "sent")
        pending = add_delivery(store, "sent")
        free = add_delivery(store, "sent")
        with store.write() as conn:
            _queue_row(conn, queued)
            conn.execute(
                "INSERT INTO surfaced (destination, code, period, delivery_id, processed)"
                " VALUES ('d', 'failed', '2023-11-14', ?, 0)",
                (pending,),
            )
            assert trim_deliveries(conn, cap=0) == 1
        left = {r[0] for r in store.conn.execute("SELECT id FROM deliveries")}
        assert left == {queued, pending} and free not in left


def _zero_byte_db(state: Path, db: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    db.touch(mode=0o600)
    assert db.stat().st_size == 0


def test_a_zero_byte_database_is_absent_for_open_readonly(
    gw: Gateway, state: Path, db: Path
) -> None:
    _zero_byte_db(state, db)
    assert open_readonly(gw) is None
    assert db.stat().st_size == 0


def test_a_zero_byte_database_is_an_empty_view_for_read_state(
    gw: Gateway, state: Path, db: Path
) -> None:
    _zero_byte_db(state, db)
    view = gw.read_state()
    assert view.exists is False and view.queue_by_destination == {}


def test_a_database_with_our_application_id_and_nothing_else_is_absent(
    gw: Gateway, state: Path, db: Path
) -> None:
    _zero_byte_db(state, db)
    c = raw(db)
    c.execute(f"PRAGMA application_id={APPLICATION_ID}")
    c.close()
    assert open_readonly(gw) is None
    assert gw.read_state().exists is False


def test_read_state_reads_an_older_database(gw: Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    from notice_route import gateway as gateway_module

    with open_store(gw) as store:
        assert store is not None
        d = add_delivery(store, "sent", "dest")
        with store.write() as conn:
            _queue_row(conn, d)
            conn.execute(
                "INSERT INTO surfaced (destination, code, period, delivery_id, processed)"
                " VALUES ('dest', 'failed', ?, ?, 0)",
                (time.strftime("%Y-%m-%d", time.gmtime(gw.wall())), d),
            )
    monkeypatch.setattr(gateway_module, "SCHEMA_VERSION", 2)
    view = gw.read_state()
    assert view.exists is True
    assert view.surfaced_today == {"dest": ("failed",)}
    assert view.queue_by_destination == {"dest": 1}


def test_read_returns_rows_and_takes_bound_parameters(gw: Gateway) -> None:
    with open_store(gw) as store:
        assert store is not None
        i = add_delivery(store)
        assert store.read("SELECT id FROM deliveries WHERE id = ?", (i,)) == [(i,)]


def test_a_read_of_a_trashed_page_is_a_store_error(gw: Gateway, db: Path) -> None:
    with open_store(gw) as store:
        assert store is not None
        for _ in range(300):
            add_delivery(store)
    c = raw(db)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    data = bytearray(db.read_bytes())
    for i in range(4096 * 2, len(data), 97):
        data[i] ^= 0xFF
    db.write_bytes(bytes(data))
    with open_store(gw) as store:
        assert store is not None
        with pytest.raises(StoreError) as err:
            store.read("SELECT * FROM deliveries")
    assert err.value.reason == "corrupt"
    _assert_fixed(err.value)


def test_busy_timeout_rejects_a_bool(gw: Gateway) -> None:
    with pytest.raises(ValueError):
        open_store(gw, busy_timeout=True)  # type: ignore[arg-type]


class _SlowClock(FakeClock):
    """Every look at the monotonic clock costs ``step`` seconds, like slow stages would."""

    def __init__(self, step: float) -> None:
        super().__init__()
        self.step = step

    def monotonic(self) -> float:
        self.advance_monotonic(self.step)
        return super().monotonic()


def test_the_open_path_shares_one_deadline(home: Path) -> None:
    gw = Gateway(passwd_home=lambda: str(home), clock=_SlowClock(0.3))
    with pytest.raises(StoreError) as err:
        open_store(gw, busy_timeout=0.5)
    assert err.value.reason == "busy"


@pytest.mark.parametrize(("busy_timeout", "refused"), [(0.5, True), (5.0, False)])
def test_the_busy_deadline_follows_the_gateway_clock(
    home: Path, monkeypatch: pytest.MonkeyPatch, busy_timeout: float, refused: bool
) -> None:
    """The deadline is read from the gateway clock, never from real time: a stall that the clock
    counts as longer than the timeout is ``busy`` (a test that speeds the clock up scales the
    timeout, see ``tests/send_support.py``; the product is not to be changed for it)."""
    clock = FakeClock()
    gw = Gateway(passwd_home=lambda: str(home), clock=clock)
    real = store_module._ensure_wal  # pyright: ignore[reportPrivateUsage]

    def stalled(*args: object, **kwargs: object) -> None:
        clock.advance_monotonic(1.0)  # one second passes on the clock
        real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module, "_ensure_wal", stalled)
    if refused:
        with pytest.raises(StoreError) as err:
            open_store(gw, busy_timeout=busy_timeout)
        assert err.value.reason == "busy"
    else:
        with open_store(gw, busy_timeout=busy_timeout) as store:
            assert store is not None


def test_a_normal_open_leaves_the_full_busy_timeout_set(home: Path) -> None:
    gw = Gateway(passwd_home=lambda: str(home), clock=_SlowClock(0.001))
    with open_store(gw, busy_timeout=0.5) as store:
        assert store is not None
        assert pragma(store.conn, "busy_timeout") == 500


def test_a_tiny_busy_timeout_stays_nonzero_after_open(gw: Gateway) -> None:
    with open_store(gw, busy_timeout=0.0004) as store:
        assert store is not None
        value = pragma(store.conn, "busy_timeout")
        assert isinstance(value, int)
        assert value >= 1
