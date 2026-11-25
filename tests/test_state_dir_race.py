"""Concurrent first runs against a new state directory (#63).

Cause: a side file (``state.db-journal``, ``-wal``) that a sibling process is deleting can be seen
by ``lstat`` with ``st_nlink == 0`` for a moment (the kernel drops the link count before it drops
the name), and ``Gateway._check_side_file`` took that for a hard link."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from notice_route import gateway as gateway_module
from notice_route.gateway import Gateway, UnsafePathError
from notice_route.store import open_store

ROUNDS = 12
PROCS = 8

CHILD = """
import os, sys, time
from notice_route.gateway import Gateway
from notice_route.store import StoreError, open_store
root, rounds = sys.argv[1], int(sys.argv[2])
failures = []
for k in range(rounds):
    home = f"{root}/h{k}"
    while not os.path.exists(f"{root}/go{k}"):
        time.sleep(0.0005)
    try:
        open_store(Gateway(passwd_home=lambda: home)).close()
    except StoreError as exc:
        failures.append(f"round {k}: {exc}")
print("\\n".join(failures))
sys.exit(1 if failures else 0)
"""


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


def _state_dir(home: Path) -> Path:
    return home / ".local/state/notice-route"


def _gateway(home: Path) -> Gateway:
    return Gateway(passwd_home=lambda: str(home))


def _with_link_count(monkeypatch: pytest.MonkeyPatch, name: str, nlink: int) -> None:
    real_stat = os.stat

    def fake_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        info = real_stat(path, *args, **kwargs)
        if os.fsdecode(path) != name:
            return info
        fields = list(info)
        fields[stat.ST_NLINK] = nlink
        return os.stat_result(fields)

    monkeypatch.setattr(gateway_module.os, "stat", fake_stat)


@pytest.mark.parametrize("name", ["state.db-journal", "state.db-wal", "state.db-shm"])
def test_a_side_file_being_deleted_is_absent_not_unsafe(
    home: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    open_store(_gateway(home)).close()
    (_state_dir(home) / name).write_bytes(b"")
    (_state_dir(home) / name).chmod(0o600)
    _with_link_count(monkeypatch, name, 0)  # what lstat shows while a sibling unlinks it
    files = _gateway(home).prepare_state_db(create=True)
    assert files is not None
    assert files.wal_present is False


@pytest.mark.parametrize("name", ["state.db-journal", "state.db-wal", "state.db-shm"])
def test_a_hard_linked_side_file_is_still_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    open_store(_gateway(home)).close()
    (_state_dir(home) / name).write_bytes(b"")
    (_state_dir(home) / name).chmod(0o600)
    _with_link_count(monkeypatch, name, 2)
    with pytest.raises(UnsafePathError) as caught:
        _gateway(home).prepare_state_db(create=True)
    assert caught.value.reason == "not_regular"


def test_a_state_database_with_no_link_left_is_still_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_store(_gateway(home)).close()
    real_fstat = os.fstat

    def fake_fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode):
            fields = list(info)
            fields[stat.ST_NLINK] = 0
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(gateway_module.os, "fstat", fake_fstat)
    with pytest.raises(UnsafePathError) as caught:
        _gateway(home).prepare_state_db(create=True)
    assert caught.value.reason == "not_regular"


def test_eight_processes_creating_the_state_directory_together_all_get_a_store(
    tmp_path: Path, children: list[subprocess.Popen[str]]
) -> None:
    root = tmp_path / "rounds"
    root.mkdir()
    for k in range(ROUNDS):
        (root / f"h{k}").mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "XDG_CONFIG_HOME": str(root / "xdg")}
    for _ in range(PROCS):
        children.append(
            subprocess.Popen(
                [sys.executable, "-I", "-c", CHILD, str(root), str(ROUNDS)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    time.sleep(0.5)  # let every child reach its first barrier
    for k in range(ROUNDS):
        (root / f"go{k}").touch()
        time.sleep(0.01)
    for p in children:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, out + err
    for k in range(ROUNDS):
        assert stat.S_IMODE((_state_dir(root / f"h{k}") / "state.db").stat().st_mode) == 0o600
