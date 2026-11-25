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
        closed stdin (EPIPE) is ignored. Only the group created here is ever signaled; when the
        leader exits, the group is SIGKILLed (a same-group descendant dies, its later output is
        dropped).

        Raises ``ValueError`` for a bad ``argv`` (not a non-empty list of strings without NUL),
        ``env`` (keys with ``=`` or NUL, values with NUL, non-strings), ``timeout`` (not a
        finite positive int or float) or ``stdin`` (not None, str, bytes, bytearray or
        memoryview), all before anything starts. Raises ``RuntimeError`` when SIGCHLD is
        ignored (the kernel would reap children and the results would be wrong) or when the
        child could not be reaped. Precondition: SIGPIPE is ignored, as Python does by default.
        """
        # TODO(#19): the notifier argv/env builders (scrubbed PATH, locale, named variables)
        # live in the caller; this method passes ``env`` through untouched.
        if isinstance(argv, (str, bytes)):
            raise ValueError("argv must be a list of strings, not one string")
        args = list(argv)
        if not args or not all(isinstance(a, str) and "\x00" not in a for a in args):
            raise ValueError("argv must be a non-empty list of strings without NUL")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be an int or a float")
        timeout = _finite(timeout, "timeout")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        environment = dict(env)
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
            raise ValueError("env must map strings to strings")
        if any("=" in k or "\x00" in k or "\x00" in v for k, v in environment.items()):
            raise ValueError("env names may not hold '=' or NUL and values may not hold NUL")
        if stdin is None:
            payload = b""
        elif isinstance(stdin, str):
            payload = stdin.encode("utf-8")
        elif isinstance(stdin, (bytes, bytearray, memoryview)):
            payload = bytes(stdin)
        else:
            raise ValueError("stdin must be None, str, bytes, bytearray or memoryview")
        if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
            raise RuntimeError("SIGCHLD is ignored: the kernel would reap children unseen")
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
    ``overflow`` names the first stream to cross its cap; the output holds at most the cap.
    The field is also set, beside status ``timeout``, when a stream overflows during the TERM
    grace window (the group is then SIGKILLed at once); the status keeps the first reason.
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
        self.sel: selectors.BaseSelector | None = None
        self.pending = memoryview(payload)
        self.stdin_fd: int | None = None
        self.pipes: list[_Pipe] = []  # still open
        self.all_pipes: list[_Pipe] = []
        self.reaped = False
        self.foreign_reap = False
        self.first_over: Literal["stdout", "stderr"] | None = None

    # The group id is this process's pid. It cannot be reused while the leader is unreaped
    # (even as a zombie), so every signal is sent before the leader is reaped, never after.
    def _signal_group(self, sig: int) -> None:
        if self.reaped:
            return
        try:
            os.killpg(self.pgid, sig)
        except OSError:
            pass  # ProcessLookupError (group gone) or PermissionError: nothing more to do

    def _leader_exited(self) -> bool:
        try:
            return os.waitid(os.P_PID, self.pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            self.reaped = True  # someone else reaped it: the group id may be reused, never signal
            self.foreign_reap = True
            return True

    def _close_stdin(self) -> None:
        if self.stdin_fd is not None:
            assert self.sel is not None
            self.sel.unregister(self.stdin_fd)
            self.stdin_fd = None
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass  # EPIPE on flushing a closed pipe: ignored by design

    def _close_pipe(self, pipe: _Pipe) -> None:
        assert self.sel is not None
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
            if self.first_over is None:
                self.first_over = pipe.name

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
            result = self._loop()
        except BaseException:
            try:
                self._signal_group(signal.SIGKILL)
            finally:
                self._release()
            raise
        if self.foreign_reap:
            raise RuntimeError("child was reaped by someone else; status unknown")
        if self.proc.returncode is None:
            raise RuntimeError(f"could not reap child pid {self.pgid}")
        return result

    def _release(self) -> None:
        """Close every pipe and reap the leader (bounded); used on every exit path."""
        try:
            self._close_stdin()
        finally:
            try:
                for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                if self.sel is not None:
                    self.sel.close()
            finally:
                try:
                    self.proc.wait(timeout=5)
                    self.reaped = True
                except subprocess.TimeoutExpired:
                    pass  # no return in a finally: an in-flight exception must keep propagating

    def _loop(self) -> RunResult:
        proc = self.proc
        assert proc.stdin and proc.stdout and proc.stderr
        self.sel = selectors.DefaultSelector()
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
            over_now = any(p.over for p in self.all_pipes)
            if reason is None and over_now:
                reason = "overflow"
            exited = self._leader_exited()
            now = self.clock.monotonic()
            if phase != "final":
                if over_now or exited:
                    # Overflow (also during the TERM grace): kill at once. Exit: whatever is
                    # left in the group is orphaned.
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
            overflow=self.first_over,
            pid=self.pgid,
            elapsed=self.clock.monotonic() - self.t0,
        )
