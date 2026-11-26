"""``Gateway.run_detached``: the double fork of an ``async`` hook (#27).

Real processes, each bounded; every signal goes to a PID read from the process itself and
verified (same start time, expected parent, own session) before it is sent.
"""

from __future__ import annotations

import fcntl
import os
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from notice_route import hooks
from notice_route.gateway import Gateway
from notice_route.validate import Hook
from tests.fakes import alive, proc_start, scrubbed_env

ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
HIGH_FD = 5000


def wait_for_text(path: Path, timeout: float = 5.0) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.02)
    raise AssertionError(f"{path} was not written within {timeout}s")


def test_a_descriptor_beyond_4096_is_not_inherited_by_the_supervisor(tmp_path: Path) -> None:
    soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    if soft != resource.RLIM_INFINITY and soft <= HIGH_FD + 10:
        pytest.skip("the soft descriptor limit is below the test's descriptor number")
    secret = tmp_path / "must-not-leak"
    secret.write_text("x")
    low = os.open(secret, os.O_RDONLY)
    high = fcntl.fcntl(low, fcntl.F_DUPFD, HIGH_FD)
    os.close(low)
    out = tmp_path / "fds"
    try:
        assert high >= HIGH_FD
        started = Gateway().run_detached(
            ["/bin/sh", "-c", f"ls -l /proc/$PPID/fd > {out}.tmp; mv {out}.tmp {out}"],
            env=ENV,
            timeout=10,
            stdin="",
        )
        assert started is True
        listing = wait_for_text(out)
    finally:
        os.close(high)
    assert str(secret) not in listing
    assert f" {HIGH_FD}" not in listing


def test_a_failed_second_fork_is_not_reported_as_started(monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.fork
    calls: list[int] = []

    def fork() -> int:
        calls.append(1)
        if len(calls) >= 2:  # the first child's fork (the count is copied into the child)
            raise OSError("no more processes")
        return real()

    monkeypatch.setattr(os, "fork", fork)
    assert Gateway().run_detached(["/bin/true"], env=ENV, timeout=5, stdin="") is False


def test_the_hook_reports_spawn_failed_when_the_detached_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = os.fork
    calls: list[int] = []

    def fork() -> int:
        calls.append(1)
        if len(calls) >= 2:
            raise OSError("no more processes")
        return real()

    monkeypatch.setattr(os, "fork", fork)
    payload = {"object_id": "chat"}
    hook = Hook(cmd="/bin/true", timeout=5.0, is_async=True)
    assert hooks.start_detached(Gateway(), hook, payload) == "spawn_failed"


_SCRIPT = (
    "import sys\n"
    "from notice_route.gateway import Gateway\n"
    "gw = Gateway()\n"
    "with gw.trapped_signals():\n"
    "    ok = gw.run_detached(['/bin/sh', '-c', sys.argv[1]],\n"
    "        env={'PATH': '/usr/bin:/bin'}, timeout=30, stdin='')\n"
    "    sys.exit(0 if ok else 3)\n"
)


def test_the_supervisor_of_a_detached_hook_dies_on_sigterm(tmp_path: Path) -> None:
    """The supervisor inherits flush's flag-setting handlers across the fork; it must get the
    default dispositions back, or it ignores SIGTERM, SIGINT and SIGHUP."""
    home = tmp_path / "home"
    home.mkdir()
    pids = tmp_path / "pids"
    hook = f'echo "$$ $PPID" > {pids}.tmp; mv {pids}.tmp {pids}; exec sleep 25'
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", _SCRIPT, hook],
        env=scrubbed_env(home),
        cwd="/",
        stdin=subprocess.DEVNULL,
    )
    hook_pid = supervisor = 0
    hook_start = supervisor_start = None
    try:
        assert proc.wait(timeout=10) == 0
        hook_pid, supervisor = (int(x) for x in wait_for_text(pids).split())
        hook_start, supervisor_start = proc_start(hook_pid), proc_start(supervisor)
        assert hook_start is not None and supervisor_start is not None
        assert supervisor != proc.pid  # the first child is gone; this is the grandchild
        assert os.getsid(supervisor) != os.getsid(0)  # left the caller's session
        assert os.getpgid(hook_pid) == hook_pid  # the hook has its own group under it
        os.kill(supervisor, signal.SIGTERM)
        end = time.monotonic() + 5
        while alive(supervisor, supervisor_start) and time.monotonic() < end:
            time.sleep(0.02)
        assert not alive(supervisor, supervisor_start)
    finally:
        if hook_start is not None and alive(hook_pid, hook_start):
            os.killpg(hook_pid, signal.SIGKILL)  # the orphaned hook: its own group, verified
        if proc.poll() is None:
            proc.kill()
        proc.wait()
