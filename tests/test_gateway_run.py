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
import subprocess
import sys
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

import pytest

from notice_route import gateway as gw_mod
from notice_route.gateway import (
    FINAL_READ,
    STDERR_LIMIT,
    STDOUT_LIMIT,
    TERM_GRACE,
    FakeClock,
    Gateway,
    RunResult,
    _Pipe,
    _Supervisor,
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


def _spy_popen(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen[bytes]]:
    """Record every Popen the gateway makes (the list is empty when nothing was spawned)."""
    made: list[subprocess.Popen[bytes]] = []

    class Recording(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, *a: object, **k: object) -> None:
            super().__init__(*a, **k)  # type: ignore[call-overload]
            made.append(self)

    monkeypatch.setattr(gw_mod.subprocess, "Popen", Recording)
    return made


SLEEPER = [sys.executable, "-I", "-c", "import time; time.sleep(60)"]


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


@pytest.mark.parametrize("argv", ["echo hi", b"echo hi"])
def test_argv_given_as_one_string_is_refused(argv: object, home: Path) -> None:
    with pytest.raises(ValueError):
        Gateway().run(argv, env=scrubbed_env(home), timeout=5)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "env",
    [{"A=B": "x"}, {"A\x00B": "x"}, {"A": "x\x00y"}],
    ids=["equals-in-key", "nul-in-key", "nul-in-value"],
)
def test_bad_env_names_and_values_are_a_value_error_before_spawn(
    env: dict[str, str], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = _spy_popen(monkeypatch)
    with pytest.raises(ValueError):
        Gateway().run([sys.executable, "-I", "-c", "pass"], env=env, timeout=5)
    assert made == []


@pytest.mark.parametrize("timeout", [None, "5", True, 0.0, -1.0, math.nan, math.inf])
def test_a_bad_timeout_is_refused_before_anything_starts(
    make_notifier: Make, home: Path, timeout: float
) -> None:
    n = make_notifier()
    with pytest.raises(ValueError):
        call(n, home, timeout=timeout)  # type: ignore[arg-type]
    assert n.records() == []


def test_a_non_string_env_value_is_refused(make_notifier: Make, home: Path) -> None:
    n = make_notifier()
    with pytest.raises(ValueError):
        call(n, home, env={"PATH": 1})  # type: ignore[dict-item]
    assert n.records() == []


@pytest.mark.parametrize("stdin", [5, ["x"], {"a": 1}])
def test_a_bad_stdin_type_is_refused_before_anything_starts(
    stdin: object, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = _spy_popen(monkeypatch)
    with pytest.raises(ValueError):
        Gateway().run(
            [sys.executable, "-I", "-c", "pass"],
            env=scrubbed_env(home),
            timeout=5,
            stdin=stdin,  # type: ignore[arg-type]
        )
    assert made == []


@pytest.mark.parametrize("kind", [bytearray, memoryview])
def test_bytearray_and_memoryview_stdin_are_accepted(kind: type, home: Path) -> None:
    code = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
    result = Gateway().run(
        [sys.executable, "-I", "-c", code], env=scrubbed_env(home), timeout=15, stdin=kind(b"abc")
    )
    assert result.stdout == b"abc"


def test_an_ignored_sigchld_is_refused_before_spawning(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = _spy_popen(monkeypatch)
    old = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        with pytest.raises(RuntimeError, match="SIGCHLD"):
            Gateway().run([sys.executable, "-I", "-c", "pass"], env=scrubbed_env(home), timeout=5)
    finally:
        signal.signal(signal.SIGCHLD, old)
    assert made == []


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
    ("fd", "limit", "stream"), [(1, STDOUT_LIMIT, "stdout"), (2, STDERR_LIMIT, "stderr")]
)
def test_output_at_the_cap_is_accepted_and_one_byte_more_overflows(
    home: Path, fd: int, limit: int, stream: str
) -> None:
    def writer(count: int) -> list[str]:
        code = f"import os; os.write({fd}, b'a' * {count})"
        return [sys.executable, "-I", "-c", code]

    ok = Gateway().run(writer(limit), env=scrubbed_env(home), timeout=15)
    assert ok.status == "exited" and ok.overflow is None
    assert getattr(ok, stream) == b"a" * limit
    over = Gateway().run(writer(limit + 1), env=scrubbed_env(home), timeout=15)
    assert over.status == "overflow" and over.overflow == stream
    assert getattr(over, stream) == b"a" * limit
    assert over.returncode is None


def test_the_first_stream_to_cross_its_cap_is_the_one_reported() -> None:
    proc = type("P", (), {"pid": 1})()
    sup = _Supervisor(FakeClock(), proc, b"", 1.0, 0.0)  # type: ignore[arg-type]
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    try:
        out, err = _Pipe("stdout", out_r, 10), _Pipe("stderr", err_r, 10)
        sup.all_pipes, sup.pipes = [out, err], [out, err]
        os.write(err_w, b"x" * 20)
        os.write(out_w, b"x" * 20)
        sup._read(err)  # stderr crosses first although stdout comes first in field order
        sup._read(out)
        assert sup.first_over == "stderr"
    finally:
        for fd in (out_r, out_w, err_r, err_w):
            os.close(fd)


def test_overflow_during_the_term_grace_kills_at_once(home: Path) -> None:
    code = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: os.write(1, b'a' * 200000))\n"
        "while True: time.sleep(0.05)\n"
    )
    began = time.monotonic()
    result = Gateway().run([sys.executable, "-I", "-c", code], env=scrubbed_env(home), timeout=1.5)
    assert time.monotonic() - began < TERM_GRACE - 1
    assert result.status == "timeout"  # the deadline came first; overflow is reported beside it
    assert result.overflow == "stdout" and len(result.stdout) == STDOUT_LIMIT
    assert result.signal == signal.SIGKILL


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
    # The child never reads, so the 5 MB write can never finish: the deadline must fire (TERM
    # reaches the child) while stdin is still pending. Bounded by a thread so a regression fails
    # instead of hanging.
    n = make_notifier(hang=True, hang_before_read=True, validate_input=False)
    box: list[RunResult] = []
    worker = threading.Thread(
        target=lambda: box.append(call(n, home, stdin="z" * 5_000_000, timeout=0.5)), daemon=True
    )
    worker.start()
    worker.join(15)
    n.kill_tracked()
    assert box, "the call did not end: the stdin write is outside the deadline"
    assert box[0].status == "timeout" and box[0].elapsed < TERM_GRACE
    assert n.events("stdin") == []  # nothing was ever read


# cleanup on failure -------------------------------------------------------------------------


class _ExplodingClock(FakeClock):
    """Raises once ``after`` real seconds have passed since it was built."""

    def __init__(self, after: float) -> None:
        super().__init__()
        self.born = time.monotonic()
        self.after = after

    def monotonic(self) -> float:
        if time.monotonic() - self.born > self.after:
            raise RuntimeError("clock failed")
        return super().monotonic()


def test_an_exception_inside_the_call_kills_the_group_and_propagates(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(hang=True, grandchild={"hold_stdout": True})
    gw = Gateway(clock=_ExplodingClock(after=0.5))
    with pytest.raises(RuntimeError, match="clock failed"):
        call(n, home, gw, timeout=100)
    rec = n.events("invoke")[0]
    assert wait_dead(rec["pid"], rec["start"])
    g = n.events("grandchild")
    if g:
        assert wait_dead(g[0]["pid"], g[0]["start"])


def test_only_the_created_group_is_signaled_and_only_while_its_leader_is_unreaped(
    make_notifier: Make, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[int, int]] = []
    unreaped: list[bool] = []
    real = os.killpg

    def spy(pgid: int, sig: int) -> None:
        try:  # still reports the leader (running or zombie) only while it is unreaped
            os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            unreaped.append(True)
        except ChildProcessError:
            unreaped.append(False)
        sent.append((pgid, sig))
        real(pgid, sig)

    monkeypatch.setattr(gw_mod.os, "killpg", spy)
    n = make_notifier(hang=True)
    result = call(n, home, timeout=0.3)
    assert result.pid is not None
    assert {pgid for pgid, _ in sent} == {result.pid}
    assert [sig for _, sig in sent][0] == signal.SIGTERM
    assert all(sig in (signal.SIGTERM, signal.SIGKILL) for _, sig in sent)
    assert unreaped and all(unreaped)
    with pytest.raises(ProcessLookupError):
        real(result.pid, 0)  # the group is gone and the pid is free: the gateway left it alone


def test_a_setup_failure_after_spawn_leaves_no_child(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = _spy_popen(monkeypatch)

    def broken() -> None:
        raise OSError("EMFILE")

    monkeypatch.setattr(gw_mod.selectors, "DefaultSelector", broken)
    with pytest.raises(OSError, match="EMFILE"):
        Gateway().run(SLEEPER, env=scrubbed_env(home), timeout=30)
    assert len(made) == 1
    assert made[0].returncode is not None  # killed and reaped
    with pytest.raises(ProcessLookupError):
        os.killpg(made[0].pid, 0)


def test_a_permission_error_from_killpg_does_not_skip_the_cleanup(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = _spy_popen(monkeypatch)

    def denied(pgid: int, sig: int) -> None:
        raise PermissionError(1, "denied")

    monkeypatch.setattr(gw_mod.os, "killpg", denied)
    code = "import time; time.sleep(1.0)"  # ends by itself, so the cleanup can reap it
    with pytest.raises(RuntimeError, match="clock failed"):
        Gateway(clock=_ExplodingClock(after=0.4)).run(
            [sys.executable, "-I", "-c", code], env=scrubbed_env(home), timeout=100
        )
    assert made[0].returncode is not None  # _release still ran and reaped the child
    assert made[0].stdout is not None and made[0].stdout.closed


def test_a_child_that_cannot_be_reaped_is_an_error_never_exited_with_no_code(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = _spy_popen(monkeypatch)
    real_wait = subprocess.Popen.wait

    def never(self: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("x", timeout or 0)

    code = "pass"
    with monkeypatch.context() as m:
        m.setattr(subprocess.Popen, "wait", never)
        with pytest.raises(RuntimeError, match="could not reap child pid"):
            Gateway().run([sys.executable, "-I", "-c", code], env=scrubbed_env(home), timeout=15)
    real_wait(made[0], timeout=5)  # reap it for real


def test_a_leader_reaped_by_someone_else_is_never_signaled(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[int] = []
    monkeypatch.setattr(gw_mod.os, "killpg", lambda pgid, sig: sent.append(sig))

    def gone(*_a: object) -> None:
        raise ChildProcessError

    monkeypatch.setattr(gw_mod.os, "waitid", gone)
    code = "import time; time.sleep(0.2)"
    with pytest.raises(RuntimeError, match="reaped by someone else; status unknown"):
        Gateway().run([sys.executable, "-I", "-c", code], env=scrubbed_env(home), timeout=15)
    assert sent == []


def test_an_inflight_error_survives_a_release_whose_reap_times_out(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = _spy_popen(monkeypatch)
    real_wait = subprocess.Popen.wait
    real_selector = gw_mod.selectors.DefaultSelector

    class Broken(real_selector):
        def unregister(self, fileobj: object) -> NoReturn:
            raise OSError("unregister boom")

    def never(self: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("x", timeout or 0)

    monkeypatch.setattr(gw_mod.selectors, "DefaultSelector", Broken)
    with monkeypatch.context() as m:
        m.setattr(subprocess.Popen, "wait", never)
        with pytest.raises(OSError, match="unregister boom"):
            Gateway(clock=_ExplodingClock(after=0.3)).run(
                SLEEPER, env=scrubbed_env(home), timeout=100, stdin=b"x" * 1_000_000
            )
    real_wait(made[0], timeout=5)  # reap it for real


def test_a_same_group_background_writer_is_killed_and_its_output_dropped(home: Path) -> None:
    began = time.monotonic()
    result = Gateway().run(
        ["/bin/sh", "-c", "(sleep 0.3; echo late) & exit 0"], env=scrubbed_env(home), timeout=15
    )
    assert result.status == "exited" and result.returncode == 0
    assert result.stdout == b""
    time.sleep(0.5)
    assert time.monotonic() - began < FINAL_READ + 2
