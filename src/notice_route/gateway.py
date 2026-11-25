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
import secrets
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "Clock",
    "FakeClock",
    "FixedTokens",
    "Gateway",
    "SystemClock",
    "SystemTokens",
    "TokenSource",
]


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
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``argv`` without a shell, capturing text stdout and stderr.

        PLACEHOLDER, unsafe for notifier calls: it raises raw ``subprocess`` exceptions
        (``TimeoutExpired``, ``OSError``), reads unbounded output into memory and starts no
        process group of its own. Issue #4 replaces it.
        """
        # TODO(#4): own process group, bounded reader, monotonic deadline, group TERM then KILL.
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    # TODO(#5): flock helpers and filesystem helpers (directories, files, home lookup).
