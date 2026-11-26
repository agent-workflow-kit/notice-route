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
  ``os.system``, the ``os.exec*`` family and ``os.posix_spawn(p)``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
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


class RealCrontabAttempt(BaseException):  # noqa: N818 - must not be caught by ``except Exception``
    """Something tried to run a crontab that is not a registered fake."""


class Guard:
    def __init__(self) -> None:
        self.allowed: set[str] = set()  # realpaths of the fake stubs
        self.attempts: list[str] = []  # what was refused
        self.expected = False  # the guard's own test sets this
        self.base_popen: type[Any] = subprocess.Popen  # what the guard lets a call through to

    def allow(self, stub: str | os.PathLike[str]) -> None:
        self.allowed.add(os.path.realpath(stub))

    def check(self, program: object, search_path: str | None = None) -> None:
        """Refuse ``program`` when it is a crontab that is not a registered fake."""
        try:
            name = os.fsdecode(program)  # type: ignore[arg-type]
        except TypeError:
            return
        real = _resolve(name, search_path)
        by_name = os.path.basename(name) == "crontab" or os.path.basename(real) == "crontab"
        if real in self.allowed:
            return
        if by_name or real in SYSTEM_CRONTABS:
            self.attempts.append(name)
            raise RealCrontabAttempt(f"refused to run a crontab that is not a test fake: {name!r}")


def _resolve(name: str, search_path: str | None) -> str:
    if "/" in name:
        return os.path.realpath(name)
    found = shutil.which(name, path=search_path if search_path is not None else os.environ.get("PATH"))
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

    class GuardedPopen(original_popen):  # type: ignore[type-arg,misc]
        def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
            env = kwargs.get("env")
            search = env.get("PATH") if env is not None else None
            program = _argv_program(args, kwargs.get("executable"))
            if kwargs.get("shell") and isinstance(args, str):
                program = args.split()[0] if args.split() else ""
            guard.check(program, search)
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)

    def guarded_system(command: Any) -> int:
        words = os.fsdecode(command).split()
        for word in words:
            guard.check(word)
        raise AssertionError("os.system is not used by the router or its tests")

    monkeypatch.setattr(os, "system", guarded_system)

    def wrap_exec(name: str) -> None:
        real = getattr(os, name)

        def guarded(path: Any, *args: Any, **kwargs: Any) -> Any:
            guard.check(path)
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, name, guarded)

    for name in ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe"):
        if hasattr(os, name):
            wrap_exec(name)

    def wrap_spawn(name: str) -> None:
        real: Callable[..., Any] = getattr(os, name)

        def guarded(path: Any, *args: Any, **kwargs: Any) -> Any:
            guard.check(path)
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, name, guarded)

    for name in ("posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            wrap_spawn(name)


def private_path(fake_dir: Path, current: str) -> str:
    """``PATH`` for a test: the fake's directory first, then what was there."""
    return f"{fake_dir}:{current}" if current else str(fake_dir)
