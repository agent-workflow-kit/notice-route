"""The send lock (#21): in-process timing with a fake clock, then real processes."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from notice_route.gateway import FakeClock, Gateway
from notice_route.sendlock import SHARED_LOCK_NAME, lock_name, send_lock
from notice_route.validate import Kind


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def fake() -> FakeClock:
    return FakeClock(wall=5.0, monotonic=100.0)


@pytest.fixture
def gw(fake: FakeClock) -> Gateway:
    return Gateway(clock=fake)


@pytest.fixture
def state(gw: Gateway, state_path: Path) -> Iterator[object]:
    with gw.open_private_dir(state_path) as d:
        yield d


def lock(gw: Gateway, state: object, name: str = "d", kind: Kind = "generic", wait: float = 1.0):  # type: ignore[no-untyped-def]
    return send_lock(gw, state, name, kind, wait)  # type: ignore[arg-type]


# --- naming -----------------------------------------------------------------------------------


def test_lock_name_is_the_destination_or_the_shared_name_for_wsl() -> None:
    assert lock_name("slack-a", "slack-notice") == "slack-a"
    assert lock_name("ha", "ha-notice") == "ha"
    assert lock_name("g", "generic") == "g"
    assert lock_name("w1", "wsl-notice") == lock_name("w2", "wsl-notice") == SHARED_LOCK_NAME
    assert SHARED_LOCK_NAME == "_wsl"


# --- in-process behavior -----------------------------------------------------------------------


def test_a_free_lock_is_taken_and_the_file_is_private(gw: Gateway, state: object) -> None:
    with lock(gw, state) as held:
        assert held.acquired is True
        assert held.busy_reason is None
    locks = Path(state.path) / "locks"  # type: ignore[attr-defined]
    assert stat.S_IMODE(locks.stat().st_mode) == 0o700
    assert stat.S_IMODE((locks / "d.lock").stat().st_mode) == 0o600


def test_a_held_lock_is_busy_after_exactly_lock_wait_on_the_monotonic_clock(
    gw: Gateway, state: object, fake: FakeClock
) -> None:
    with lock(gw, state) as first:
        assert first.acquired
        start = fake.monotonic()
        with lock(gw, state, wait=2.0) as second:
            assert second.acquired is False
            assert second.busy_reason == "timeout"
        assert fake.monotonic() - start == pytest.approx(2.0, abs=1e-6)


@pytest.mark.parametrize("wait", [1.0, 60.0])
def test_lock_wait_boundaries(gw: Gateway, state: object, fake: FakeClock, wait: float) -> None:
    with lock(gw, state) as first:
        assert first.acquired
        start = fake.monotonic()
        with lock(gw, state, wait=wait) as second:
            assert second.acquired is False
        assert fake.monotonic() - start == pytest.approx(wait, abs=1e-6)


def test_the_lock_is_free_again_after_the_block(gw: Gateway, state: object) -> None:
    with lock(gw, state) as first:
        assert first.acquired
    with lock(gw, state, wait=0) as again:
        assert again.acquired


def test_an_exception_in_the_critical_section_releases_the_lock(gw: Gateway, state: object) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with lock(gw, state) as held:
            assert held.acquired
            raise RuntimeError("boom")
    with lock(gw, state, wait=0) as again:
        assert again.acquired


def test_a_base_exception_releases_the_lock_too(gw: Gateway, state: object) -> None:
    with pytest.raises(KeyboardInterrupt):
        with lock(gw, state):
            raise KeyboardInterrupt
    with lock(gw, state, wait=0) as again:
        assert again.acquired


def test_different_destinations_do_not_block_each_other(
    gw: Gateway, state: object, fake: FakeClock
) -> None:
    with lock(gw, state, "a") as a:
        start = fake.monotonic()
        with lock(gw, state, "b", wait=5.0) as b:
            assert a.acquired and b.acquired
        assert fake.monotonic() == start  # no waiting at all


def test_wsl_destinations_share_one_lock_and_the_second_waits(
    gw: Gateway, state: object, fake: FakeClock
) -> None:
    with lock(gw, state, "w1", "wsl-notice") as first:
        assert first.acquired
        start = fake.monotonic()
        with lock(gw, state, "w2", "wsl-notice", wait=1.0) as second:
            assert second.acquired is False
            assert second.busy_reason == "timeout"
        assert fake.monotonic() - start == pytest.approx(1.0, abs=1e-6)
    with lock(gw, state, "w2", "wsl-notice", wait=0) as later:  # one after the other
        assert later.acquired
    files = sorted(p.name for p in (Path(state.path) / "locks").iterdir())  # type: ignore[attr-defined]
    assert files == ["_wsl.lock"]  # one lock only: no second lock to take in another order


def test_a_wsl_destination_and_another_kind_do_not_block_each_other(
    gw: Gateway, state: object
) -> None:
    with lock(gw, state, "w1", "wsl-notice") as w, lock(gw, state, "s", "slack-notice", 0) as s:
        assert w.acquired and s.acquired


def test_a_wall_clock_jump_during_the_wait_changes_nothing(state_path: Path) -> None:
    class Jumpy(FakeClock):
        def sleep(self, seconds: float) -> None:
            self.advance_monotonic(seconds)
            self.jump_wall(-86_400 if self._monotonic % 2 else 86_400)

    clock = Jumpy(wall=1e9, monotonic=50.0)
    gw = Gateway(clock=clock)
    with gw.open_private_dir(state_path) as d:
        with send_lock(gw, d, "d", "generic", 1.0) as first:
            assert first.acquired
            start = clock.monotonic()
            with send_lock(gw, d, "d", "generic", 1.0) as second:
                assert second.acquired is False
            assert clock.monotonic() - start == pytest.approx(1.0, abs=1e-6)


# --- unsafe lock files -------------------------------------------------------------------------


def _locks_dir(state_path: Path) -> Path:
    path = state_path / "locks"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def test_a_symlink_in_place_of_the_lock_file_is_busy_and_not_followed(
    gw: Gateway, state: object, state_path: Path, tmp_path: Path
) -> None:
    target = tmp_path / "victim"
    target.write_text("keep")
    (_locks_dir(state_path) / "d.lock").symlink_to(target)
    with lock(gw, state) as held:
        assert held.acquired is False
        assert held.busy_reason == "unsafe"
    assert target.read_text() == "keep"
    assert stat.S_IMODE(target.stat().st_mode) != 0o000


def test_a_dangling_symlink_is_not_created_through(
    gw: Gateway, state: object, state_path: Path, tmp_path: Path
) -> None:
    target = tmp_path / "not-there"
    (_locks_dir(state_path) / "d.lock").symlink_to(target)
    with lock(gw, state) as held:
        assert held.acquired is False
    assert not target.exists()


def test_a_fifo_in_place_of_the_lock_file_is_busy_and_does_not_hang(
    gw: Gateway, state: object, state_path: Path
) -> None:
    os.mkfifo(_locks_dir(state_path) / "d.lock", 0o600)
    with lock(gw, state) as held:
        assert held.acquired is False
        assert held.busy_reason == "unsafe"


def test_a_directory_in_place_of_the_lock_file_is_busy(
    gw: Gateway, state: object, state_path: Path
) -> None:
    (_locks_dir(state_path) / "d.lock").mkdir()
    with lock(gw, state) as held:
        assert held.acquired is False
        assert held.busy_reason == "unsafe"


def test_a_symlink_in_place_of_the_locks_directory_is_busy(
    gw: Gateway, state: object, state_path: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (state_path / "locks").symlink_to(elsewhere)
    with lock(gw, state) as held:
        assert held.acquired is False
        assert held.busy_reason == "unsafe"
    assert list(elsewhere.iterdir()) == []


def test_a_locks_directory_open_to_the_group_is_busy(
    gw: Gateway, state: object, state_path: Path
) -> None:
    path = _locks_dir(state_path)
    path.chmod(0o770)
    with lock(gw, state) as held:
        assert held.acquired is False
        assert held.busy_reason == "unsafe"


def test_an_os_error_while_locking_is_busy(
    gw: Gateway, state: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: Gateway, file: object, wait: float) -> bool:
        raise OSError(37, "no locks available")

    monkeypatch.setattr(Gateway, "try_lock", broken)
    with lock(gw, state) as held:
        assert held.acquired is False
        assert held.busy_reason == "error"


def test_an_os_error_while_opening_is_busy(
    gw: Gateway, state: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: Gateway, *a: object, **k: object) -> object:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Gateway, "open_file", broken)
    with lock(gw, state) as held:
        assert held.acquired is False
        assert held.busy_reason == "error"


def test_a_bad_destination_name_is_a_programming_error_not_busy(gw: Gateway, state: object) -> None:
    with pytest.raises(ValueError):
        with lock(gw, state, "../x"):
            pass


def test_the_descriptors_are_closed_after_the_block(gw: Gateway, state: object) -> None:
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(3):
        with lock(gw, state):
            pass
        with lock(gw, state, "x") as a, lock(gw, state, "x", wait=0) as b:
            assert a.acquired and not b.acquired
    assert len(os.listdir("/proc/self/fd")) == before


# --- real processes ----------------------------------------------------------------------------

_WORKER = textwrap.dedent(
    """
    import os, sys, time
    from notice_route.gateway import Gateway
    from notice_route.sendlock import send_lock
    state, name, kind, wait, hold, log = sys.argv[1:7]
    gw = Gateway()
    with gw.open_private_dir(state) as d:
        t0 = time.monotonic()
        with send_lock(gw, d, name, kind, float(wait)) as held:
            waited = time.monotonic() - t0
            if held.acquired:
                with open(log, "a") as f:
                    f.write(f"in {os.getpid()} {time.monotonic():.6f}\\n")
                if hold == "stdin":
                    print("held", flush=True)
                    sys.stdin.read()
                else:
                    time.sleep(float(hold))
                with open(log, "a") as f:
                    f.write(f"out {os.getpid()} {time.monotonic():.6f}\\n")
            else:
                with open(log, "a") as f:
                    f.write(f"busy {os.getpid()} {waited:.6f}\\n")
    """
)


@pytest.fixture
def spawn(home: Path, tmp_path: Path) -> Iterator[Callable[..., subprocess.Popen[str]]]:
    started: list[subprocess.Popen[str]] = []

    def start(
        name: str, kind: str, wait: float, hold: str, *, log: Path | None = None
    ) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                _WORKER,
                str(tmp_path / "state"),
                name,
                kind,
                str(wait),
                hold,
                str(log or tmp_path / "log.txt"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": "/usr/bin"},
        )
        started.append(proc)
        return proc

    yield start
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()


def _held(proc: subprocess.Popen[str]) -> None:
    assert proc.stdout is not None
    assert proc.stdout.readline() == "held\n"


def test_a_second_process_is_busy_after_about_lock_wait_and_never_blocks_past_it(
    spawn: Callable[..., subprocess.Popen[str]], tmp_path: Path
) -> None:
    holder = spawn("d", "generic", 5.0, "stdin")
    _held(holder)
    log = tmp_path / "second.txt"
    start = time.monotonic()
    second = spawn("d", "generic", 0.4, "0", log=log)
    assert second.wait(timeout=10) == 0
    elapsed = time.monotonic() - start
    kind, _pid, waited = log.read_text().split()
    assert kind == "busy"
    assert 0.4 <= float(waited) < 0.4 + 0.5
    assert elapsed < 5.0


def test_a_holder_killed_with_sigkill_frees_the_lock_at_once(
    spawn: Callable[..., subprocess.Popen[str]], tmp_path: Path
) -> None:
    holder = spawn("d", "generic", 5.0, "stdin")
    _held(holder)
    pid = holder.pid
    holder.kill()  # the exact PID started here
    holder.wait(timeout=10)
    assert pid == holder.pid
    log = tmp_path / "after.txt"
    waiter = spawn("d", "generic", 1.0, "0", log=log)
    assert waiter.wait(timeout=10) == 0
    assert log.read_text().split()[0] == "in"


def test_a_wsl_holder_in_another_process_blocks_the_other_wsl_destination(
    spawn: Callable[..., subprocess.Popen[str]], tmp_path: Path
) -> None:
    holder = spawn("w1", "wsl-notice", 5.0, "stdin")
    _held(holder)
    log = tmp_path / "w2.txt"
    other = spawn("w2", "wsl-notice", 0.2, "0", log=log)
    assert other.wait(timeout=10) == 0
    assert log.read_text().split()[0] == "busy"
    log3 = tmp_path / "s.txt"  # a different kind is not held up
    free = spawn("s", "slack-notice", 0.2, "0", log=log3)
    assert free.wait(timeout=10) == 0
    assert log3.read_text().split()[0] == "in"


def test_different_destinations_run_in_parallel_across_processes(
    spawn: Callable[..., subprocess.Popen[str]], tmp_path: Path
) -> None:
    holder = spawn("a", "generic", 5.0, "stdin")
    _held(holder)
    log = tmp_path / "b.txt"
    other = spawn("b", "generic", 0.2, "0", log=log)
    assert other.wait(timeout=10) == 0
    assert log.read_text().split()[0] == "in"


def test_eight_processes_contending_never_overlap_and_none_waits_past_lock_wait(
    spawn: Callable[..., subprocess.Popen[str]], tmp_path: Path
) -> None:
    log = tmp_path / "eight.txt"
    wait = 1.0
    procs = [spawn("d", "generic", wait, "0.05", log=log) for _ in range(8)]
    for proc in procs:
        assert proc.wait(timeout=30) == 0
    entries = [line.split() for line in log.read_text().splitlines()]
    assert len(entries) >= 8  # each process logged in+out, or busy
    spans: dict[str, list[float]] = {}
    for kind, pid, value in entries:
        if kind == "busy":
            assert float(value) < wait + 0.5  # never waits past lock_wait (plus scheduling)
        else:
            spans.setdefault(pid, []).append(float(value))
    ordered = sorted(spans.values())
    assert ordered, "nobody got the lock"
    for (_a_in, a_out), (b_in, _b_out) in zip(ordered, ordered[1:], strict=False):
        assert a_out <= b_in  # no overlap


def test_the_lock_descriptor_is_not_inherited_by_a_spawned_child(
    gw: Gateway, state: object, state_path: Path
) -> None:
    probe = (
        "import os\n"
        "for fd in os.listdir('/proc/self/fd'):\n"
        "    try: print(os.readlink('/proc/self/fd/' + fd))\n"
        "    except OSError: pass\n"
    )
    with lock(gw, state) as held:
        assert held.acquired
        out = subprocess.run(
            [sys.executable, "-I", "-c", probe],
            close_fds=False,  # even when the spawner does not close descriptors
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    assert "d.lock" not in out
    assert str(state_path / "locks") not in out


def test_a_child_that_outlives_the_router_does_not_keep_the_lock(
    spawn: Callable[..., subprocess.Popen[str]], tmp_path: Path, home: Path
) -> None:
    script = textwrap.dedent(
        """
        import subprocess, sys
        from notice_route.gateway import Gateway
        from notice_route.sendlock import send_lock
        gw = Gateway()
        d = gw.open_private_dir(sys.argv[1])
        cm = send_lock(gw, d, "d", "generic", 2.0)
        held = cm.__enter__()
        assert held.acquired
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", "import time; time.sleep(3)"], close_fds=False
        )
        print(child.pid, flush=True)
        sys.stdin.read()
        import os
        os._exit(0)  # the router goes away without releasing anything itself
        """
    )
    router = subprocess.Popen(
        [sys.executable, "-I", "-c", script, str(tmp_path / "state")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": "/usr/bin"},
    )
    assert router.stdout is not None and router.stdin is not None
    child_pid = int(router.stdout.readline())
    try:
        router.stdin.close()
        assert router.wait(timeout=10) == 0
        log = tmp_path / "after.txt"
        waiter = spawn("d", "generic", 0.3, "0", log=log)
        assert waiter.wait(timeout=10) == 0
        assert log.read_text().split()[0] == "in"  # the sleeping child holds nothing
    finally:
        router.stdout.close()
        try:
            os.kill(child_pid, 9)  # the exact PID this test started
        except ProcessLookupError:
            pass
