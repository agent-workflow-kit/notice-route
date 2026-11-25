"""The send lock: one call at a time per destination (#21).

``send_lock`` wraps one notifier call::

    with send_lock(gateway, state, destination.name, destination.kind, config.lock_wait) as held:
        if held.acquired:
            result = call_notifier(...)
    # the lock is free here, before the result is recorded

Rules (interface: Calling the notifiers, Files):

- The lock is an exclusive ``flock`` on ``locks/<name>.lock`` in the state directory, taken by
  polling every 50 ms on the monotonic clock until ``lock_wait`` has passed
  (:meth:`Gateway.try_lock`); a wall-clock step changes nothing.
- Every ``wsl-notice`` destination uses the one lock ``locks/_wsl.lock`` *instead of* its own (no
  destination name starts with ``_``, so the names cannot collide). A call therefore never holds
  two locks, so two processes cannot take them in opposite orders and deadlock.
- The files are opened by :meth:`Gateway.open_file` (``O_NOFOLLOW``, ``O_NONBLOCK``, ``O_CLOEXEC``,
  regular file only, created 0600) inside the ``locks`` directory (private, 0700, checked). A
  symlink, FIFO, directory or other bad file, an unusable ``locks`` directory and any ``OSError``
  (``ENOLCK``, permissions) make the call ``lock_busy`` (``acquired`` False, ``busy_reason``
  ``unsafe`` or ``error``): nothing is sent. A bad destination name is a ``ValueError`` (a
  programming error: validation allows only ``[a-z0-9][a-z0-9_-]*``).
- The lock is released on every path (return, exception, ``KeyboardInterrupt``) by unlocking and
  closing the descriptor; the kernel releases it too when the process dies. The descriptor is
  close-on-exec, so a notifier child never holds it. Lock files are never renamed or unlinked.

On ``acquired`` False the pipeline records :data:`notice_route.outcome.LOCK_BUSY` for the row
(requeue 60 s later, no attempt, hourly count given back with ``count_hour``).
TODO(#22): the send pipeline wraps its :func:`~notice_route.notify.call_notifier` call with this.
TODO(#23): ``flush`` does the same for each row it sends (and for a digest).
TODO(#29): ``validate --probe`` calls a notifier under the same lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Literal

from .gateway import DirHandle, Gateway, UnsafePathError

__all__ = ["LOCKS_DIR", "SHARED_LOCK_NAME", "Held", "lock_name", "send_lock"]

LOCKS_DIR = "locks"
SHARED_LOCK_NAME = "_wsl"
BusyReason = Literal["timeout", "unsafe", "error"]


@dataclass(frozen=True)
class Held:
    """The result of asking for the lock: ``acquired`` or the reason it is busy."""

    acquired: bool
    busy_reason: BusyReason | None = None


def lock_name(destination: str, kind: str) -> str:
    """The lock file's base name: the shared one for ``wsl-notice``, else the destination's."""
    return SHARED_LOCK_NAME if kind == "wsl-notice" else destination


@contextmanager
def send_lock(
    gateway: Gateway, state: DirHandle, destination: str, kind: str, lock_wait: float
) -> Iterator[Held]:
    """Take the send lock of ``destination`` (``state`` is the open state directory) for the
    body of the ``with`` block, waiting at most ``lock_wait`` seconds. See the module doc."""
    file_name = f"{lock_name(destination, kind)}.lock"
    locks: DirHandle | None = None
    file = None
    held = False
    try:
        try:
            locks = gateway.open_private_subdir(state, LOCKS_DIR)
            file = gateway.open_file(locks, file_name, mode="a", create=True)
            held = gateway.try_lock(file, lock_wait)
            result = Held(True) if held else Held(False, "timeout")
        except UnsafePathError:
            result = Held(False, "unsafe")
        except OSError:
            result = Held(False, "error")
        yield result
    finally:
        if file is not None:
            if held:
                with suppress(OSError):
                    gateway.unlock(file)
            with suppress(OSError):
                file.close()
        if locks is not None:
            locks.close()
