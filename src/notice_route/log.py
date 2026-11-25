"""The public log, ``log.jsonl`` (docs/interface.md, Log; schemas/log.json).

One line per destination outcome (``record`` ``delivery``) and per ``flush`` (``record``
``flush``, built by #23 with :func:`record`). A line holds only fixed words, validated names, the
first 16 hex characters of two SHA-256 digests of the rendered text and of the dedupe digest, and
numbers: never a title, a message, a link or a credential. It is written ASCII-only, so no
character of a payload can change its shape.

Choices where the interface is silent:

- ``log.lock`` is owned here (docs/design.md C-52). It is an exclusive ``flock`` taken for the
  length of one append (and the rotation it may trigger), polled for at most :data:`LOCK_WAIT_S`
  seconds. ``send``, ``flush`` and ``prune`` all write through :class:`LogWriter`, so one lock
  orders every append and every rotation; a writer that cannot take it in time skips its line.
- Rotation happens before the append that would find ``log.jsonl`` at :data:`MAX_BYTES` or more:
  ``log.2`` becomes ``log.3`` (the old ``log.3`` is deleted), ``log.1`` becomes ``log.2``,
  ``log.jsonl`` becomes ``log.1``, then a new ``log.jsonl`` is started.
- A problem (a bad or unwritable ``log.jsonl``, a busy lock, any ``OSError``) is counted in
  :attr:`LogWriter.problems`, never raised: the caller prints one warning and the result of the
  command is unchanged.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from . import __version__
from .gateway import DirHandle, Gateway, UnsafePathError

__all__ = [
    "KEEP",
    "LOCK_NAME",
    "LOCK_WAIT_S",
    "LOG_NAME",
    "MAX_BYTES",
    "LogWriter",
    "record",
    "sha16",
    "timestamp",
]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
LOG_NAME = "log.jsonl"
LOCK_NAME = "log.lock"
MAX_BYTES = 10 * 1024 * 1024
KEEP = 3  # log.1 to log.3
LOCK_WAIT_S = 0.5

_FIELDS = (
    "source",
    "hook",
    "rule",
    "destination",
    "level",
    "outcome",
    "reason",
    "code",
    "attempts",
    "title_sha256",
    "message_sha256",
    "event_id",
    "dedupe_key_sha256",
    "due",
    "sent",
    "queued_retry",
    "failed",
    "unknown",
    "dropped",
    "expired",
    "duration_ms",
)


def sha16(text: str) -> str:
    """The first 16 hex characters of the SHA-256 of the UTF-8 text (a lone surrogate, which the
    router never renders, is replaced so that logging can never fail on it)."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def timestamp(wall: float) -> str:
    """UTC with millisecond precision: ``YYYY-MM-DDTHH:MM:SS.mmmZ``."""
    stamp = _EPOCH + timedelta(seconds=wall)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def record(
    kind: str, *, wall: float, actor: str, decision_id: str | None, **fields: Any
) -> dict[str, Any]:
    """One log record with every key of schemas/log.json (absent fields are null)."""
    unknown = set(fields) - set(_FIELDS)
    if unknown:
        raise ValueError(f"unknown log field {sorted(unknown)[0]!r}")
    out: dict[str, Any] = {
        "ts": timestamp(wall),
        "tool": "notice-route",
        "version": __version__,
        "record": kind,
    }
    out.update(dict.fromkeys(_FIELDS))
    out.update(fields)
    out["actor"] = actor
    out["decision_id"] = decision_id
    return out


class LogWriter:
    """Appends records to ``log.jsonl`` in the state directory ``state``.

    ``state`` is an open :class:`~notice_route.gateway.DirHandle` or ``None`` (then nothing is
    written, as when the config says ``log: false``). :attr:`problems` counts the lines that could
    not be written."""

    def __init__(self, gateway: Gateway, state: DirHandle | None) -> None:
        self._gateway = gateway
        self._state = state
        self.problems = 0

    def write(self, rec: dict[str, Any]) -> bool:
        """Append one record. ``False`` (and a problem counted) when it could not be written."""
        if self._state is None:
            return True
        line = (json.dumps(rec, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
        try:
            if self._append(self._state, line):
                return True
        except (UnsafePathError, OSError, ValueError):
            pass
        self.problems += 1
        return False

    def _append(self, state: DirHandle, line: bytes) -> bool:
        gateway = self._gateway
        with gateway.open_file(state, LOCK_NAME, mode="a", create=True) as lock:
            if not gateway.try_lock(lock, LOCK_WAIT_S):
                return False
            try:
                log = gateway.open_file(state, LOG_NAME, mode="a", create=True)
                try:
                    if gateway.file_size(log) >= MAX_BYTES:
                        log.close()
                        self._rotate(state)
                        log = gateway.open_file(state, LOG_NAME, mode="a", create=True)
                    view = memoryview(line)
                    while view:
                        written = log.write(view)
                        if not written:
                            return False
                        view = view[written:]
                finally:
                    log.close()
                return True
            finally:
                gateway.unlock(lock)

    def _rotate(self, state: DirHandle) -> None:
        gateway = self._gateway
        gateway.remove_in(state, f"log.{KEEP}")
        for number in range(KEEP - 1, 0, -1):
            try:
                gateway.rename_in(state, f"log.{number}", f"log.{number + 1}")
            except FileNotFoundError:
                pass
        gateway.rename_in(state, LOG_NAME, "log.1")
