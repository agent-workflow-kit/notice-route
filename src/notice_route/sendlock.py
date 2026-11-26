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
  symlink, FIFO, directory or other file that is not a regular file, an unusable ``locks``
  directory and any ``OSError`` (``ENOLCK``, permissions) make the call ``lock_busy``
  (``acquired`` False, ``busy_reason`` ``unsafe`` or ``error``): nothing is sent. A bad
  destination name or an unknown kind is a ``ValueError`` (a programming error, raised before
  anything is opened: validation allows only ``[a-z0-9][a-z0-9_-]{0,31}`` and the kinds of
  ``validate.KINDS``).
- The lock is released on every path (return, exception, ``KeyboardInterrupt``) by unlocking and
  closing the descriptor; the kernel releases it too when the process dies. The descriptor is
  close-on-exec, so a notifier child never holds it. Lock files are never renamed or unlinked.

``send_lock`` takes an optional ``cancel`` callback (``flush`` passes one that is true after a
trapped signal): the wait then ends at once with ``busy_reason`` ``cancelled`` and the caller
makes no call. With ``None`` (``send``) the behavior is unchanged.

On ``acquired`` False the pipeline records :data:`notice_route.outcome.LOCK_BUSY` for the row
(requeue 60 s later, no attempt, hourly count given back with ``count_hour``).
``send.py`` (#22) wraps its :func:`~notice_route.notify.call_notifier` call with this.
``flush`` (#23) does the same for each row it sends; :func:`flush_lock` is its own lock.
``validate --probe`` (#29, ``probe.py``) calls a notifier under the same lock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Literal

from .gateway import DirHandle, Gateway, UnsafePathError
from .validate import _DESTINATION_NAME, KINDS  # the one grammar of destination names

__all__ = ["LOCKS_DIR", "SHARED_LOCK_NAME", "Held", "flush_lock", "lock_name", "send_lock"]

LOCKS_DIR = "locks"
SHARED_LOCK_NAME = "_wsl"
BusyReason = Literal["timeout", "unsafe", "error", "cancelled"]


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
    gateway: Gateway,
    state: DirHandle,
    destination: str,
    kind: str,
    lock_wait: float,
    cancel: Callable[[], bool] | None = None,
) -> Iterator[Held]:
    """Take the send lock of ``destination`` (``state`` is the open state directory) for the
    body of the ``with`` block, waiting at most ``lock_wait`` seconds. See the module doc.
    ``cancel`` (default ``None``: the wait is unchanged) ends the wait early with ``busy_reason``
    ``cancelled`` when it returns true (``flush`` passes it to end the wait on a signal)."""
    if not isinstance(destination, str) or not _DESTINATION_NAME.fullmatch(destination):
        raise ValueError("the destination name is not a valid destination name")
    if kind not in KINDS:
        raise ValueError("the destination kind is unknown")
    with _flock(gateway, state, f"{lock_name(destination, kind)}.lock", lock_wait, cancel) as held:
        yield held


@contextmanager
def flush_lock(gateway: Gateway, state: DirHandle, destination: str) -> Iterator[Held]:
    """Take the flush lock of ``destination`` (``locks/<name>.flush``) without waiting (#23): one
    ``flush`` per destination at a time. ``acquired`` False with ``busy_reason`` ``timeout`` means
    another flusher holds it; ``unsafe`` and ``error`` mean the lock cannot be used. The kernel
    releases it when the holder dies."""
    if not isinstance(destination, str) or not _DESTINATION_NAME.fullmatch(destination):
        raise ValueError("the destination name is not a valid destination name")
    with _flock(gateway, state, f"{destination}.flush", 0) as held:
        yield held


@contextmanager
def _flock(
    gateway: Gateway,
    state: DirHandle,
    file_name: str,
    wait: float,
    cancel: Callable[[], bool] | None = None,
) -> Iterator[Held]:
    locks: DirHandle | None = None
    file = None
    held = False
    try:
        try:
            locks = gateway.open_private_subdir(state, LOCKS_DIR)
            file = gateway.open_file(locks, file_name, mode="a", create=True)
            held = gateway.try_lock(file, wait, cancel) if cancel else gateway.try_lock(file, wait)
            if held:
                result = Held(True)
            else:
                cancelled = cancel is not None and cancel()
                result = Held(False, "cancelled" if cancelled else "timeout")
        except UnsafePathError:
            result = Held(False, "unsafe")
        except OSError:
            result = Held(False, "error")
        yield result
    finally:
        try:
            if file is not None:
                try:
                    if held:
                        with suppress(OSError):
                            gateway.unlock(file)
                finally:
                    with suppress(OSError):
                        file.close()
        finally:
            if locks is not None:
                with suppress(OSError):
                    locks.close()
