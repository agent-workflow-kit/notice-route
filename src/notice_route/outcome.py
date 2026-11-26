"""Classify one notifier call and record its outcome (#20).

docs/interface.md (Retries and outcomes) is normative: the first row of the table that applies
decides the outcome of one call. Two halves:

- :func:`classify` is pure. A :class:`~notice_route.notify.CallResult` goes in, an
  :class:`Outcome` comes out. Only fixed words, the pattern-checked ``error.code`` and
  ``error.reason`` and a pattern-checked ``notifier_outcome`` are ever taken from a notifier's
  stdout; stderr is never read (the call already emptied it). Free text (``message``,
  ``details``, anything else) is never kept, so hostile output cannot reach the log or history.
- :func:`apply_outcome` (inside a ``Store.write`` block) and :func:`record_outcome` (which opens
  it) write the outcome to ``deliveries`` and the queue in one transaction, fenced by the claim
  token. A recorder that lost its claim (taken over, evicted) changes nothing and gets
  ``applied`` false: its result goes to the log only.

Choices where the interface is silent (the narrower reading):

- The exit code picks the row; the envelope only supplies the flags and codes. An envelope that
  disagrees with the exit code (an error envelope on exit 0 or 22, flags on exit 20) is ignored.
- The flags are read on exit 12 and 1 and both must be present booleans; exit 20 reads no flag
  (the notifiers send null there). ``error.code`` must match ``[a-z][a-z0-9_]{0,63}`` and, on
  exit 20, so must ``error.reason``; otherwise the envelope is unparseable. A well-shaped code the
  router has never heard of is used as it is and the flags decide (forward compatibility).
- Exit 22 is ``disabled`` whatever stdout holds; ``notifier_outcome`` is null when it is unreadable
  (nothing is invented).
- ``lost`` (the child ran, its fate is unknown, the group was killed) is ``unknown``, code
  ``killed``. ``not_started`` is an internal fault: nothing is recorded, the claim lapses and
  ``flush`` resends (no blind retry). ``env_missing`` found at call time requeues the row
  ``deferred``/``env_missing`` with no attempt counted; a stored refusal reason
  (``link_refused``/``keyword_refused``) is kept, so a later refusal still ends ``failed``.
- A refusal retry (rows 13 and 14) follows the retry schedule like any other retry (the interface
  names no exception to it) and is bounded by its attempt cap. A second refusal and a link refusal
  of a row that has no link (nothing can cure it) end ``failed`` with the refusal reason as code;
  these checks come before the attempt cap, so the code is never ``retries_exhausted`` for them.
- A requeued row is always a ``single`` row (a ``held`` row that is retried or deferred is not held
  any more). ``env_missing`` never overwrites a stored refusal reason.
- ``not_started`` records nothing, so the hourly count it took stays (nothing is known about whether
  the call ran; the 24 h expiry and the cap bound it).
- ``reason`` of a delivery is left as it was for outcomes that set none (a ``sent`` after
  ``quiet`` keeps ``quiet``), which also keeps "already link_refused" true on later calls.

The retry schedule and attempt caps are :mod:`notice_route.retry`; ``retry_at`` is the seam.
The caller inserts the ``surfaced`` row for ``failed`` and ``unknown`` ends (``surfacing.py``).
``send.py`` (#22) logs the result of a call that was not applied, and treats an exception escaping
``call_notifier`` through :func:`record_escaped`.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from . import dedup
from .decide import _sha16, give_back
from .gateway import STDOUT_LIMIT, Gateway
from .notify import CallResult
from .payload import StrictJsonError, parse_json_object
from .queue import Evicted, Lease, QueueRow, complete_row, requeue_row
from .store import Store

__all__ = [
    "LOCK_BUSY",
    "LOCK_BUSY_RETRY_S",
    "MESSAGE_CHARS",
    "Applied",
    "Outcome",
    "apply_outcome",
    "classify",
    "record_escaped",
    "record_outcome",
]

MESSAGE_CHARS = 4000
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_NOTIFIER_OUTCOME = re.compile(r"[a-z_]{1,32}")
_LINK_REASONS = ("link_invalid", "link_scheme")
_KEYWORD_REASON = "command_keyword"
_REFUSED = ("link_refused", "keyword_refused")
_SIGNAL_EXITS = range(129, 160)

OutcomeName = Literal["sent", "failed", "unknown", "queued_retry", "disabled", "deferred"]
Refusal = Literal["link_refused", "keyword_refused"]
_TERMINAL = ("sent", "failed", "unknown", "disabled")
_RELEASING = ("failed", "disabled")  # the ends that release the dedup records

_Conn = sqlite3.Connection


@dataclass(frozen=True)
class Outcome:
    """The classification of one call.

    ``outcome`` is None for an internal fault (nothing to record). ``code`` is a router code or a
    pattern-checked notifier code. ``reason`` is set only for ``deferred``/``env_missing``.
    ``refusal`` marks a ``queued_retry`` that rewrites the row. ``counted`` says the call counts
    as an attempt.
    """

    outcome: OutcomeName | None
    code: str | None = None
    reason: str | None = None
    notifier_outcome: str | None = None
    refusal: Refusal | None = None
    counted: bool = True

    @property
    def internal(self) -> bool:
        return self.outcome is None


LOCK_BUSY_RETRY_S = 60
LOCK_BUSY = Outcome("queued_retry", None, "lock_busy", counted=False)
"""A call never made because the send lock was not free within ``lock_wait`` (#21): the row goes
back ``queued_retry``/``lock_busy`` with code null, no attempt counted, its hourly count given back
(when the caller passes ``count_hour``) and ``deliver_after`` 60 seconds later."""


def _out(
    outcome: OutcomeName,
    code: str | None = None,
    *,
    notifier_outcome: str | None = None,
    refusal: Refusal | None = None,
) -> Outcome:
    return Outcome(outcome, code, None, notifier_outcome, refusal)


def _object(stdout: bytes) -> dict[str, Any] | None:
    try:
        return parse_json_object(stdout, max_bytes=STDOUT_LIMIT)
    except StrictJsonError:
        return None


def _notifier_outcome(stdout: bytes) -> str | None:
    found = _object(stdout)
    value = None if found is None else found.get("outcome")
    if isinstance(value, str) and _NOTIFIER_OUTCOME.fullmatch(value):
        return value
    return None


@dataclass(frozen=True)
class _Envelope:
    code: str
    retryable: bool | None
    unknown_outcome: bool | None
    reason: str | None


def _envelope(stdout: bytes, rc: int) -> _Envelope | None:
    """The error envelope of an exit 12, 1 or 20, or None when it is not parseable."""
    found = _object(stdout)
    error = None if found is None else found.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if not isinstance(code, str) or _CODE.fullmatch(code) is None:
        return None
    if rc == 20:
        reason = error.get("reason")
        if not isinstance(reason, str) or _CODE.fullmatch(reason) is None:
            return None
        return _Envelope(code, None, None, reason)
    retryable = error.get("retryable")
    unknown = error.get("unknown_outcome")
    if type(retryable) is not bool or type(unknown) is not bool:
        return None
    return _Envelope(code, retryable, unknown, None)


def classify(result: CallResult, *, kind: str, has_dedupe_key: bool) -> Outcome:
    """The outcome of one call: the first row of the table that applies. ``kind`` is the
    destination's kind and ``has_dedupe_key`` whether the call passed ``--dedupe-key``."""
    if result.status == "not_started":
        return Outcome(None, counted=False)
    if result.status == "env_missing":
        return Outcome("deferred", None, "env_missing", counted=False)
    if result.status == "exec_failed":
        return _out("failed", "exec_failed")
    if result.status == "lost" or result.run is None:
        return _out("unknown", "killed")
    run = result.run
    if run.status == "spawn_failed":  # row 1
        return _out("failed", "exec_failed")
    if run.status == "timeout":  # row 2
        return _out("unknown", "killed")
    if run.status == "overflow":  # row 3
        return _out("unknown", "output_overflow")
    if run.status == "signaled":  # row 4
        return _out("unknown", "signal")
    rc = run.returncode
    if run.status != "exited" or type(rc) is not int:  # not reachable from the gateway
        return _out("unknown", "signal")
    if kind == "generic":  # rows 5 and 6; stdout is never read
        return _out("sent") if rc == 0 else _out("failed", "generic_failed")
    stdout = run.stdout
    if rc == 0:  # row 7
        return _out("sent", notifier_outcome=_notifier_outcome(stdout))
    if rc == 22:  # row 8
        return _out("disabled", notifier_outcome=_notifier_outcome(stdout))
    if rc in (12, 20, 1):
        return _classify_envelope(rc, _envelope(stdout, rc), kind, has_dedupe_key)
    if rc in _SIGNAL_EXITS:  # row 18
        return _out("queued_retry" if stdout == b"" else "unknown", "signal")
    return _out("failed", "deployment_fault")  # row 19


def _classify_envelope(rc: int, env: _Envelope | None, kind: str, has_dedupe_key: bool) -> Outcome:
    if env is None:  # row 9
        return _out("failed" if rc == 20 else "unknown", "envelope_invalid")
    if rc == 20:  # rows 13 to 15
        reason = env.reason
        assert reason is not None
        if reason in _LINK_REASONS:
            return _out("queued_retry", reason, refusal="link_refused")
        if reason == _KEYWORD_REASON and kind == "ha-notice":
            return _out("queued_retry", reason, refusal="keyword_refused")
        return _out("failed", reason)
    if rc == 1:  # rows 16 and 17
        if env.unknown_outcome:
            return _out("unknown", env.code)
        return _out("failed", "deployment_fault")
    if env.unknown_outcome:  # row 12
        if kind == "ha-notice" and env.retryable and has_dedupe_key:
            return _out("queued_retry", env.code)
        return _out("unknown", env.code)
    return _out("queued_retry" if env.retryable else "failed", env.code)  # rows 10 and 11


# --- recording ------------------------------------------------------------------------------


@dataclass(frozen=True)
class FallbackRoute:
    """What the fallback seam did for a delivery that ended ``failed`` (docs/design.md C-63):
    ``destination`` is the fallback destination and ``decision`` the decision of its step
    (``defer``, ``drop``, ``disabled``, ``suppress``, or ``fault`` when the seam failed and was
    rolled back: the failure is recorded without a fallback). ``outcome`` and ``reason`` are the
    ``deliveries`` ones; ``delivery_id`` is the new row (``None`` for ``suppress``, ``fault``)."""

    destination: str
    decision: str
    outcome: str | None
    reason: str | None
    delivery_id: int | None


@dataclass(frozen=True)
class Applied:
    """What :func:`apply_outcome` did. ``applied`` is false when the recorder no longer holds
    the claim (nothing changed: log only) or the outcome was an internal fault (nothing to
    record). ``outcome`` and ``code`` are the final ones (a retry that ran out of attempts, a
    second refusal and an eviction of the requeued row change them)."""

    applied: bool
    outcome: str | None
    code: str | None
    reason: str | None
    notifier_outcome: str | None
    attempts: int
    deliver_after: int | None
    released: bool = False
    evicted: tuple[Evicted, ...] = ()
    internal: bool = False
    fallback: FallbackRoute | None = None


_NOT_APPLIED = Applied(False, None, None, None, None, 0, None)


def _cut(text: str) -> str:
    return text[:MESSAGE_CHARS].rstrip(" ")


def _refused_row(row: QueueRow, refusal: Refusal) -> tuple[str, str | None]:
    """The message and link of the row queued after a refusal."""
    if refusal == "link_refused":
        return _cut(f"{row.message}\n{row.link}"), None
    return _cut(f"{row.source}: {row.message}"), row.link


def apply_outcome(
    conn: _Conn,
    *,
    row: QueueRow,
    token: str,
    outcome: Outcome,
    lease: Lease,
    retry_at: Callable[[int], int | None],
    count_hour: int | None = None,
    requeue_kind: Literal["held", "single"] = "single",
) -> Applied:
    """Record ``outcome`` for the claimed ``row`` inside a ``Store.write`` transaction.

    ``retry_at(attempts)`` is the retry schedule (:mod:`notice_route.retry`): the
    ``deliver_after`` of a retry after ``attempts`` calls (refusal retries included), or None
    when the attempts are used up (the row then ends ``failed``, code ``retries_exhausted``,
    unless a second or uncurable refusal ended it first with the refusal code). ``count_hour``
    is the UTC hour start in which the hourly count of this call was taken; when given and no
    call was made (``env_missing``, ``lock_busy``) the count is given back (a digest passes it
    for one of its rows only). ``requeue_kind`` is the kind of a row that goes back unchanged (a
    digest member stays ``held``); a refusal retry rewrites the row and always makes it
    ``single``. Everything is fenced by ``token``."""
    if outcome.outcome is None:
        return Applied(False, None, None, None, None, 0, None, internal=True)
    held = conn.execute(
        "SELECT 1 FROM queue WHERE id = ? AND claimed_by = ?", (row.id, token)
    ).fetchone()
    if held is None:  # fence first: a stale claimant's delivery may be trimmed already
        return _NOT_APPLIED
    state = conn.execute(
        "SELECT attempts, reason FROM deliveries WHERE id = ?", (row.delivery_id,)
    ).fetchone()
    if state is None:
        raise ValueError("the row's delivery does not exist")
    attempts = max(int(state[0]), 0) + (1 if outcome.counted else 0)  # clamp a corrupt count
    prior_reason = state[1]

    message: str | None = None
    link: str | None = None
    deliver_after: int | None = None
    reason = outcome.reason
    code = outcome.code
    name: str = outcome.outcome
    if name == "queued_retry" and outcome.reason == "lock_busy":
        deliver_after = lease.now + LOCK_BUSY_RETRY_S  # no attempt, so no schedule step or cap
        if prior_reason in _REFUSED:
            reason = None  # keep the stored refusal: a later refusal must still end failed
    elif name == "queued_retry":
        if outcome.refusal is not None and (
            prior_reason in _REFUSED or (outcome.refusal == "link_refused" and not row.link)
        ):
            name = "failed"  # a second refusal, or nothing to cure: the refusal reason is the code
        else:
            later = retry_at(attempts)
            if later is None:
                name, code = "failed", "retries_exhausted"
            else:
                deliver_after = later
                if outcome.refusal is not None:
                    reason = outcome.refusal
                    message, link = _refused_row(row, outcome.refusal)
    elif name == "deferred":
        deliver_after = lease.now
        if prior_reason in _REFUSED:
            reason = None  # keep the stored refusal: a later refusal must still end failed
    given_back = (name == "deferred" or outcome.reason == "lock_busy") and count_hour is not None

    evicted: tuple[Evicted, ...] = ()
    if name in _TERMINAL:
        if not complete_row(conn, row.id, token):
            raise RuntimeError("the claim vanished inside the transaction")
    else:  # queued_retry or deferred: the row goes back, due at deliver_after
        assert deliver_after is not None
        if message is not None:
            conn.execute(
                "UPDATE queue SET kind = 'single', message = ?, link = ?"
                " WHERE id = ? AND claimed_by = ?",
                (message, link, row.id, token),
            )
        else:
            conn.execute(
                "UPDATE queue SET kind = ? WHERE id = ? AND claimed_by = ?",
                (requeue_kind, row.id, token),
            )
        change = requeue_row(conn, row.id, token, deliver_after, lease)
        if not change.applied:
            raise RuntimeError("the claim vanished inside the transaction")
        evicted = change.evicted
    released = False
    if any(e.delivery_id == row.delivery_id for e in evicted):
        # the cap dropped the row just requeued: its delivery is already dropped/overflow
        conn.execute("UPDATE deliveries SET attempts = ? WHERE id = ?", (attempts, row.delivery_id))
        name, code, reason, deliver_after = "dropped", "overflow", "overflow", None
        released = True
    else:
        conn.execute(
            "UPDATE deliveries SET outcome = ?, code = ?, reason = COALESCE(?, reason),"
            " attempts = ?, deliver_after = ?, message_sha256 = COALESCE(?, message_sha256)"
            " WHERE id = ?",
            (
                name,
                code,
                reason,
                attempts,
                deliver_after,
                None if message is None else _sha16(message),
                row.delivery_id,
            ),
        )
        if name in _RELEASING:
            dedup.release(conn, row.delivery_id)
            released = True
    if given_back:  # decided before the eviction: the call was never made either way
        assert count_hour is not None
        give_back(conn, destination=_destination(row), level=row.level, hour=count_hour)
    shown = reason if reason is not None else prior_reason
    if name == "queued_retry" and outcome.reason == "lock_busy":
        shown = "lock_busy"  # the stored column may keep a refusal; the result says what happened
    return Applied(
        True,
        name,
        code,
        shown,
        outcome.notifier_outcome,
        attempts,
        deliver_after,
        released,
        evicted,
    )


def _destination(row: QueueRow) -> str:
    if row.destination is None:
        raise ValueError("the row has no destination")
    return row.destination


def record_outcome(
    store: Store,
    gateway: Gateway,
    *,
    send_worst_case_s: float,
    row: QueueRow,
    token: str,
    outcome: Outcome,
    retry_at: Callable[[int], int | None],
    count_hour: int | None = None,
) -> Applied:
    """:func:`apply_outcome` in its own write transaction (the clock is read after the write
    lock is held). Any exception rolls everything back."""
    with store.write() as conn:
        lease = Lease.at(gateway.wall(), send_worst_case_s)
        return apply_outcome(
            conn,
            row=row,
            token=token,
            outcome=outcome,
            lease=lease,
            retry_at=retry_at,
            count_hour=count_hour,
        )


def record_escaped(
    store: Store,
    *,
    row: QueueRow,
    token: str,
    surface: Callable[[_Conn, int], None] | None = None,
) -> bool:
    """An exception escaped ``call_notifier``: the child's fate is unknown. Mark the delivery
    ``unknown``/``killed`` with one more attempt and keep the queue row claimed (it lapses and
    ``flush`` resends: at least once). Dedup records stay. False when the claim is gone.
    ``surface(conn, attempts)`` runs inside the transaction when the row was recorded (it may run
    twice: it writes only through ``conn``)."""
    with store.write() as conn:
        cur = conn.execute(
            "UPDATE deliveries SET outcome = 'unknown', code = 'killed', attempts = attempts + 1,"
            " deliver_after = NULL WHERE id = ? AND EXISTS"
            " (SELECT 1 FROM queue WHERE id = ? AND claimed_by = ?)",
            (row.delivery_id, row.id, token),
        )
        if cur.rowcount != 1:
            return False
        if surface is not None:
            found = conn.execute(
                "SELECT attempts FROM deliveries WHERE id = ?", (row.delivery_id,)
            ).fetchone()
            surface(conn, int(found[0]))
        return True
