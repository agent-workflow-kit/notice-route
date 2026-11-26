"""A guard that keeps every test away from the real ``crontab`` program.

The suite must never read or write the user's real crontab, so every test runs under this guard
(``tests/conftest.py``, an autouse fixture):

- ``crontab`` is allowed only when its resolved path is a stub the test registered (the fake of
  ``tests/fakecron.py``). Any other program whose base name is ``crontab``, or whose resolved
  path (by ``realpath``, so a symlink or ``..`` does not help) is a system ``crontab``, is
  refused before anything starts: a :class:`RealCrontabAttempt` (a ``BaseException``, so no
  ``except Exception`` of the code under test swallows it) is raised and the attempt is
  recorded. The fixture fails the test at teardown when an attempt was recorded and not
  expected.
- The guard sits in front of ``subprocess.Popen`` (so ``subprocess.run`` and the gateway),
  the ``os.exec*`` and ``os.spawn*`` families and ``os.posix_spawn(p)``; ``os.system`` is
  refused for every command. A bare name is searched the way the call itself would search it
  (``os.get_exec_path`` of the child's ``env`` except for ``posix_spawnp``, which uses the
  parent's ``PATH``, so a child ``env`` without ``PATH`` uses the
  default path, never the parent's). With ``shell=True`` and for an interpreter or launcher
  (``sh``, ``bash``, ``dash``, ``zsh``, ``env``, ``xargs``, ``python*`` and the wrappers
  ``timeout``, ``nohup``, ``nice``, ``setsid``, ``flock``, ``time``, ``sudo``, ``doas``,
  ``ionice``, ``stdbuf``, ``chroot``, ``command``, ``exec``, ``busybox``) every word of every
  argument is checked too.
- The real start functions are kept in ``Guard.real`` (a table the wrappers call at call
  time), so a test of the guard replaces the entries with recorders and can never reach the
  real function if the guard regresses.

What the guard does NOT cover: it lives in this interpreter only. A child process is not
guarded (one that resets ``PATH``, uses an absolute path or an empty environment escapes;
today no test child can reach cron). Also unguarded: any other wrapper or interpreter not
listed above (``perl``, ``ruby``, ``node``, ``awk``, ``find -exec``, ``strace``, ...),
``ctypes``/``cffi`` calls, ``_posixsubprocess.fork_exec``, a copied or hard-linked ``crontab``
binary (its realpath is not a system path and its name may differ)
and code that runs at import time before the fixture.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

# Looked up once, at import, before any test changes PATH: the places a real crontab lives.
SYSTEM_CRONTABS = frozenset(
    {
        os.path.realpath(p)
        for p in (
            "/usr/bin/crontab",
            "/bin/crontab",
            "/usr/local/bin/crontab",
            "/usr/sbin/crontab",
            "/sbin/crontab",
            "/opt/homebrew/bin/crontab",
            shutil.which("crontab") or "/usr/bin/crontab",
        )
    }
)

LAUNCHERS = frozenset(
    {
        "sh", "bash", "dash", "zsh", "env", "xargs",
        "timeout", "nohup", "nice", "setsid", "flock", "time", "sudo", "doas",
        "ionice", "stdbuf", "chroot", "command", "exec", "busybox",
    }
)  # fmt: skip
_WORD_BREAKS = re.compile(r"[\s;&|()<>`'\"$]+")


class RealCrontabAttempt(BaseException):  # noqa: N818 - must not be caught by ``except Exception``
    """Something tried to run a crontab that is not a registered fake."""


class Guard:
    def __init__(self) -> None:
        self.allowed: set[str] = set()  # realpaths of the fake stubs
        self.attempts: list[str] = []  # what was refused
        self.expected = False  # the guard's own test sets this
        # the real start functions a call is let through to, by name; ``install`` fills it
        self.real: dict[str, Callable[..., Any]] = {}

    def allow(self, stub: str | os.PathLike[str]) -> None:
        self.allowed.add(os.path.realpath(stub))

    def _refuse(self, name: str) -> None:
        self.attempts.append(name)
        raise RealCrontabAttempt(f"refused to run a crontab that is not a test fake: {name!r}")

    def check(self, program: object, search: Sequence[str] | None = None) -> None:
        """Refuse ``program`` when it is a crontab that is not a registered fake. ``search`` is
        the list of directories a bare name is looked up in (default: this process's)."""
        try:
            name = os.fsdecode(program)  # type: ignore[arg-type]
        except TypeError:
            return
        real = _resolve(name, search)
        by_name = os.path.basename(name) == "crontab" or os.path.basename(real) == "crontab"
        if real in self.allowed:
            return
        if by_name or real in SYSTEM_CRONTABS:
            self._refuse(name)

    def check_words(self, text: object, search: Sequence[str] | None = None) -> None:
        """Check every word of a command line or an interpreter argument."""
        try:
            raw = os.fsdecode(text)  # type: ignore[arg-type]
        except TypeError:
            return
        try:
            words = shlex.split(raw)
        except ValueError:
            words = raw.split()
        for word in [raw, *words]:
            for piece in _WORD_BREAKS.split(word):
                if piece and ("/" in piece or os.path.basename(piece) == "crontab"):
                    self.check(piece, search)

    def check_launch(
        self, program: object, argv: Sequence[object], search: Sequence[str] | None
    ) -> None:
        self.check(program, search)
        try:
            base = os.path.basename(os.path.realpath(_resolve(os.fsdecode(program), search)))  # type: ignore[arg-type]
        except TypeError:
            return
        if base in LAUNCHERS or base.startswith("python"):
            for arg in argv[1:]:
                self.check_words(arg, search)


def _exec_dirs(env: Mapping[Any, Any] | None) -> list[str]:
    """The directories ``subprocess`` and ``exec*p`` search: ``os.get_exec_path(env)``."""
    try:
        return os.get_exec_path(env)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return os.get_exec_path({})


def _resolve(name: str, search: Sequence[str] | None) -> str:
    if "/" in name:
        return os.path.realpath(name)
    dirs = list(search) if search is not None else _exec_dirs(None)
    found = shutil.which(name, path=os.pathsep.join(d for d in dirs if d))
    return os.path.realpath(found) if found else name


def _argv_program(args: object, executable: object) -> object:
    if executable is not None:
        return executable
    if isinstance(args, (str, bytes, os.PathLike)):
        return args  # a single command string: its first word is checked below
    seq: Sequence[Any] = args  # type: ignore[assignment]
    return seq[0] if seq else ""


def install(monkeypatch: pytest.MonkeyPatch, guard: Guard) -> None:
    original_popen = subprocess.Popen
    guard.real["Popen.__init__"] = original_popen.__init__

    class GuardedPopen(original_popen):  # type: ignore[type-arg,misc]
        def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
            search = _exec_dirs(kwargs.get("env"))
            if kwargs.get("shell"):
                words = (
                    args
                    if isinstance(args, (str, bytes, os.PathLike))
                    else " ".join(os.fsdecode(a) for a in args)
                )
                guard.check_words(words, search)
            else:
                program = _argv_program(args, kwargs.get("executable"))
                argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
                guard.check_launch(program, argv, search)
            guard.real["Popen.__init__"](self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)

    def guarded_system(command: Any) -> int:
        guard.check_words(command)
        raise AssertionError("os.system is refused: the router and its tests never use it")

    monkeypatch.setattr(os, "system", guarded_system)

    def wrap(name: str, *, skip: int = 0) -> None:
        """Wrap ``os.<name>``; ``skip`` leading arguments (the spawn mode) precede the path."""
        guard.real[name] = getattr(os, name)
        letter_e = name.endswith("e") or name.endswith("pe")
        suffix = name.removeprefix("exec").removeprefix("spawn").removeprefix("posix_spawn")
        searched = "p" in suffix

        def guarded(*args: Any, **kwargs: Any) -> Any:
            path = args[skip]
            rest = args[skip + 1 :]
            env: Mapping[Any, Any] | None = None
            if "v" in name or name.startswith("posix_spawn"):
                argv = list(rest[0]) if rest else []
                if letter_e and len(rest) > 1:
                    env = rest[1]
                if name.startswith("posix_spawn") and len(rest) > 1:
                    env = rest[1]
            else:
                argv = list(rest[:-1] if letter_e and rest else rest)
                if letter_e and rest:
                    env = rest[-1]
            if name == "posix_spawnp":
                search: list[str] | None = _exec_dirs(None)  # it searches the parent's PATH
            else:
                search = _exec_dirs(env) if searched else None
            guard.check_launch(path, argv or [path], search)
            return guard.real[name](*args, **kwargs)

        monkeypatch.setattr(os, name, guarded)

    for name in ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe"):
        if hasattr(os, name):
            wrap(name)
    for name in (
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
    ):
        if hasattr(os, name):
            wrap(name, skip=1)
    for name in ("posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            wrap(name)


def private_path(fake_dir: Path, current: str) -> str:
    """``PATH`` for a test: the fake's directory first, then what was there."""
    return f"{fake_dir}:{current}" if current else str(fake_dir)
