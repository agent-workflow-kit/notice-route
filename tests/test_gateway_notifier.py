"""Gateway support for the notifier call (#19): ``resolve_exec``, ``clean_path`` and the typed
errors of ``Gateway.run``. Nothing here starts a notifier."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from notice_route import gateway as gw_mod
from notice_route.gateway import (
    ChildLostError,
    Gateway,
    SigchldIgnoredError,
)
from tests.fakes import scrubbed_env


def _dir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    return path


def _exe(path: Path, mode: int = 0o700) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(mode)
    return path


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return _dir(tmp_path / "t")


@pytest.fixture
def gw(base: Path) -> Gateway:
    return Gateway(exec_check_from=str(base))


# --- resolve_exec ----------------------------------------------------------------------------


def test_resolve_exec_returns_the_resolved_path_of_a_passing_file(gw: Gateway, base: Path) -> None:
    real = _exe(_dir(base / "real") / "tool")
    link = base / "link"
    link.symlink_to(real)
    assert gw.resolve_exec(str(link)) == str(real.resolve())


def test_resolve_exec_agrees_with_check_exec(gw: Gateway, base: Path) -> None:
    wide = _dir(base / "wide", 0o777)
    real = _exe(wide / "real")
    link = base / "link"
    link.symlink_to(real)
    assert gw.check_exec(str(link)) is False
    assert gw.resolve_exec(str(link)) is None
    wide.chmod(0o700)


@pytest.mark.parametrize("mode", [0o720, 0o702, 0o4700, 0o2700])
def test_resolve_exec_refuses_a_wide_or_setid_file(gw: Gateway, base: Path, mode: int) -> None:
    assert gw.resolve_exec(str(_exe(base / "tool", mode))) is None


@pytest.mark.parametrize("name", ["missing", "", "rel/tool", "a\x00b"])
def test_resolve_exec_refuses_a_missing_relative_or_nul_path(gw: Gateway, name: str) -> None:
    assert gw.resolve_exec(name) is None


def test_resolve_exec_refuses_a_directory_and_a_dangling_link(gw: Gateway, base: Path) -> None:
    (base / "dangling").symlink_to(base / "nowhere")
    assert gw.resolve_exec(str(_dir(base / "d"))) is None
    assert gw.resolve_exec(str(base / "dangling")) is None


def test_resolve_exec_refuses_a_symlink_loop(gw: Gateway, base: Path) -> None:
    (base / "a").symlink_to(base / "b")
    (base / "b").symlink_to(base / "a")
    assert gw.resolve_exec(str(base / "a")) is None


# --- clean_path ------------------------------------------------------------------------------


def test_clean_path_keeps_private_directories_in_order(gw: Gateway, base: Path) -> None:
    a, b = _dir(base / "a"), _dir(base / "b", 0o755)
    assert gw.clean_path(f"{a}:{b}") == f"{a}:{b}"


def test_clean_path_drops_empty_relative_and_missing_entries(gw: Gateway, base: Path) -> None:
    a = _dir(base / "a")
    value = f":{a}::bin:./x:../y:{base}/missing:"
    assert gw.clean_path(value) == str(a)


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777, 0o1777, 0o722])
def test_clean_path_drops_a_directory_writable_by_others(
    gw: Gateway, base: Path, mode: int
) -> None:
    wide = _dir(base / "wide", mode)
    a = _dir(base / "a")
    assert gw.clean_path(f"{wide}:{a}") == str(a)
    wide.chmod(0o700)


def test_clean_path_drops_a_file_and_a_directory_of_another_owner(
    gw: Gateway, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _dir(base / "a")
    f = _exe(base / "file")
    assert gw.clean_path(f"{f}:{a}") == str(a)
    other = os.geteuid() + 12345
    monkeypatch.setattr(gw_mod.os, "geteuid", lambda: other)
    assert gw.clean_path(str(a)) == ""


def test_clean_path_drops_a_missing_entry_alone(gw: Gateway, base: Path) -> None:
    assert gw.clean_path(f"{base}/missing") == ""


def test_clean_path_drops_a_non_directory_entry_alone(gw: Gateway, base: Path) -> None:
    assert gw.clean_path(str(_exe(base / "file", 0o600))) == ""


def test_clean_path_drops_a_foreign_owned_entry_but_not_a_foreign_owned_ancestor(
    gw: Gateway, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _dir(base / "a")
    real_stat = os.stat

    def foreign(path: Any, *args: Any, **kw: Any) -> Any:
        info = real_stat(path, *args, **kw)
        owner = 54321 if str(path) == str(a) else info.st_uid
        return os.stat_result((*tuple(info)[:4], owner, *tuple(info)[5:]))

    monkeypatch.setattr(gw_mod.os, "stat", foreign)
    assert gw.clean_path(str(a)) == ""
    inner = _dir(a / "inner")
    assert gw.clean_path(str(inner)) == str(inner)  # the entry is judged, its ancestors are not


def test_clean_path_follows_a_symlink_to_a_wide_directory(gw: Gateway, base: Path) -> None:
    wide = _dir(base / "wide", 0o777)
    link = base / "link"
    link.symlink_to(wide)
    assert gw.clean_path(str(link)) == ""
    wide.chmod(0o700)


def test_clean_path_of_nothing_is_empty(gw: Gateway) -> None:
    assert gw.clean_path("") == ""
    assert gw.clean_path(":::") == ""


def test_clean_path_handles_a_nul_and_a_huge_value(gw: Gateway, base: Path) -> None:
    a = _dir(base / "a")
    assert gw.clean_path(f"a\x00b:{a}") == str(a)
    assert gw.clean_path(":" * 1_000_000 + str(a)) == str(a)


# --- the typed errors of run -----------------------------------------------------------------


def test_an_ignored_sigchld_is_a_typed_error_and_still_a_runtime_error(home: Path) -> None:
    old = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        with pytest.raises(SigchldIgnoredError) as caught:
            Gateway().run([sys.executable, "-I", "-c", "pass"], env=scrubbed_env(home), timeout=5)
    finally:
        signal.signal(signal.SIGCHLD, old)
    assert isinstance(caught.value, RuntimeError)
    assert not isinstance(caught.value, ChildLostError)


def test_a_child_that_cannot_be_reaped_is_a_typed_error(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[subprocess.Popen[bytes]] = []
    real_init = subprocess.Popen.__init__

    def spy(self: subprocess.Popen[bytes], *a: Any, **k: Any) -> None:
        real_init(self, *a, **k)
        made.append(self)

    monkeypatch.setattr(subprocess.Popen, "__init__", spy)
    real_wait = subprocess.Popen.wait

    def never(self: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("x", timeout or 0)

    with monkeypatch.context() as m:
        m.setattr(subprocess.Popen, "wait", never)
        with pytest.raises(ChildLostError):
            Gateway().run([sys.executable, "-I", "-c", "pass"], env=scrubbed_env(home), timeout=15)
    real_wait(made[0], timeout=5)


def test_a_child_reaped_by_someone_else_is_a_typed_error(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gone(*_a: object) -> None:
        raise ChildProcessError

    monkeypatch.setattr(gw_mod.os, "waitid", gone)
    code = "import time; time.sleep(0.2)"
    with pytest.raises(ChildLostError):
        Gateway().run([sys.executable, "-I", "-c", code], env=scrubbed_env(home), timeout=15)
