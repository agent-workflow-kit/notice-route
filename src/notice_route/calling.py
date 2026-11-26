"""The call path shared by ``send`` (#22) and ``flush`` (#23): the send lock, the signal gate and
one notifier call, run in a thread that only calls (the main thread records: a SQLite connection
is not shared between threads).

``call_row`` takes the send lock, calls once and releases the lock before it returns, so the
caller records the result with no lock held and no transaction open inside it. Anything that
escapes the call is :class:`Escaped` (the child's fate is unknown, the row stays claimed); an
exception from the lock itself, before any call started, is :class:`NotRun` (nothing was sent).
The text of an exception is never kept.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from .gateway import DirHandle, Gateway, SignalTrap
from .notify import CallResult
from .queue import QueueRow
from .sendlock import send_lock
from .validate import Destination

__all__ = [
    "Busy",
    "CallEnd",
    "Called",
    "Escaped",
    "Gate",
    "NotRun",
    "Skipped",
    "call_row",
]


@dataclass(frozen=True)
class Called:
    result: CallResult


@dataclass(frozen=True)
class Busy:
    """The send lock was not free within ``lock_wait``: no call was made."""


@dataclass(frozen=True)
class Escaped:
    """An exception escaped the call: the child's fate is unknown."""


@dataclass(frozen=True)
class Skipped:
    """A signal came before this call started: it was not made."""


@dataclass(frozen=True)
class NotRun:
    """Something failed before the call started (the send lock itself): nothing was sent, no
    attempt counts, and the command ends 1 (internal)."""


CallEnd = Called | Busy | Escaped | Skipped | NotRun


class Gate:
    """Shared by the call threads of one command. ``send`` (``strict`` false): the first call to
    start closes the gate for signals, so a thread that gets its lock after a signal and before
    any call started makes none. ``flush`` (``strict`` true): after a signal no new call starts at
    all (the calls in progress finish). ``started`` says whether any call started."""

    def __init__(self, trap: SignalTrap, *, strict: bool) -> None:
        self._trap = trap
        self._strict = strict
        self._lock = threading.Lock()
        self.started = False

    def begin(self) -> bool:
        with self._lock:
            if self._trap.received is not None and (self._strict or not self.started):
                return False
            self.started = True
            return True


def call_row(
    gateway: Gateway,
    dest: Destination,
    row: QueueRow,
    state: DirHandle,
    lock_wait: float,
    dedupe: str | None,
    call: Callable[..., CallResult],
    gate: Gate,
    cancel: Callable[[], bool] | None = None,
) -> CallEnd:
    call_started = False
    try:
        with send_lock(gateway, state, dest.name, dest.kind, lock_wait, cancel) as held:
            if not held.acquired:
                # a wait that a signal ended (``flush`` only) made no call, like a gate refusal
                return Skipped() if held.busy_reason == "cancelled" else Busy()
            if not gate.begin():
                return Skipped()
            call_started = True
            result = call(
                gateway,
                dest,
                title=row.title or "",
                message=row.message,
                level=row.level,
                link=row.link or "",
                dedupe_key=dedupe,
            )
        return Called(result)
    except BaseException:  # the text of an exception is never kept; SystemExit included
        # an exception from the lock before any call started is no escaped call: no attempt
        return Escaped() if call_started else NotRun()
