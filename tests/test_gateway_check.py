"""Gateway checks for ``validate`` (#9): ``check_exec`` (cmd_ok), ``check_env_file`` and
``read_state``. Nothing here runs a command, reads the env file or writes."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from notice_route import gateway as gateway_module
from notice_route.gateway import FakeClock, Gateway, StateError

# 2023-11-14 22:13:20 UTC
NOW = 1_700_000_000.0


def _dir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    return path


def _exe(path: Path, mode: int = 0o700) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(mode)
    return path


@pytest.fixture
def gw(tmp_path: Path) -> Gateway:
    """The directories above ``tmp_path`` (``/tmp`` is world-writable) are not under test."""
    _dir(tmp_path / "t")
    return Gateway(exec_check_from=str(tmp_path / "t"))


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path / "t"


# --- check_exec ------------------------------------------------------------------------------


def test_private_executable_passes(gw: Gateway, base: Path) -> None:
    assert gw.check_exec(str(_exe(base / "tool"))) is True


def test_default_gateway_checks_every_directory_up_to_the_root(tmp_path: Path) -> None:
    target = _exe(_dir(tmp_path / "bin") / "tool")
    # Every ancestor from "/" is checked. This is true only when the whole chain is private; the
    # call must at least not raise, and a world-writable ancestor must make it False.
    wide = _dir(tmp_path / "wide", 0o777)
    inner = _exe(_dir(wide / "sub") / "tool")
    assert Gateway().check_exec(str(inner)) is False
    assert isinstance(Gateway().check_exec(str(target)), bool)


@pytest.mark.parametrize("mode", [0o720, 0o702, 0o722, 0o770, 0o777])
def test_file_writable_by_group_or_world_fails(gw: Gateway, base: Path, mode: int) -> None:
    assert gw.check_exec(str(_exe(base / "tool", mode))) is False


@pytest.mark.parametrize("mode", [0o4700, 0o2700, 0o6700])
def test_setuid_or_setgid_file_fails(gw: Gateway, base: Path, mode: int) -> None:
    assert gw.check_exec(str(_exe(base / "tool", mode))) is False


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_directory_writable_by_group_or_world_fails(gw: Gateway, base: Path, mode: int) -> None:
    sub = _dir(base / "sub", mode)
    target = _exe(sub / "tool")
    assert gw.check_exec(str(target)) is False
    sub.chmod(0o700)


def test_a_wide_directory_further_up_fails(gw: Gateway, base: Path) -> None:
    wide = _dir(base / "wide", 0o775)
    target = _exe(_dir(wide / "deep" / "er") / "tool")
    assert gw.check_exec(str(target)) is False
    wide.chmod(0o700)


def test_symlink_is_resolved_and_the_target_is_checked(gw: Gateway, base: Path) -> None:
    wide = _dir(base / "wide", 0o777)
    real = _exe(wide / "real")
    link = base / "link"
    link.symlink_to(real)
    assert gw.check_exec(str(link)) is False  # the resolved directory is world-writable
    wide.chmod(0o700)
    good = _exe(base / "good")
    (base / "good-link").symlink_to(good)
    assert gw.check_exec(str(base / "good-link")) is True


def test_owner_must_be_the_user_or_root(
    gw: Gateway, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _exe(base / "tool")
    real = os.geteuid()
    monkeypatch.setattr(gateway_module.os, "geteuid", lambda: real + 1)
    assert gw.check_exec(str(target)) is False


def test_missing_directory_and_non_regular_files_fail(gw: Gateway, base: Path) -> None:
    assert gw.check_exec(str(base / "absent")) is False
    assert gw.check_exec(str(_dir(base / "adir"))) is False
    assert gw.check_exec("") is False
    assert gw.check_exec("/x\x00y") is False


def test_a_symlink_loop_is_false_not_an_error(gw: Gateway, base: Path) -> None:
    (base / "a").symlink_to(base / "b")
    (base / "b").symlink_to(base / "a")
    assert gw.check_exec(str(base / "a")) is False


def test_the_check_never_executes_the_file(gw: Gateway, base: Path) -> None:
    marker = base / "ran"
    target = base / "tool"
    target.write_text(f"#!/bin/sh\ntouch {marker}\n")
    target.chmod(0o700)
    assert gw.check_exec(str(target)) is True
    assert not marker.exists()


# --- check_env_file --------------------------------------------------------------------------


def _env_file(path: Path, mode: int = 0o600) -> Path:
    path.write_text("NAME=value\n")
    path.chmod(mode)
    return path


def test_env_file_private_regular_passes(gw: Gateway, base: Path) -> None:
    assert gw.check_env_file(str(_env_file(base / "env"))) is True


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o666, 0o700, 0o400, 0o644])
def test_env_file_with_any_mode_but_0600_fails(gw: Gateway, base: Path, mode: int) -> None:
    assert gw.check_env_file(str(_env_file(base / "env", mode))) is False


def test_env_file_symlink_fails_even_to_a_good_file(gw: Gateway, base: Path) -> None:
    real = _env_file(base / "real")
    (base / "link").symlink_to(real)
    assert gw.check_env_file(str(base / "link")) is False


def test_env_file_missing_directory_or_wrong_type_fails(gw: Gateway, base: Path) -> None:
    assert gw.check_env_file(str(base / "absent")) is False
    assert gw.check_env_file(str(_dir(base / "adir"))) is False
    assert gw.check_env_file("relative") is False
    assert gw.check_env_file("/a\x00b") is False


def test_env_file_in_a_wide_directory_fails(gw: Gateway, base: Path) -> None:
    wide = _dir(base / "wide", 0o777)
    target = _env_file(wide / "env")
    assert gw.check_env_file(str(target)) is False
    wide.chmod(0o700)


def test_env_file_owner_must_be_the_user(
    gw: Gateway, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _env_file(base / "env")
    real = os.geteuid()
    monkeypatch.setattr(gateway_module.os, "geteuid", lambda: real + 1)
    assert gw.check_env_file(str(target)) is False


def test_env_file_is_never_read(gw: Gateway, base: Path) -> None:
    target = _env_file(base / "env")
    before = target.stat().st_atime_ns
    assert gw.check_env_file(str(target)) is True
    assert target.stat().st_atime_ns == before
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


# --- read_state ------------------------------------------------------------------------------


def _state_dir(home: Path) -> Path:
    return _dir(home / ".local" / "state" / "notice-route")


def _db(home: Path) -> sqlite3.Connection:
    path = _state_dir(home) / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value REAL);
        CREATE TABLE deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, destination TEXT);
        CREATE TABLE queue (id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id INTEGER);
        CREATE TABLE surfaced (
            id INTEGER PRIMARY KEY AUTOINCREMENT, destination TEXT, code TEXT, period TEXT,
            delivery_id INTEGER, processed INTEGER DEFAULT 0
        );
        """
    )
    return conn


def _state_gateway(home: Path) -> Gateway:
    return Gateway(passwd_home=lambda: str(home), clock=FakeClock(wall=NOW))


def test_no_state_directory_is_empty(home: Path) -> None:
    view = _state_gateway(home).read_state()
    assert view.exists is False
    assert view.flush_last is None
    assert view.flush_env_missing is None
    assert view.surfaced_today == {}
    assert view.queue_by_destination == {}
    assert not (home / ".local").exists()  # nothing was created


def test_state_directory_without_a_database_is_empty(home: Path) -> None:
    _state_dir(home)
    view = _state_gateway(home).read_state()
    assert view.exists is False
    assert not (home / ".local" / "state" / "notice-route" / "state.db").exists()


def test_reads_meta_surfaced_today_and_queue_counts(home: Path) -> None:
    conn = _db(home)
    conn.execute("INSERT INTO meta VALUES ('flush_last', ?)", (NOW - 120,))
    conn.execute("INSERT INTO meta VALUES ('flush_env_missing', ?)", (NOW - 120,))
    conn.executemany(
        "INSERT INTO surfaced (destination, code, period) VALUES (?, ?, ?)",
        [
            ("phone", "killed", "2023-11-14"),
            ("phone", "rate_capped", "2023-11-14T22"),
            ("phone", "killed", "2023-11-13"),  # yesterday
            ("chat", "overflow", "2023-11-14T03"),
            ("phone", "BAD CODE", "2023-11-14"),  # not a notifier code shape
        ],
    )
    conn.executemany(
        "INSERT INTO deliveries (destination) VALUES (?)", [("gone",), ("gone",), ("phone",)]
    )
    conn.executemany("INSERT INTO queue (delivery_id) VALUES (?)", [(1,), (2,), (3,)])
    conn.commit()
    conn.close()
    view = _state_gateway(home).read_state()
    assert view.exists is True
    assert view.flush_last == NOW - 120
    assert view.flush_env_missing == NOW - 120
    assert view.surfaced_today == {"phone": ("killed", "rate_capped"), "chat": ("overflow",)}
    assert view.queue_by_destination == {"gone": 2, "phone": 1}


def test_null_meta_values_are_none(home: Path) -> None:
    conn = _db(home)
    conn.execute("INSERT INTO meta VALUES ('flush_last', NULL)")
    conn.commit()
    conn.close()
    view = _state_gateway(home).read_state()
    assert view.flush_last is None
    assert view.flush_env_missing is None


def test_the_database_is_opened_read_only(home: Path) -> None:
    conn = _db(home)
    conn.commit()
    conn.close()
    db = home / ".local" / "state" / "notice-route" / "state.db"
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    _state_gateway(home).read_state()
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    assert sorted(p.name for p in db.parent.iterdir()) == ["state.db"]


def test_a_corrupt_database_is_a_state_error(home: Path) -> None:
    _state_dir(home)
    (home / ".local" / "state" / "notice-route" / "state.db").write_bytes(b"not a database" * 100)
    with pytest.raises(StateError):
        _state_gateway(home).read_state()


def test_a_database_with_other_tables_is_a_state_error(home: Path) -> None:
    conn = sqlite3.connect(_state_dir(home) / "state.db")
    conn.execute("CREATE TABLE other (x)")
    conn.commit()
    conn.close()
    with pytest.raises(StateError):
        _state_gateway(home).read_state()


def test_a_symlinked_database_is_a_state_error(home: Path, tmp_path: Path) -> None:
    real = _dir(tmp_path / "elsewhere") / "state.db"
    sqlite3.connect(real).close()
    (_state_dir(home) / "state.db").symlink_to(real)
    with pytest.raises(StateError):
        _state_gateway(home).read_state()


def test_a_directory_as_state_db_is_a_state_error(home: Path) -> None:
    (_state_dir(home) / "state.db").mkdir()
    with pytest.raises(StateError):
        _state_gateway(home).read_state()


def test_a_wide_state_directory_is_a_state_error(home: Path) -> None:
    _db(home).close()
    _state_dir(home).chmod(0o777)
    try:
        with pytest.raises(StateError):
            _state_gateway(home).read_state()
    finally:
        _state_dir(home).chmod(0o700)
