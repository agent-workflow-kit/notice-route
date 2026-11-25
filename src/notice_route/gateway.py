"""Gateway: the only module that touches processes, locks, clocks and the filesystem.

Every other module reaches the outside world through a :class:`Gateway`, so tests can
substitute a fake clock, a fixed token source and fake notifier binaries
(``tests/fakebin/``). ``tests/test_import_boundary.py`` fails when any other module imports
``subprocess``, ``fcntl``, ``time`` or ``os``.

Commands run as argv lists, never through a shell, and untrusted text travels on stdin or in
files, never in argv or the environment.
"""

from __future__ import annotations

import math
import os
import secrets
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

__all__ = [
    "Clock",
    "FakeClock",
    "FixedTokens",
    "FINAL_READ",
    "Gateway",
    "RunResult",
    "SystemClock",
    "STDERR_LIMIT",
    "STDOUT_LIMIT",
    "SystemTokens",
    "TERM_GRACE",
    "TokenSource",
]


STDOUT_LIMIT = 65536
STDERR_LIMIT = 16384
TERM_GRACE = 5.0
FINAL_READ = 1.0
_POLL_SLICE = 0.02
_CHUNK = 65536

RunStatus = Literal["exited", "signaled", "timeout", "overflow", "spawn_failed"]


def _finite(value: float, what: str) -> float:
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{what} must be finite") from None
    if not math.isfinite(number):
        raise ValueError(f"{what} must be finite")
    return number


class Clock(Protocol):
    """Two independent time sources.

    ``wall`` is the calendar time (decisions, windows, leases) and may jump.
    ``monotonic`` only moves forward and is the only source for deadlines.
    """

    def wall(self) -> float: ...

    def monotonic(self) -> float: ...


class SystemClock:
    """The real clocks."""

    def wall(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """A deterministic clock whose wall and monotonic time move independently."""

    def __init__(self, wall: float = 1_700_000_000.0, monotonic: float = 1_000.0) -> None:
        self._wall = _finite(wall, "wall")
        self._monotonic = _finite(monotonic, "monotonic")

    def wall(self) -> float:
        return self._wall

    def monotonic(self) -> float:
        return self._monotonic

    def jump_wall(self, seconds: float) -> None:
        """Move only wall time, in either direction (an NTP step or a manual change)."""
        self._wall = _finite(self._wall + _finite(seconds, "seconds"), "wall")

    def advance_monotonic(self, seconds: float) -> None:
        """Move only monotonic time; it never goes backwards."""
        seconds = _finite(seconds, "seconds")
        if seconds < 0:
            raise ValueError("monotonic time must advance by a finite, non-negative amount")
        self._monotonic = _finite(self._monotonic + seconds, "monotonic")

    def advance(self, seconds: float) -> None:
        """Let real time pass: both clocks move forward together."""
        seconds = _finite(seconds, "seconds")
        if seconds < 0:
            raise ValueError("time must advance by a finite, non-negative amount")
        wall = _finite(self._wall + seconds, "wall")
        monotonic = _finite(self._monotonic + seconds, "monotonic")
        self._wall, self._monotonic = wall, monotonic


class TokenSource(Protocol):
    """Source of unguessable ids (claims, delivery ids)."""

    def new(self) -> str: ...


class SystemTokens:
    """128 random bits as 32 lowercase hex characters."""

    def new(self) -> str:
        return secrets.token_hex(16)


class FixedTokens:
    """A fixed sequence of tokens for deterministic tests; refuses to run dry silently."""

    def __init__(self, tokens: Sequence[str]) -> None:
        if isinstance(tokens, str):
            raise TypeError("tokens must be a sequence of strings, not a single string")
        if len(set(tokens)) != len(tokens):
            raise ValueError("tokens must be unique")
        self._tokens = list(tokens)
        self._next = 0

    def new(self) -> str:
        if self._next >= len(self._tokens):
            raise RuntimeError("FixedTokens exhausted")
        token = self._tokens[self._next]
        self._next += 1
        return token


@dataclass
class Gateway:
    """The injectable boundary. Build one at startup and pass it down."""

    clock: Clock = field(default_factory=SystemClock)
    tokens: TokenSource = field(default_factory=SystemTokens)

    def wall(self) -> float:
        return self.clock.wall()

    def monotonic(self) -> float:
        return self.clock.monotonic()

    def new_token(self) -> str:
        return self.tokens.new()

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        stdin: str | bytes | None = None,
    ) -> RunResult:
        """Run ``argv`` the way the router calls every notifier and hook.

        No shell; a new session and process group; descriptors beyond stdin, stdout and
        stderr closed; working directory ``/``; exactly the environment ``env``. Output is read
        through a bounded reader (``STDOUT_LIMIT`` and ``STDERR_LIMIT`` bytes; more kills the
        group at once and reports ``overflow``). One deadline on the clock's monotonic time,
        counted from the process start and including the stdin write: at the deadline SIGTERM
        goes to the group, ``TERM_GRACE`` seconds later SIGKILL. After the child exits or the
        group is killed, output is read for at most ``FINAL_READ`` more seconds and the pipes
        are closed, so a descendant that left the group cannot hold the call. A write to a
        closed stdin (EPIPE) is ignored. Only the group created here is ever signaled.
        """
        # TODO(#19): the notifier argv/env builders (scrubbed PATH, locale, named variables)
        # live in the caller; this method passes ``env`` through untouched.
        args = list(argv)
        if not args or not all(isinstance(a, str) for a in args):
            raise ValueError("argv must be a non-empty list of strings")
        timeout = _finite(timeout, "timeout")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        environment = dict(env)
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
            raise ValueError("env must map strings to strings")
        payload = stdin.encode("utf-8") if isinstance(stdin, str) else (stdin or b"")
        started = self.clock.monotonic()
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env=environment,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            return RunResult(
                status="spawn_failed",
                returncode=None,
                signal=None,
                stdout=b"",
                stderr=b"",
                overflow=None,
                pid=None,
                elapsed=self.clock.monotonic() - started,
                error=f"{type(exc).__name__}: {exc}",
            )
        return _Supervisor(self.clock, proc, payload, started + timeout, started).run()

    # TODO(#5): flock helpers and filesystem helpers (directories, files, home lookup).


@dataclass(frozen=True)
class RunResult:
    """What :meth:`Gateway.run` reports. The four outcomes are distinct.

    ``exited``: the child ended by itself, ``returncode`` is its exit code.
    ``signaled``: a signal ended it that the gateway did not send, ``signal`` is the number.
    ``timeout``: the deadline passed and the gateway killed the group; ``returncode`` or
    ``signal`` say how the child ended afterwards (``signal`` is SIGTERM or SIGKILL unless the
    child handled TERM).
    ``overflow``: stdout or stderr exceeded its cap and the gateway killed the group;
    ``overflow`` names the stream; the output holds at most the cap.
    ``spawn_failed``: the program could not be started; ``error`` says why.
    ``stdout`` and ``stderr`` are raw bytes, never decoded here.
    """

    status: RunStatus
    returncode: int | None
    signal: int | None
    stdout: bytes
    stderr: bytes
    overflow: Literal["stdout", "stderr"] | None
    pid: int | None
    elapsed: float
    error: str | None = None


class _Pipe:
    def __init__(self, name: Literal["stdout", "stderr"], fd: int, cap: int) -> None:
        self.name: Literal["stdout", "stderr"] = name
        self.fd = fd
        self.cap = cap
        self.buf = bytearray()
        self.over = False


class _Supervisor:
    """Drives one spawned child to its end in a single-threaded loop (no helper threads)."""

    def __init__(
        self,
        clock: Clock,
        proc: subprocess.Popen[bytes],
        payload: bytes,
        deadline: float,
        t0: float,
    ) -> None:
        self.clock = clock
        self.proc = proc
        self.pgid = proc.pid  # start_new_session: the child leads a group with its own pid
        self.deadline = deadline
        self.t0 = t0
        self.sel = selectors.DefaultSelector()
        self.pending = memoryview(payload)
        self.stdin_fd: int | None = None
        self.pipes: list[_Pipe] = []  # still open
        self.all_pipes: list[_Pipe] = []
        self.reaped = False

    # The group id is this process's pid. It cannot be reused while the leader is unreaped
    # (even as a zombie), so every signal is sent before the leader is reaped, never after.
    def _signal_group(self, sig: int) -> None:
        if self.reaped:
            return
        try:
            os.killpg(self.pgid, sig)
        except ProcessLookupError:
            pass

    def _leader_exited(self) -> bool:
        try:
            return os.waitid(os.P_PID, self.pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return True

    def _close_stdin(self) -> None:
        if self.stdin_fd is not None:
            self.sel.unregister(self.stdin_fd)
            self.stdin_fd = None
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass  # EPIPE on flushing a closed pipe: ignored by design

    def _close_pipe(self, pipe: _Pipe) -> None:
        self.sel.unregister(pipe.fd)
        self.pipes.remove(pipe)
        stream = self.proc.stdout if pipe.name == "stdout" else self.proc.stderr
        if stream is not None:
            stream.close()

    def _read(self, pipe: _Pipe) -> None:
        want = _CHUNK if pipe.over else min(_CHUNK, pipe.cap + 1 - len(pipe.buf))
        try:
            data = os.read(pipe.fd, want)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._close_pipe(pipe)
            return
        if pipe.over:
            return
        pipe.buf.extend(data)
        if len(pipe.buf) > pipe.cap:
            del pipe.buf[pipe.cap :]
            pipe.over = True

    def _write(self) -> None:
        assert self.stdin_fd is not None
        try:
            sent = os.write(self.stdin_fd, self.pending[:_CHUNK])
        except BlockingIOError:
            return
        except BrokenPipeError:
            self._close_stdin()  # the child closed stdin early; its exit code decides
            return
        self.pending = self.pending[sent:]
        if not self.pending:
            self._close_stdin()

    def run(self) -> RunResult:
        try:
            return self._loop()
        except BaseException:
            self._signal_group(signal.SIGKILL)
            self._release()
            raise

    def _release(self) -> None:
        """Close every pipe and reap the leader (bounded); used on every exit path."""
        self._close_stdin()
        for pipe in list(self.pipes):
            self._close_pipe(pipe)
        self.sel.close()
        if not self.reaped:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return
            self.reaped = True

    def _loop(self) -> RunResult:
        proc = self.proc
        assert proc.stdin and proc.stdout and proc.stderr
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
        self.pipes = [
            _Pipe("stdout", proc.stdout.fileno(), STDOUT_LIMIT),
            _Pipe("stderr", proc.stderr.fileno(), STDERR_LIMIT),
        ]
        self.all_pipes = list(self.pipes)
        for pipe in self.pipes:
            self.sel.register(pipe.fd, selectors.EVENT_READ, pipe)
        if self.pending:
            self.stdin_fd = proc.stdin.fileno()
            self.sel.register(self.stdin_fd, selectors.EVENT_WRITE, None)
        else:
            self._close_stdin()

        phase = "run"  # run -> (grace ->) final
        reason: Literal["timeout", "overflow"] | None = None
        term_at = 0.0
        final_until = 0.0
        while True:
            if self.sel.get_map():
                for key, _ in self.sel.select(_POLL_SLICE):
                    if key.data is None:
                        if self.stdin_fd is not None:
                            self._write()
                    elif key.data in self.pipes:
                        self._read(key.data)
            else:
                time.sleep(_POLL_SLICE)
            if reason is None and any(p.over for p in self.pipes):
                reason = "overflow"
            exited = self._leader_exited()
            now = self.clock.monotonic()
            if phase != "final":
                if reason == "overflow" or exited:
                    # Overflow: kill at once. Exit: whatever is left in the group is orphaned.
                    self._signal_group(signal.SIGKILL)
                    phase = "final"
                elif phase == "run" and now >= self.deadline:
                    reason = "timeout"
                    self._signal_group(signal.SIGTERM)
                    phase, term_at = "grace", now
                elif phase == "grace" and now >= term_at + TERM_GRACE:
                    self._signal_group(signal.SIGKILL)
                    phase = "final"
                if phase == "final":
                    final_until = now + FINAL_READ
                    self._close_stdin()
            elif reason == "overflow" or not self.pipes or now >= final_until:
                break
        self._signal_group(signal.SIGKILL)  # idempotent; every path to here already sent it
        self._release()
        stdout, stderr = (bytes(p.buf) for p in self.all_pipes)
        over: Literal["stdout", "stderr"] | None = None
        for pipe in self.all_pipes:
            if pipe.over:
                over = pipe.name
                break
        returncode = proc.returncode
        if reason == "overflow":
            status: RunStatus = "overflow"
        elif reason == "timeout":
            status = "timeout"
        elif returncode is not None and returncode < 0:
            status = "signaled"
        else:
            status = "exited"
        return RunResult(
            status=status,
            returncode=returncode if returncode is not None and returncode >= 0 else None,
            signal=-returncode if returncode is not None and returncode < 0 else None,
            stdout=stdout,
            stderr=stderr,
            overflow=over,
            pid=self.pgid,
            elapsed=self.clock.monotonic() - self.t0,
        )
