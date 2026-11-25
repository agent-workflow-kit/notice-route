"""Gateway: the only module that touches processes, locks, clocks and the filesystem.

Every other module reaches the outside world through a :class:`Gateway`, so tests can
substitute a fake clock, a fixed token source and fake notifier binaries
(``tests/fakebin/``). ``tests/test_import_boundary.py`` fails when any other module imports
``subprocess``, ``fcntl``, ``time`` or ``os``.

Commands run as argv lists, never through a shell, and untrusted text travels on stdin or in
files, never in argv or the environment.
"""

from __future__ import annotations

import errno
import fcntl
import io
import math
import os
import pwd
import re
import secrets
import select
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import zoneinfo
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from .schema import APPLICATION_ID, SCHEMA_VERSION

__all__ = [
    "Clock",
    "DirHandle",
    "FakeClock",
    "FixedTokens",
    "FINAL_READ",
    "Gateway",
    "HomeError",
    "InputFileError",
    "InputRead",
    "POLL_INTERVAL",
    "RunResult",
    "SYSTEM_TZ_DIRS",
    "StateError",
    "StateFiles",
    "StateView",
    "SystemClock",
    "STDERR_LIMIT",
    "STDOUT_LIMIT",
    "SystemTokens",
    "TERM_GRACE",
    "TokenSource",
    "UnsafePathError",
    "ZoneError",
]


STDOUT_LIMIT = 65536
STDERR_LIMIT = 16384
TERM_GRACE = 5.0
FINAL_READ = 1.0
POLL_INTERVAL = 0.05
_POLL_SLICE = 0.02
_INPUT_SLICE = 0.1
_CHUNK = 65536
# The system time zone database, in search order. ``PYTHONTZPATH`` and ``zoneinfo.TZPATH`` are
# never consulted: the config selects a zone by name and must not reach other files.
SYSTEM_TZ_DIRS = (
    "/usr/share/zoneinfo",
    "/usr/lib/zoneinfo",
    "/usr/share/lib/zoneinfo",
    "/etc/zoneinfo",
)
_ZONE_KEY = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+){0,2}")
_ZONE_REFUSED = frozenset({"localtime", "posixrules", "factory"})
_ZONE_MAX_BYTES = 256 * 1024

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

    def sleep(self, seconds: float) -> None:
        """Let ``seconds`` pass; the fake moves both clocks instead of waiting.

        Contract: after ``sleep(n)`` returns, ``monotonic()`` has advanced by at least ``n``.
        """
        ...


class SystemClock:
    """The real clocks."""

    def wall(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


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

    def sleep(self, seconds: float) -> None:
        """Sleeping is time passing: both clocks move forward, nothing waits."""
        self.advance(seconds)

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


class HomeError(Exception):
    """The password-database home directory is unusable.

    ``reason`` is ``empty``, ``relative`` or ``unresolvable`` (no entry, a bad value).
    """

    def __init__(self, reason: Literal["empty", "relative", "unresolvable"], detail: str) -> None:
        super().__init__(f"home directory is {reason}: {detail}")
        self.reason = reason


class UnsafePathError(Exception):
    """A path was refused for safety, never repaired.

    ``reason``: ``symlink``, ``not_regular`` (a FIFO, directory, device or socket where a regular
    file belongs), ``not_directory`` (a symlink or file where a directory belongs), ``owner``
    (a directory not owned by the effective user) or ``mode`` (group or world access).
    """

    def __init__(self, reason: str, path: str) -> None:
        super().__init__(f"refused {path!r}: {reason}")
        self.reason = reason
        self.path = path


class StateError(Exception):
    """``state.db`` cannot be read (unsafe path, corrupt, not the expected schema): exit 1."""


@dataclass(frozen=True)
class StateFiles:
    """A checked ``state.db``: where SQLite may open it and whether a ``-wal`` file exists."""

    path: str
    wal_present: bool


@dataclass(frozen=True)
class StateView:
    """What ``validate`` reads from ``state.db``, read-only (``exists`` False: no database)."""

    exists: bool = False
    flush_last: float | None = None
    flush_env_missing: float | None = None
    surfaced_today: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    queue_by_destination: Mapping[str, int] = field(default_factory=dict)


_NOTIFIER_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class ZoneError(Exception):
    """A time zone cannot be loaded from the system database (the message never names it)."""


class InputFileError(Exception):
    """A ``--file`` path cannot be used: ``unreadable`` (cannot be opened or read) or
    ``not_regular`` (a directory, FIFO, device or socket). The message never names the path."""

    def __init__(self, reason: Literal["unreadable", "not_regular"]) -> None:
        super().__init__(f"input file is {reason.replace('_', ' ')}")
        self.reason = reason


@dataclass(frozen=True)
class InputRead:
    """Bytes read from an input and how the read ended.

    ``ok``: end of input. ``too_large``: more than the limit (``data`` holds the first
    ``limit`` bytes). ``timeout``: the deadline passed first. ``unreadable``: stdin is closed or
    cannot be read (``data`` is empty).
    """

    data: bytes
    status: Literal["ok", "too_large", "timeout", "unreadable"]


class LimitExceededError(Exception):
    """A file or a directory listing is larger than the caller's limit (never truncated)."""


class DirHandle:
    """An open directory descriptor; files and subdirectories are opened relative to it."""

    def __init__(self, fd: int, path: str) -> None:
        self.fd = fd
        self.path = path

    @property
    def closed(self) -> bool:
        return self.fd < 0

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            os.close(fd)

    def __enter__(self) -> DirHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


FileMode = Literal["r", "a", "rw"]
_FILE_FLAGS: dict[str, int] = {"r": os.O_RDONLY, "a": os.O_WRONLY | os.O_APPEND, "rw": os.O_RDWR}
_FILE_IO_MODES: dict[str, str] = {"r": "rb", "a": "ab", "rw": "rb+"}
_UMASK_LOCK = threading.Lock()


@contextmanager
def _umask_077() -> Iterator[None]:
    """Run with umask 077; the swap is process-wide, so one thread at a time holds it."""
    with _UMASK_LOCK:
        old = os.umask(0o077)
        try:
            yield
        finally:
            os.umask(old)


def _component(name: str) -> str:
    if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\x00" in name:
        raise ValueError("name must be one plain path component")
    return name


def _system_environ() -> Mapping[str, str]:
    return os.environ


def _passwd_home() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError as exc:
        raise HomeError("unresolvable", "no password-database entry") from exc


def _open_checked_dir(path: str, dir_fd: int | None, shown: str) -> DirHandle:
    """Create ``path`` (0700, umask 077) when absent, open it without following a symlink, and
    refuse it unless it is a directory owned by the effective user with no group/world access.
    Setgid and sticky bits grant nothing on such a directory (a child of a setgid parent
    inherits setgid) and are tolerated."""
    with _umask_077():
        try:
            os.mkdir(path, 0o700, dir_fd=dir_fd)
        except FileExistsError:
            pass
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise UnsafePathError("not_directory", shown) from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafePathError("not_directory", shown)
        if info.st_uid != os.geteuid():
            raise UnsafePathError("owner", shown)
        if info.st_mode & 0o077:  # any group/world access
            raise UnsafePathError("mode", shown)
    except BaseException:
        os.close(fd)
        raise
    return DirHandle(fd, shown)


@dataclass
class Gateway:
    """The injectable boundary. Build one at startup and pass it down."""

    clock: Clock = field(default_factory=SystemClock)
    tokens: TokenSource = field(default_factory=SystemTokens)
    passwd_home: Callable[[], str] = _passwd_home
    tz_dirs: tuple[str, ...] = SYSTEM_TZ_DIRS
    stdin_fd: int = 0
    environ: Callable[[], Mapping[str, str]] = _system_environ
    exec_check_from: str = "/"  # directories above this one are trusted (tests only)

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

    def home_dir(self) -> Path:
        """The home directory from the password database; ``HOME`` and ``XDG_*`` are never read.

        Raises :class:`HomeError` for an empty, relative or unresolvable home (callers report
        exit 2 ``config_invalid``, ``details.key`` ``home``). The path is not checked for
        existence and symlinks are not resolved.
        """
        try:
            value = self.passwd_home()
        except HomeError:
            raise
        except (KeyError, OSError) as exc:
            raise HomeError("unresolvable", type(exc).__name__) from exc
        if not isinstance(value, str) or "\x00" in value:
            raise HomeError("unresolvable", "not a usable string")
        if value == "":
            raise HomeError("empty", "the password database gives no home")
        if not value.startswith("/"):
            raise HomeError("relative", repr(value))
        return Path(value)

    def open_private_dir(self, path: str | os.PathLike[str]) -> DirHandle:
        """Open the absolute directory ``path``, creating it and its parents with mode 0700
        under ``umask 077`` when absent.

        The last component is opened with ``O_NOFOLLOW``; the directory is refused
        (:class:`UnsafePathError`, never repaired) when it is a symlink or not a directory, is
        not owned by the effective user (``owner``) or has any group or world access
        (``mode``; setgid and sticky bits are tolerated). The caller closes the handle.

        Trust assumption: parents that already exist are neither checked nor protected from
        symlinks; they are followed as the operating system resolves them, so the user's home
        and every ancestor of the state directory are trusted. Only the last component is checked.

        The path is validated before anything is created: it must be an absolute ``str`` (or
        path-like giving one) without NUL and without ``.`` or ``..`` components, else
        ``ValueError``; empty components (``//``) are collapsed, so ``DirHandle.path`` is
        normalized. A parent that is not a directory (a regular file) is
        ``UnsafePathError("not_directory")``. Other ``OSError`` values (``PermissionError`` on an
        unreadable directory, ``ENAMETOOLONG``, ...) propagate unchanged.
        """
        text = os.fspath(path)
        if isinstance(text, bytes):
            raise ValueError("path must be a str, not bytes")
        if not isinstance(text, str) or not text.startswith("/") or "\x00" in text:
            raise ValueError("path must be an absolute path without NUL")
        parts = [part for part in text.split("/") if part]
        if any(part in (".", "..") for part in parts):
            raise ValueError("path must not contain '.' or '..' components")
        normalized = "/" + "/".join(parts)
        if not parts:
            return _open_checked_dir("/", None, "/")
        parent = os.path.dirname(normalized)
        try:
            with _umask_077():
                os.makedirs(parent, mode=0o700, exist_ok=True)
        except (FileExistsError, NotADirectoryError) as exc:
            raise UnsafePathError("not_directory", normalized) from exc
        return _open_checked_dir(normalized, None, normalized)

    def open_private_subdir(self, parent: DirHandle, name: str) -> DirHandle:
        """Open (creating at 0700) the directory ``name`` inside ``parent``, with the same
        refusals as :meth:`open_private_dir`."""
        self._live(parent)
        return _open_checked_dir(_component(name), parent.fd, f"{parent.path.rstrip('/')}/{name}")

    def open_file(
        self, parent: DirHandle, name: str, *, mode: FileMode = "r", create: bool = False
    ) -> io.FileIO:
        """Open the file ``name`` inside ``parent`` as an unbuffered binary file.

        Uses ``O_NOFOLLOW`` and ``O_NONBLOCK`` relative to the directory descriptor and checks
        ``fstat`` for a regular file: a symlink (``symlink``) or a FIFO, directory, device or
        socket (``not_regular``) raises :class:`UnsafePathError`. ``mode`` is ``r`` (read), ``a``
        (append) or ``rw``; ``create`` makes a missing file with mode 0600 (before the umask).
        A missing file without ``create`` is ``FileNotFoundError``. The descriptor is not
        inherited by children and cannot become a controlling terminal (``O_NOCTTY``).

        Precondition: ``parent`` is a private directory handle from :meth:`open_private_dir` or
        :meth:`open_private_subdir`; this method does not re-check the directory. Typed errors
        are :class:`UnsafePathError` and ``ValueError`` (a bad ``name`` or ``mode``); other
        ``OSError`` values propagate.
        """
        self._live(parent)
        if not isinstance(mode, str) or mode not in _FILE_FLAGS:
            raise ValueError(f"unknown mode {mode!r}")
        shown = f"{parent.path.rstrip('/')}/{_component(name)}"
        flags = _FILE_FLAGS[mode] | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY
        if create:
            flags |= os.O_CREAT
        try:
            fd = os.open(name, flags, 0o600, dir_fd=parent.fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise UnsafePathError("symlink", shown) from exc
            if exc.errno in (errno.EISDIR, errno.ENXIO):  # ENXIO: socket, or FIFO with no reader
                raise UnsafePathError("not_regular", shown) from exc
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise UnsafePathError("not_regular", shown)
            # FileIO takes ownership of fd only once constructed; a failure leaves it ours.
            return io.FileIO(fd, _FILE_IO_MODES[mode], closefd=True)
        except BaseException:
            os.close(fd)
            raise

    def try_lock(self, file: io.FileIO, wait: float) -> bool:
        """Take an exclusive ``flock`` on ``file`` without ever blocking.

        Tries at once, then every ``POLL_INTERVAL`` (50 ms) until ``wait`` seconds of the
        clock's monotonic time have passed (the last sleep is shortened to the deadline, and one
        last try follows it). Returns False when not acquired. The kernel releases the lock when
        the holder dies or closes the file; :meth:`unlock` releases it early. ``wait`` must be a
        finite number of seconds >= 0 (``ValueError`` otherwise, before anything is tried); the
        1 to 60 s range of ``lock_wait`` is enforced by the config loader (#7), not here. Any
        ``OSError`` other than "would block" (``ENOLCK``, a closed file) propagates.

        Lock files must never be renamed or unlinked while in use: a second process would
        create a new file and hold a different lock.
        """
        if isinstance(wait, bool) or not isinstance(wait, (int, float)):
            raise ValueError("wait must be a number of seconds")
        wait = _finite(wait, "wait")
        if wait < 0:
            raise ValueError("wait must not be negative")
        deadline = self.clock.monotonic() + wait
        while True:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                pass
            remaining = deadline - self.clock.monotonic()
            if remaining <= 0:
                return False
            self.clock.sleep(min(POLL_INTERVAL, remaining))

    def unlock(self, file: io.FileIO) -> None:
        """Release a lock taken with :meth:`try_lock`; harmless when not held."""
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def open_config_dir(self, path: str, parent: DirHandle | None = None) -> DirHandle | None:
        """Open an existing config directory without creating anything; ``None`` when absent.

        ``path`` is any path without NUL when ``parent`` is None (relative paths use the working
        directory), else one plain component inside ``parent``. The last component is opened
        with ``O_NOFOLLOW``; it is refused (:class:`UnsafePathError`) when it is a symlink or
        not a directory (``not_directory``), is not owned by the effective user (``owner``) or
        is group- or world-writable (``mode``). Unlike :meth:`open_private_dir`, read access for
        group and world is accepted, and setgid and sticky bits are tolerated. A missing
        directory, or a missing parent on the way (also one that is not a directory), is ``None``.
        Other ``OSError`` values (``PermissionError``, ``ENAMETOOLONG``) propagate.
        The caller closes the handle.
        """
        if not isinstance(path, str) or "\x00" in path:
            raise ValueError("path must be a string without NUL")
        if parent is not None:
            self._live(parent)
            shown = f"{parent.path.rstrip('/')}/{_component(path)}"
            dir_fd: int | None = parent.fd
        else:
            if path == "":
                raise ValueError("path must not be empty")
            shown, dir_fd = path, None
        try:
            fd = os.open(
                path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                if exc.errno == errno.ENOTDIR and not _last_component_exists(path, dir_fd):
                    return None  # a non-directory earlier on the way: nothing is there
                raise UnsafePathError("not_directory", shown) from exc
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                raise UnsafePathError("not_directory", shown)
            if info.st_uid != os.geteuid():
                raise UnsafePathError("owner", shown)
            if info.st_mode & 0o022:  # group or world write
                raise UnsafePathError("mode", shown)
        except BaseException:
            os.close(fd)
            raise
        return DirHandle(fd, shown)

    def read_config_file(self, parent: DirHandle, name: str, limit: int) -> bytes | None:
        """Read the config file ``name`` inside ``parent``; ``None`` when it does not exist.

        Opened as :meth:`open_file` does (``O_NOFOLLOW``, ``O_NONBLOCK``, regular files only:
        ``symlink`` and ``not_regular`` are :class:`UnsafePathError`) and then checked on the
        descriptor: owned by the effective user (``owner``) and not group- or world-writable
        (``mode``). At most ``limit`` bytes are accepted: one byte more raises
        :class:`LimitExceededError` after reading no more than ``limit + 1`` bytes.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        try:
            file = self.open_file(parent, name)
        except FileNotFoundError:
            return None
        with file:
            info = os.fstat(file.fileno())
            shown = f"{parent.path.rstrip('/')}/{name}"
            if info.st_uid != os.geteuid():
                raise UnsafePathError("owner", shown)
            if info.st_mode & 0o022:
                raise UnsafePathError("mode", shown)
            data = bytearray()
            while len(data) <= limit:
                chunk = file.read(min(_CHUNK, limit + 1 - len(data)))
                if not chunk:
                    return bytes(data)
                data += chunk
        raise LimitExceededError(f"{shown!r} is larger than {limit} bytes")

    def list_dir(self, handle: DirHandle, *, limit: int) -> list[str]:
        """The names in the directory (unsorted, ``.`` and ``..`` excluded). More than ``limit``
        entries is :class:`LimitExceededError` (the listing stops there)."""
        self._live(handle)
        names: list[str] = []
        with os.scandir(handle.fd) as entries:
            for entry in entries:
                if len(names) >= limit:
                    raise LimitExceededError(f"{handle.path!r} has more than {limit} entries")
                names.append(entry.name)
        return names

    def load_zone(self, key: str) -> zoneinfo.ZoneInfo:
        """The zone ``key`` from the system database (``tz_dirs``), else :class:`ZoneError`.

        ``key`` must match ``[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}`` and be at most 64
        characters, so it cannot leave the directory; ``localtime``, ``posixrules`` and
        ``Factory`` (any case, as the last path segment) are refused. The file is read here, at most
        256 KiB, a regular file (symlinks are followed: the database links its aliases), and
        parsed from memory, so the environment's search path plays no part. Any failure is one
        :class:`ZoneError`.
        """
        if (
            not isinstance(key, str)  # pyright: ignore[reportUnnecessaryIsInstance]
            or len(key) > 64
            or not _ZONE_KEY.fullmatch(key)
            or key.rsplit("/", 1)[-1].lower() in _ZONE_REFUSED
        ):
            raise ZoneError("not a usable time zone name")
        for base in self.tz_dirs:
            try:
                fd = os.open(os.path.join(base, key), os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise ZoneError("not a regular file")
                    file = os.fdopen(fd, "rb")
                except BaseException:
                    os.close(fd)
                    raise
                with file:
                    data = file.read(_ZONE_MAX_BYTES + 1)
            except OSError:
                raise ZoneError("unreadable zone file") from None
            if len(data) > _ZONE_MAX_BYTES:
                raise ZoneError("zone file too large")
            try:
                return zoneinfo.ZoneInfo.from_file(io.BytesIO(data), key=key)
            except Exception:  # any parse failure is one outcome; its text is not kept
                raise ZoneError("not a time zone file") from None
        raise ZoneError("unknown time zone")

    def _dirs_trusted(self, directory: str) -> bool:
        """Every directory from ``exec_check_from`` down to ``directory`` (both resolved) is owned
        by the user or root and not group- or world-writable."""
        uid = os.geteuid()
        floor = os.path.realpath(self.exec_check_from)
        current = directory
        while True:
            info = os.stat(current)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (uid, 0)
                or info.st_mode & 0o022
            ):
                return False
            if current == floor or current == "/":
                return True
            parent = os.path.dirname(current)
            if parent == current:
                return True
            current = parent

    def check_exec(self, path: str) -> bool:
        """``cmd_ok``: ``path`` resolved with ``realpath`` is a regular file owned by the user or
        root, not group- or world-writable, without setuid or setgid, in directories that are
        owned by the user or root and not group- or world-writable. Runs nothing."""
        try:
            resolved = os.path.realpath(path, strict=True)
            info = os.stat(resolved)
            uid = os.geteuid()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid not in (uid, 0)
                or info.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
            ):
                return False
            return self._dirs_trusted(os.path.dirname(resolved))
        except (OSError, ValueError):
            return False

    def check_env_file(self, path: str) -> bool:
        """``cron.env_file``: by ``lstat`` a regular file (not a symlink) owned by the user with
        mode 0600, in a directory chain that passes the directory rule of :meth:`check_exec`.
        The file is never opened."""
        try:
            if not path.startswith("/"):
                return False
            info = os.lstat(path)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                return False
            return self._dirs_trusted(os.path.realpath(os.path.dirname(path)))
        except (OSError, ValueError):
            return False

    def prepare_state_db(self, *, create: bool) -> StateFiles | None:
        """Check the state directory and ``state.db`` and its side files, then return where
        SQLite may open the database; ``None`` when ``create`` is false and there is no database.

        SQLite opens a file by path and cannot use ``O_NOFOLLOW``, so every refusal happens
        here first, on descriptors: the directory is the last component checked as for the other
        state files (owned by the user, no group or world access; created 0700 under umask 077
        when ``create``), ``state.db`` is opened relative to it with ``O_NOFOLLOW`` and must be a
        regular file owned by the user, with one link, mode without group or world access
        (created 0600 when ``create``), and ``-wal``, ``-shm`` and ``-journal``, when present,
        must meet the same rules. Raises :class:`HomeError`, :class:`UnsafePathError` and
        ``OSError`` (``PermissionError`` for a file the user cannot write when ``create``).

        Accepted race: a swap of ``state.db`` between this check and SQLite's open needs write
        access to the 0700 directory, which only the user has.
        """
        directory = str(self.home_dir()).rstrip("/") + "/.local/state/notice-route"
        if create:
            handle = self.open_private_dir(directory)
        else:
            opened = self.open_config_dir(directory)
            if opened is None:
                return None
            handle = opened
        with handle:
            if os.fstat(handle.fd).st_mode & 0o077:  # open_config_dir refuses writes only
                raise UnsafePathError("mode", handle.path)
            sides = {}
            for suffix in ("-wal", "-shm", "-journal"):
                sides[suffix] = self._check_side_file(handle, "state.db" + suffix)
            try:
                file = self.open_file(
                    handle, "state.db", mode="rw" if create else "r", create=create
                )
            except FileNotFoundError:
                return None
            with file:
                info = os.fstat(file.fileno())
            shown = handle.path.rstrip("/") + "/state.db"
            if info.st_uid != os.geteuid():
                raise UnsafePathError("owner", shown)
            if info.st_mode & 0o077:
                raise UnsafePathError("mode", shown)
            if info.st_nlink != 1:
                raise UnsafePathError("not_regular", shown)
            return StateFiles(shown, wal_present=sides["-wal"])

    @staticmethod
    def _check_side_file(handle: DirHandle, name: str) -> bool:
        """Whether the SQLite side file ``name`` exists; refuse one that is not a plain private
        file of the user (checked with ``lstat``, never followed)."""
        try:
            info = os.stat(name, dir_fd=handle.fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        shown = handle.path.rstrip("/") + "/" + name
        if stat.S_ISLNK(info.st_mode):
            raise UnsafePathError("symlink", shown)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UnsafePathError("not_regular", shown)
        if info.st_uid != os.geteuid():
            raise UnsafePathError("owner", shown)
        if info.st_mode & 0o077:
            raise UnsafePathError("mode", shown)
        return True

    def read_state(self) -> StateView:
        """Read ``<home>/.local/state/notice-route/state.db`` read-only; an empty view when the
        directory or the file is missing. Raises :class:`HomeError` and :class:`StateError`.

        The queries are those of the schema in ``schema.py`` (#14); a database whose
        ``user_version`` is newer than this build, or that is not ours, is a ``StateError``.
        """
        try:
            files = self.prepare_state_db(create=False)
        except HomeError:
            raise
        except (UnsafePathError, OSError, ValueError):
            raise StateError("unsafe state.db") from None
        if files is None:
            return StateView()
        # a read-only open of a WAL database without a -wal file would create -wal and -shm;
        # such a database holds no uncheckpointed data, so it is opened immutable instead
        return self._query_state(files.path, immutable=not files.wal_present)

    def _query_state(self, db_path: str, *, immutable: bool = False) -> StateView:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.wall()))
        uri = "file:" + db_path.replace("%", "%25").replace("?", "%3f").replace("#", "%23")
        surfaced: dict[str, list[str]] = {}
        try:
            # Accepted race: a writer may create the -wal between the caller's stat and this
            # open, so immutable=1 can miss its newest rows. The output is advisory and read-only.
            options = "?mode=ro&immutable=1" if immutable else "?mode=ro"
            conn = sqlite3.connect(uri + options, uri=True, timeout=5.0)
            try:
                app = conn.execute("PRAGMA application_id").fetchone()[0]
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version == 0 and app in (0, APPLICATION_ID):
                    objects = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
                    if objects == 0:  # created, never migrated: no database yet
                        return StateView()
                if app != APPLICATION_ID:
                    raise StateError("state.db is not ours")
                if version > SCHEMA_VERSION:
                    raise StateError("state.db is newer")
                meta = {
                    str(k): v
                    for k, v in conn.execute(
                        "SELECT key, value FROM meta WHERE key IN (?, ?)",
                        ("flush_last", "flush_env_missing"),
                    )
                }
                for dest, code in conn.execute(
                    "SELECT destination, code FROM surfaced "
                    "WHERE substr(period, 1, 10) = ? ORDER BY id",
                    (day,),
                ):
                    if (
                        isinstance(dest, str)
                        and isinstance(code, str)
                        and _NOTIFIER_CODE.fullmatch(code)
                        and code not in surfaced.setdefault(dest, [])
                    ):
                        surfaced[dest].append(code)
                queue = {
                    str(d): int(n)
                    for d, n in conn.execute(
                        "SELECT d.destination, COUNT(*) FROM queue q "
                        "JOIN deliveries d ON d.id = q.delivery_id "
                        "WHERE d.destination IS NOT NULL GROUP BY d.destination"
                    )
                }
            finally:
                conn.close()
        except (sqlite3.Error, ValueError, OverflowError):
            raise StateError("state.db cannot be read") from None

        def number(value: object) -> float | None:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return None
            try:
                result = float(value)
            except ValueError:
                return None
            return result if math.isfinite(result) else None

        return StateView(
            True,
            number(meta.get("flush_last")),
            number(meta.get("flush_env_missing")),
            {d: tuple(c) for d, c in surfaced.items() if c},
            queue,
        )

    def env_present(self, name: str) -> bool:
        """Whether ``name`` is in the environment, even with an empty value."""
        return name in self.environ()

    def discard_stdout(self) -> None:
        """After a failed write to standard output, point the process's own descriptor 1 at the
        null device so the buffered text cannot fail again at interpreter exit (exit code 120).
        This redirects descriptor 1 for the rest of the process, so it is only called after a
        failed stdout write.
        Does nothing when ``sys.stdout`` is not the process's own stream (a test's capture)."""
        stream = sys.stdout
        if stream is None or stream is not sys.__stdout__:
            return
        try:
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, stream.fileno())
            finally:
                os.close(null)
        except (OSError, ValueError, AttributeError):
            pass

    def stdin_is_tty(self) -> bool:
        try:
            return os.isatty(self.stdin_fd)
        except (OSError, ValueError, OverflowError):
            return False

    def read_stdin(self, limit: int, deadline: float) -> InputRead:
        """Read standard input to end of input, at most ``limit`` bytes, until ``deadline``
        (an absolute :meth:`monotonic` value).

        The deadline is checked before every read, so a stalled or slow writer cannot hold the
        caller past it, partial data included. A closed or unreadable descriptor (a directory,
        write-only, an I/O error) is ``unreadable``; the error text is never kept. One byte past
        ``limit`` ends the read as ``too_large`` without waiting for the writer.
        """
        fd = self.stdin_fd
        poller = select.poll()
        registered = False
        try:
            poller.register(fd, select.POLLIN)
            registered = True
        except (OSError, ValueError, OverflowError):
            pass
        if not registered:
            return InputRead(b"", "unreadable")
        data = bytearray()
        while True:
            left = deadline - self.clock.monotonic()
            if left <= 0:
                return InputRead(bytes(data), "timeout")
            events = poller.poll(max(1, math.ceil(min(_INPUT_SLICE, left) * 1000)))
            if not events:
                continue
            if events[0][1] & select.POLLNVAL:
                return InputRead(b"", "unreadable")
            failed = False
            chunk = b""
            try:
                chunk = os.read(fd, min(_CHUNK, limit + 1 - len(data)))
            except BlockingIOError:
                continue
            except OSError:
                failed = True
            if failed:  # outside the handler: no context carries the OS text
                return InputRead(b"", "unreadable")
            if not chunk:
                return InputRead(bytes(data), "ok")
            data += chunk
            if len(data) > limit:
                return InputRead(bytes(data[:limit]), "too_large")

    def read_input_file(self, path: str, limit: int) -> InputRead:
        """Read the ``--file`` ``path``, at most ``limit`` bytes, with no deadline.

        Symlinks are followed (the interface says a symlink to a regular file is fine). The file
        is opened ``O_NONBLOCK`` (a FIFO never blocks the open) and ``O_NOCTTY`` and checked on
        the descriptor to be a regular file before any byte is read, so a FIFO, directory,
        device or socket is :class:`InputFileError` ``not_regular``. Any open or read failure is
        ``unreadable``. Neither error names the path.
        """
        fd = -1
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY)
        except (OSError, ValueError):
            pass
        if fd < 0:  # outside the handler: no context carries the path
            raise InputFileError("unreadable")
        try:
            return self._read_regular(fd, limit)
        finally:
            os.close(fd)

    @staticmethod
    def _read_regular(fd: int, limit: int) -> InputRead:
        failed = False
        regular = False
        data = bytearray()
        try:
            regular = stat.S_ISREG(os.fstat(fd).st_mode)
            while regular and len(data) <= limit:
                chunk = os.read(fd, min(_CHUNK, limit + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
        except OSError:
            failed = True
        if failed:
            raise InputFileError("unreadable")
        if not regular:
            raise InputFileError("not_regular")
        if len(data) > limit:
            return InputRead(bytes(data[:limit]), "too_large")
        return InputRead(bytes(data), "ok")

    @staticmethod
    def _live(handle: DirHandle) -> None:
        if handle.closed:
            raise ValueError("directory handle is closed")


def _last_component_exists(path: str, dir_fd: int | None) -> bool:
    """Whether ``path`` itself exists (a symlink or file), so that ENOTDIR came from it and not
    from a component on the way."""
    try:
        os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return True


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
