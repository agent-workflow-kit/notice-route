"""Gateway flock polling: real locks across real processes, plus fake-clock timing."""

from __future__ import annotations

import math
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from io import FileIO
from pathlib import Path

import pytest

from notice_route.gateway import POLL_INTERVAL, DirHandle, FakeClock, Gateway

_HOLDER = textwrap.dedent(
    """
    import fcntl, os, sys
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    sys.stdout.write("held\\n")
    sys.stdout.flush()
    sys.stdin.read()
    """
)


@pytest.fixture
def state(tmp_path: Path) -> Iterator[DirHandle]:
    with Gateway().open_private_dir(tmp_path / "state") as d:
        yield d


@pytest.fixture
def start_holder(tmp_path: Path, home: Path) -> Iterator[Callable[[Path], subprocess.Popen[str]]]:
    """Start a process that holds an exclusive flock; kill exactly the ones started here."""
    started: list[subprocess.Popen[str]] = []

    def start(path: Path) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _HOLDER, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": "/usr/bin"},
        )
        started.append(proc)
        assert proc.stdout is not None
        assert proc.stdout.readline() == "held\n"
        return proc

    yield start
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()


def _lock_file(gw: Gateway, state: DirHandle) -> FileIO:
    return gw.open_file(state, "a.lock", mode="a", create=True)


def test_free_lock_is_taken_at_once(state: DirHandle) -> None:
    gw = Gateway()
    with _lock_file(gw, state) as f:
        assert gw.try_lock(f, 0) is True


def test_second_holder_does_not_get_it_until_unlock(state: DirHandle) -> None:
    gw = Gateway()
    with _lock_file(gw, state) as first, _lock_file(gw, state) as second:
        assert gw.try_lock(first, 0)
        assert gw.try_lock(second, 0) is False
        gw.unlock(first)
        assert gw.try_lock(second, 0) is True


def test_closing_the_file_releases_the_lock(state: DirHandle) -> None:
    gw = Gateway()
    first = _lock_file(gw, state)
    assert gw.try_lock(first, 0)
    first.close()
    with _lock_file(gw, state) as second:
        assert gw.try_lock(second, 0)


def test_lock_held_by_a_killed_process_is_free_at_once(
    state: DirHandle, tmp_path: Path, start_holder: Callable[[Path], subprocess.Popen[str]]
) -> None:
    gw = Gateway()
    path = tmp_path / "state" / "a.lock"
    holder = start_holder(path)
    with _lock_file(gw, state) as f:
        assert gw.try_lock(f, 0) is False
        holder.kill()  # SIGKILL to the one PID this test started
        holder.wait(timeout=10)
        started = gw.monotonic()
        assert gw.try_lock(f, 0) is True
        assert gw.monotonic() - started < 5


def test_lock_is_taken_when_the_holder_lets_go_during_the_wait(
    state: DirHandle, tmp_path: Path, start_holder: Callable[[Path], subprocess.Popen[str]]
) -> None:
    gw = Gateway()
    holder = start_holder(tmp_path / "state" / "a.lock")
    assert holder.stdin is not None
    with _lock_file(gw, state) as f:
        assert gw.try_lock(f, 0) is False
        holder.stdin.close()  # the holder reads EOF and exits, releasing the lock
        started = gw.monotonic()
        assert gw.try_lock(f, 20) is True
        assert gw.monotonic() - started < 10


def test_real_wait_returns_not_acquired_without_overrunning(
    state: DirHandle, tmp_path: Path, start_holder: Callable[[Path], subprocess.Popen[str]]
) -> None:
    gw = Gateway()
    start_holder(tmp_path / "state" / "a.lock")
    with _lock_file(gw, state) as f:
        started = gw.monotonic()
        assert gw.try_lock(f, 0.3) is False
        elapsed = gw.monotonic() - started
    assert 0.3 <= elapsed < 5


def test_fake_clock_wait_polls_every_50ms_and_stops_at_the_deadline(state: DirHandle) -> None:
    clock = FakeClock(wall=5.0, monotonic=100.0)
    gw = Gateway(clock=clock)
    assert POLL_INTERVAL == 0.05
    with _lock_file(gw, state) as holder, _lock_file(gw, state) as waiter:
        assert gw.try_lock(holder, 0)
        assert gw.try_lock(waiter, 2.0) is False
    assert clock.monotonic() == pytest.approx(102.0, abs=1e-9)


def test_wait_shorter_than_one_poll_does_not_sleep_a_full_poll(state: DirHandle) -> None:
    clock = FakeClock(monotonic=0.0)
    gw = Gateway(clock=clock)
    with _lock_file(gw, state) as holder, _lock_file(gw, state) as waiter:
        assert gw.try_lock(holder, 0)
        assert gw.try_lock(waiter, 0.01) is False
    assert clock.monotonic() == pytest.approx(0.01, abs=1e-9)


def test_zero_wait_tries_once_and_never_sleeps(state: DirHandle) -> None:
    clock = FakeClock()
    gw = Gateway(clock=clock)
    with _lock_file(gw, state) as holder, _lock_file(gw, state) as waiter:
        assert gw.try_lock(holder, 0)
        assert gw.try_lock(waiter, 0) is False
    assert clock.monotonic() == 1_000.0


def test_wait_is_measured_on_the_monotonic_clock_not_the_wall_clock(state: DirHandle) -> None:
    class JumpingClock(FakeClock):
        def sleep(self, seconds: float) -> None:
            self.jump_wall(-1e6)  # the wall clock steps back on every poll
            super().sleep(seconds)

    clock = JumpingClock(monotonic=0.0)
    gw = Gateway(clock=clock)
    with _lock_file(gw, state) as holder, _lock_file(gw, state) as waiter:
        assert gw.try_lock(holder, 0)
        assert gw.try_lock(waiter, 1.0) is False
    assert clock.monotonic() == pytest.approx(1.0, abs=1e-9)


def test_lock_released_during_a_fake_wait_is_taken(state: DirHandle) -> None:
    class ReleasingClock(FakeClock):
        def __init__(self, release: Callable[[], None]) -> None:
            super().__init__(monotonic=0.0)
            self.release = release
            self.sleeps = 0

        def sleep(self, seconds: float) -> None:
            self.sleeps += 1
            if self.sleeps == 3:
                self.release()
            super().sleep(seconds)

    gw0 = Gateway()
    with _lock_file(gw0, state) as holder, _lock_file(gw0, state) as waiter:
        assert gw0.try_lock(holder, 0)
        clock = ReleasingClock(lambda: gw0.unlock(holder))
        gw = Gateway(clock=clock)
        assert gw.try_lock(waiter, 5.0) is True
        assert clock.sleeps == 3


@pytest.mark.parametrize("bad", [-1, -0.001, math.nan, math.inf, -math.inf, "1", None, True])
def test_bad_wait_is_refused_before_touching_the_lock(state: DirHandle, bad: object) -> None:
    gw = Gateway()
    with _lock_file(gw, state) as f:
        with pytest.raises(ValueError):
            gw.try_lock(f, bad)  # type: ignore[arg-type]
        assert gw.try_lock(f, 0) is True  # nothing was taken by the refused call


def test_huge_int_wait_is_refused(state: DirHandle) -> None:
    gw = Gateway()
    with _lock_file(gw, state) as f, pytest.raises(ValueError):
        gw.try_lock(f, 10**400)  # type: ignore[arg-type]


def test_unlock_without_holding_is_harmless(state: DirHandle) -> None:
    gw = Gateway()
    with _lock_file(gw, state) as f:
        gw.unlock(f)
        assert gw.try_lock(f, 0)
        gw.unlock(f)
        gw.unlock(f)


def test_lock_file_descriptor_is_not_inherited(state: DirHandle) -> None:
    import os

    gw = Gateway()
    with _lock_file(gw, state) as f:
        assert os.get_inheritable(f.fileno()) is False


def test_fake_clock_sleep_moves_both_clocks_like_real_time() -> None:
    clock = FakeClock(wall=10.0, monotonic=20.0)
    clock.sleep(0.5)
    assert (clock.wall(), clock.monotonic()) == (10.5, 20.5)
    with pytest.raises(ValueError):
        clock.sleep(-1.0)
    with pytest.raises(ValueError):
        clock.sleep(math.nan)
