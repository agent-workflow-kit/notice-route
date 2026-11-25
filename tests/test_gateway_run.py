"""``Gateway.run``: own group, bounded reader, monotonic deadline, group TERM then KILL.

Every process comes from the fake notifier (``tests/fakebin``), which records PIDs with start
times; the ``make_notifier`` fixture signals exactly those at teardown. Deadline tests use a
``FakeClock`` and a ticker thread that advances only the monotonic clock, so nothing sleeps the
real 5 s grace.
"""

from __future__ import annotations

import math
import os
import signal
import sys
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from notice_route.gateway import (
    FINAL_READ,
    STDERR_LIMIT,
    STDOUT_LIMIT,
    TERM_GRACE,
    FakeClock,
    Gateway,
    RunResult,
)
from tests.fakes import CANONICAL, Notifier, alive, scrubbed_env

Make = Callable[..., Notifier]
PAYLOAD = '{"message": "hello"}'


def call(
    n: Notifier,
    home: Path,
    gw: Gateway | None = None,
    *,
    stdin: str | bytes | None = PAYLOAD,
    timeout: float = 15.0,
    env: Mapping[str, str] | None = None,
) -> RunResult:
    gateway = gw or Gateway()
    return gateway.run(
        [*n.cmd, *CANONICAL],
        stdin=stdin,
        timeout=timeout,
        env=env if env is not None else scrubbed_env(home),
    )


def fake_alive(n: Notifier) -> bool:
    rec = n.events("invoke")[-1]
    return alive(rec["pid"], rec["start"])


def wait_dead(pid: int, start: str, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not alive(pid, start):
            return True
        time.sleep(0.02)
    return False


@contextmanager
def ticker(clock: FakeClock, step: float, every: float = 0.01) -> Iterator[threading.Event]:
    """Advance only the monotonic clock by ``step`` each ``every`` real seconds, once enabled."""
    go, stop = threading.Event(), threading.Event()

    def work() -> None:
        while not stop.is_set():
            if go.is_set():
                clock.advance_monotonic(step)
            time.sleep(every)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    try:
        yield go
    finally:
        stop.set()
        thread.join(5)


# spawn shape --------------------------------------------------------------------------------


def test_child_gets_its_own_session_and_group_cwd_root_and_only_three_descriptors(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier()
    result = call(n, home)
    assert result.status == "exited" and result.returncode == 0
    rec = n.events("invoke")[0]
    assert rec["pid"] == rec["pgid"] == rec["sid"] == result.pid
    assert rec["sid"] != os.getsid(0)
    assert rec["cwd"] == "/"
    assert sorted(rec["open_fds"])[:3] == [0, 1, 2]
    assert len(rec["open_fds"]) == 3


def test_environment_is_exactly_the_one_given(make_notifier: Make, home: Path) -> None:
    n = make_notifier()
    env = {"PATH": "/usr/bin:/bin", "ONLY_THIS": "1", "HOME": str(home)}
    os.environ["LEAK_CANARY"] = "x"
    try:
        call(n, home, env=env)
    finally:
        del os.environ["LEAK_CANARY"]
    seen = n.events("invoke")[0]["env"]
    assert "LEAK_CANARY" not in seen
    assert seen["ONLY_THIS"] == "1" and seen["HOME"] == str(home)
    assert set(seen) - set(env) <= {"LC_CTYPE"}  # the interpreter may add its coerced locale


def test_no_shell_argv_is_passed_verbatim(home: Path) -> None:
    hostile = "a; echo $HOME `id` $(id) 'q' \"d\" *"
    code = "import sys; sys.stdout.write(sys.argv[1])"
    result = Gateway().run(
        [sys.executable, "-I", "-c", code, hostile], env=scrubbed_env(home), timeout=15
    )
    assert result.stdout == hostile.encode()


def test_stdin_arrives_intact_including_unicode(make_notifier: Make, home: Path) -> None:
    n = make_notifier(validate_input=False)
    text = '{"message": "héllo ☃ \U0001f600"}'
    result = call(n, home, stdin=text)
    assert result.status == "exited"
    assert n.events("stdin")[0]["text"] == text


def test_stdin_may_be_bytes_or_absent(make_notifier: Make, home: Path) -> None:
    n = make_notifier(validate_input=False)
    assert call(n, home, stdin=b"\xff\xfe raw").status == "exited"
    assert call(n, home, stdin=None).status == "exited"


def test_a_program_that_cannot_start_is_a_distinct_status(home: Path) -> None:
    result = Gateway().run(["/nonexistent/prog"], env=scrubbed_env(home), timeout=5)
    assert result.status == "spawn_failed"
    assert result.pid is None and result.error and "FileNotFoundError" in result.error


@pytest.mark.parametrize("argv", [[], [""], ["a\x00b"]])
def test_hostile_argv_is_refused_or_reported(argv: list[str], home: Path) -> None:
    gw = Gateway()
    if argv == [""]:
        assert gw.run(argv, env=scrubbed_env(home), timeout=5).status == "spawn_failed"
    else:
        with pytest.raises(ValueError):
            gw.run(argv, env=scrubbed_env(home), timeout=5)


@pytest.mark.parametrize("timeout", [0.0, -1.0, math.nan, math.inf])
def test_a_bad_timeout_is_refused_before_anything_starts(
    make_notifier: Make, home: Path, timeout: float
) -> None:
    n = make_notifier()
    with pytest.raises(ValueError):
        call(n, home, timeout=timeout)
    assert n.records() == []


def test_a_non_string_env_value_is_refused(make_notifier: Make, home: Path) -> None:
    n = make_notifier()
    with pytest.raises(ValueError):
        call(n, home, env={"PATH": 1})  # type: ignore[dict-item]
    assert n.records() == []


# distinct outcomes --------------------------------------------------------------------------


def test_exit_status_is_reported_with_output(make_notifier: Make, home: Path) -> None:
    n = make_notifier(exit=7, stdout="out-text", stderr="err-text")
    result = call(n, home)
    assert (result.status, result.returncode, result.signal) == ("exited", 7, None)
    assert b"out-text" in result.stdout and b"err-text" in result.stderr
    assert result.overflow is None


def test_signal_death_is_reported_as_signaled_not_exited(make_notifier: Make, home: Path) -> None:
    n = make_notifier(die_by_signal="KILL")
    result = call(n, home)
    assert (result.status, result.returncode, result.signal) == ("signaled", None, signal.SIGKILL)


def test_timeout_is_not_an_exit_or_a_signal_death(make_notifier: Make, home: Path) -> None:
    n = make_notifier(hang=True)
    result = call(n, home, timeout=0.3)
    assert result.status == "timeout"
    assert result.signal == signal.SIGTERM and result.returncode is None
    assert not fake_alive(n)


# group kill ---------------------------------------------------------------------------------


def test_group_kill_takes_the_grandchild_and_the_call_does_not_hang(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(hang=True, grandchild={"hold_stdout": True, "hold_stderr": True})
    began = time.monotonic()
    result = call(n, home, timeout=0.5)
    assert time.monotonic() - began < 4
    assert result.status == "timeout"
    g = n.events("grandchild")[0]
    assert g["pgid"] == result.pid  # same group as the child
    assert wait_dead(g["pid"], g["start"])


def test_a_grandchild_left_in_the_group_after_a_normal_exit_is_killed(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(grandchild={"hold_stdout": True, "lifetime": 60})
    began = time.monotonic()
    result = call(n, home, timeout=15)
    assert result.status == "exited" and result.returncode == 0
    assert time.monotonic() - began < FINAL_READ + 2
    assert b'"outcome":"sent"' in result.stdout
    g = n.events("grandchild")[0]
    assert wait_dead(g["pid"], g["start"])


def test_a_setsid_descendant_holding_stdout_cannot_hold_the_call_past_the_final_read(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(grandchild={"setsid": True, "hold_stdout": True, "lifetime": 30})
    began = time.monotonic()
    result = call(n, home, timeout=15)
    took = time.monotonic() - began
    assert result.status == "exited" and result.returncode == 0
    assert FINAL_READ - 0.1 <= took < FINAL_READ + 2
    assert b'"outcome":"sent"' in result.stdout
    g = n.events("grandchild")[0]
    assert alive(g["pid"], g["start"])  # outside the group: untouched, as designed
    n.kill_tracked()


def test_a_double_forked_setsid_descendant_holding_both_pipes_is_released(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(
        grandchild={
            "double_fork": True,
            "setsid": True,
            "hold_stdout": True,
            "hold_stderr": True,
            "lifetime": 30,
        }
    )
    began = time.monotonic()
    result = call(n, home, timeout=15)
    assert time.monotonic() - began < FINAL_READ + 2
    assert result.status == "exited"
    n.kill_tracked()


# bounded reader -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "limit", "stream"),
    [("stdout_bytes", STDOUT_LIMIT, "stdout"), ("stderr_bytes", STDERR_LIMIT, "stderr")],
)
def test_output_at_the_cap_is_accepted_and_one_byte_more_overflows(
    make_notifier: Make, home: Path, key: str, limit: int, stream: str
) -> None:
    ok = call(make_notifier(**{key: limit}), home)
    assert ok.status == "exited" and ok.overflow is None
    assert len(getattr(ok, stream)) >= limit
    n = make_notifier(**{key: limit + 1})
    over = call(n, home)
    assert over.status == "overflow" and over.overflow == stream
    assert len(getattr(over, stream)) == limit
    assert over.returncode is None


def test_limits_are_the_documented_ones() -> None:
    assert (STDOUT_LIMIT, STDERR_LIMIT, TERM_GRACE, FINAL_READ) == (65536, 16384, 5.0, 1.0)


def test_a_200_mib_writer_is_cut_off_with_bounded_memory(home: Path) -> None:
    # The fake notifier refuses more than 50 MB; a plain writer shows the 200 MiB case. It
    # records its own pid and start time first, so the test can prove the gateway killed it.
    pidfile = home / "writer.pid"
    code = (
        "import os, sys\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "chunk = b'a' * 65536\n"
        "for _ in range(200 * 16):\n"
        "    sys.stdout.buffer.write(chunk)\n"
        "    sys.stdout.buffer.flush()\n"
    )
    tracemalloc.start()
    try:
        began = time.monotonic()
        result = Gateway().run(
            [sys.executable, "-I", "-c", code], env=scrubbed_env(home), timeout=30
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.status == "overflow" and result.overflow == "stdout"
    assert len(result.stdout) == STDOUT_LIMIT
    assert peak < 4 * 2**20
    assert time.monotonic() - began < 5
    assert result.pid is not None and result.pid == int(pidfile.read_text())
    assert not alive(result.pid, None)


def test_an_overflow_kills_the_whole_group_at_once(make_notifier: Make, home: Path) -> None:
    n = make_notifier(stderr_bytes=1_000_000, grandchild={"hold_stdout": True, "lifetime": 60})
    result = call(n, home)
    assert result.status == "overflow" and result.overflow == "stderr"
    g = n.events("grandchild")
    if g:  # the grandchild may not have been spawned before the writer was cut off
        assert wait_dead(g[0]["pid"], g[0]["start"])
    assert not fake_alive(n)


# deadline on the monotonic clock ------------------------------------------------------------


def test_a_wall_clock_jump_never_moves_the_deadline(make_notifier: Make, home: Path) -> None:
    n = make_notifier(hang=True)
    clock = FakeClock()
    box: list[RunResult] = []
    worker = threading.Thread(
        target=lambda: box.append(call(n, home, Gateway(clock=clock), timeout=10)), daemon=True
    )
    worker.start()
    try:
        n.wait_for("hang_started")
        for jump in (10**7, -2 * 10**7, 5 * 10**8):  # forward, back, far forward
            clock.jump_wall(jump)
            time.sleep(0.15)
            assert box == [] and fake_alive(n)
        clock.advance_monotonic(9.9)  # still inside the 10 s deadline
        time.sleep(0.15)
        assert box == [] and fake_alive(n)
        clock.advance_monotonic(0.2)  # now past it
        worker.join(10)
    finally:
        n.kill_tracked()
    assert box and box[0].status == "timeout" and box[0].signal == signal.SIGTERM


def test_the_deadline_is_counted_from_the_process_start(make_notifier: Make, home: Path) -> None:
    n = make_notifier(hang=True)
    clock = FakeClock()
    clock.advance_monotonic(500.0)  # a long-running router: the deadline is relative to the start
    with ticker(clock, 0.5) as go:
        go.set()
        result = call(n, home, Gateway(clock=clock), timeout=3)
    assert result.status == "timeout"
    assert 3 <= result.elapsed < 10


def test_a_child_that_ignores_sigterm_is_killed_only_after_the_grace(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(hang=True, signals={"TERM": "ignore"})
    clock = FakeClock()
    box: list[RunResult] = []
    worker = threading.Thread(
        target=lambda: box.append(call(n, home, Gateway(clock=clock), timeout=10)), daemon=True
    )
    worker.start()
    try:
        n.wait_for("hang_started")
        clock.advance_monotonic(10.0)  # the deadline: TERM goes out
        n.wait_for("signal")
        assert n.events("signal")[0]["name"] == "TERM"
        clock.advance_monotonic(TERM_GRACE - 0.5)
        time.sleep(0.2)
        assert box == [] and fake_alive(n)  # still inside the grace
        clock.advance_monotonic(0.6)
        worker.join(10)
    finally:
        n.kill_tracked()
    assert box and box[0].status == "timeout" and box[0].signal == signal.SIGKILL
    assert not fake_alive(n)


def test_a_child_that_obeys_term_is_not_waited_on_for_the_grace(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(hang=True)
    began = time.monotonic()
    result = call(n, home, timeout=0.3)
    assert result.status == "timeout" and time.monotonic() - began < TERM_GRACE


def test_the_final_read_is_bounded_by_the_monotonic_clock(make_notifier: Make, home: Path) -> None:
    n = make_notifier(grandchild={"setsid": True, "hold_stdout": True, "lifetime": 30})
    clock = FakeClock()
    box: list[RunResult] = []
    worker = threading.Thread(
        target=lambda: box.append(call(n, home, Gateway(clock=clock), timeout=100)), daemon=True
    )
    worker.start()
    try:
        n.wait_for("grandchild")
        time.sleep(0.5)  # the fake clock is frozen: the final read cannot end by itself
        assert box == []
        clock.jump_wall(10**6)  # wall time does not end it either
        time.sleep(0.2)
        assert box == []
        clock.advance_monotonic(FINAL_READ + 0.1)
        worker.join(10)
    finally:
        n.kill_tracked()
    assert box and box[0].status == "exited"


# stdin --------------------------------------------------------------------------------------


def test_a_child_that_closes_stdin_early_does_not_raise_epipe(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(close_stdin=True, linger=0.3, validate_input=False)
    result = call(n, home, stdin="x" * 5_000_000)
    assert result.status == "exited" and result.returncode == 0


def test_epipe_after_the_child_is_gone_is_ignored(home: Path) -> None:
    code = "import sys; sys.stdin.close(); sys.exit(3)"
    result = Gateway().run(
        [sys.executable, "-I", "-c", code], env=scrubbed_env(home), timeout=15, stdin=b"y" * 10**7
    )
    assert (result.status, result.returncode) == ("exited", 3)


def test_the_stdin_write_counts_against_the_deadline(make_notifier: Make, home: Path) -> None:
    n = make_notifier(hang=True, hang_before_read=True, validate_input=False)
    clock = FakeClock()
    with ticker(clock, 1.0) as go:
        go.set()
        result = call(n, home, Gateway(clock=clock), stdin="z" * 5_000_000, timeout=5)
    assert result.status == "timeout"  # the child never read: the write could not finish


# cleanup on failure -------------------------------------------------------------------------


class _ExplodingClock(FakeClock):
    def __init__(self, calls_before: int) -> None:
        super().__init__()
        self.left = calls_before

    def monotonic(self) -> float:
        if self.left <= 0:
            raise RuntimeError("clock failed")
        self.left -= 1
        return super().monotonic()


def test_an_exception_inside_the_call_kills_the_group_and_propagates(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(hang=True, grandchild={"hold_stdout": True})
    gw = Gateway(clock=_ExplodingClock(calls_before=40))
    with pytest.raises(RuntimeError, match="clock failed"):
        call(n, home, gw, timeout=100)
    rec = n.events("invoke")[0]
    assert wait_dead(rec["pid"], rec["start"])
    g = n.events("grandchild")
    if g:
        assert wait_dead(g[0]["pid"], g[0]["start"])


def test_only_the_created_group_is_signaled_and_never_after_the_leader_is_reaped(
    make_notifier: Make, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notice_route import gateway as gw_mod

    sent: list[tuple[int, int]] = []
    real = os.killpg

    def spy(pgid: int, sig: int) -> None:
        sent.append((pgid, sig))
        real(pgid, sig)

    monkeypatch.setattr(gw_mod.os, "killpg", spy)
    n = make_notifier(hang=True)
    result = call(n, home, timeout=0.3)
    assert result.pid is not None
    assert {pgid for pgid, _ in sent} == {result.pid}
    assert [sig for _, sig in sent][0] == signal.SIGTERM
    assert all(sig in (signal.SIGTERM, signal.SIGKILL) for _, sig in sent)
    with pytest.raises(ProcessLookupError):
        real(result.pid, 0)  # the group is gone and the pid is free: the gateway left it alone
