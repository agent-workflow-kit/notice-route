"""Queue rows, claims, leases, the 100-row cap and eviction (#16).

Data-layer operations only. Every function takes the connection of a write transaction opened
with :meth:`Store.write` (``BEGIN IMMEDIATE``), so a claim, a record or an eviction is atomic with
whatever else the caller writes in the same transaction; nothing here commits, and a caller that
raises rolls everything back. Time is passed in as a :class:`Lease` built from the gateway's wall
clock by the caller, so the module reads no clock and tests are deterministic. Every value reaches
SQL as a bound parameter and every statement is a constant string.

Rules (docs/interface.md: Deferral and digest, Files; docs/design.md: Queue and row model):

- A row is stored rendered. It is **claimed** while ``claimed_by`` is set and ``claim_expires`` is
  after ``now`` and at most ``now + lease length``: a lease is never longer than twice
  ``send_worst_case_s`` *as seen from now*, so a wall clock that stepped backwards cannot keep a
  claim alive for longer (such a claim counts as lapsed, and the row is resent: at least once,
  never blocked forever). Anything else (no claimant, no expiry, expiry reached, expiry beyond
  the maximum) is **unclaimed**; an unclaimed row is **due** once ``deliver_after`` has passed.
- A claim writes a fresh random token, so the claimant before it is fenced out: a result is
  recorded (:func:`complete_row`, :func:`requeue_row`) only on a row whose ``claimed_by`` is still
  the recorder's token, whether or not its own lease lapsed meanwhile (nobody took it: its
  result is still the newest information).
- A destination keeps at most :data:`QUEUE_CAP` unclaimed rows. Every operation that leaves a row
  unclaimed (:func:`enqueue` of an unclaimed row, :func:`requeue_row`, :func:`release_claims`)
  calls :func:`enforce_cap`: the oldest row of the lowest level is deleted (a row just queued
  included) and its ``deliveries`` row becomes ``dropped``, reason and code ``overflow``.
  Claimed rows are neither counted nor evicted. Age is the row id (never reused and independent
  of clock steps), not ``created_at``.

Seams, owned by later issues:

TODO(#17): a dedup or event-id record of an evicted delivery is deleted with it (``dedup`` and
``event_ids`` rows by ``delivery_id``); the statements do not exist yet.
TODO(#27): an ``overflow`` eviction is surfaced once per destination per UTC hour (the caller
gets the evicted rows back and inserts the ``surfaced`` row in the same transaction).
TODO(#23): flush step 4 and the prune of #26 (expire unclaimed rows 24 hours old or more, never a
row with an unexpired claim) and the digest and step-5 helpers must reuse this module's lease
predicate (``_UNCLAIMED``) through a public helper, never a second copy of it. Also:
release_claims takes one token, so a caller holding several needs one release per token or one
per-process token.
TODO(#26): ``prune`` expires unclaimed queue rows through that same public helper.
TODO(#20): the outcome of a sent, failed or retried row is written to its ``deliveries`` row in the
transaction that calls :func:`complete_row` or :func:`requeue_row` (only when it returns applied).
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Literal, get_args

from .store import DELIVERIES_CAP, trim_deliveries
from .validate import LEVELS, QuietWindow
from .window import window_end

__all__ = [
    "LEASE_FACTOR",
    "MAX_INSTANT",
    "QUEUE_CAP",
    "Change",
    "Enqueued",
    "Evicted",
    "Lease",
    "QueueRow",
    "Released",
    "claim_due",
    "complete_row",
    "enforce_cap",
    "enqueue",
    "get_row",
    "insert_delivery",
    "quiet_deliver_after",
    "release_claims",
    "requeue_row",
    "unclaimed_count",
]

QUEUE_CAP = 100  # unclaimed rows per destination
LEASE_FACTOR = 2  # a lease is this many times ``send_worst_case_s``
MAX_INSTANT = 1 << 40  # |epoch seconds| bound: far past year 9999, far from an int64 overflow
_MAX_WORST_CASE_S = 1 << 20
_MAX_TOKEN = 128

_Conn = sqlite3.Connection
Kind = Literal["held", "single"]
_KINDS: tuple[str, ...] = get_args(Kind)


def _int(value: object, what: str) -> int:
    """An int (never a bool) within ``+-MAX_INSTANT``; anything else is a ``ValueError``."""
    if type(value) is not int or not -MAX_INSTANT <= value <= MAX_INSTANT:
        raise ValueError(f"{what} must be an integer within +-2**40")
    return value


def _id(value: object, what: str) -> int:
    """A row or delivery id: an int (never a bool) within int64, else a ``ValueError``."""
    if type(value) is not int or not -(1 << 63) <= value < (1 << 63):
        raise ValueError(f"{what} must be an integer within int64")
    return value


def _text(value: object, what: str) -> str | None:
    """A text column: ``str`` or ``None``; anything else is a ``ValueError``."""
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{what} must be a string or None")
    return value


def _token(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_TOKEN:
        raise ValueError("claim token must be a string of 1 to 128 characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("claim token must be encodable as UTF-8") from None
    return value


@dataclass(frozen=True)
class Lease:
    """The clock's reading and the longest lease: ``now`` is UTC epoch seconds (floored),
    ``length`` is :data:`LEASE_FACTOR` times ``send_worst_case_s`` rounded up.

    Build the lease inside the :meth:`Store.write` block, after the write lock is held: a lease
    built before the lock can carry an earlier ``now`` than a concurrent claimant's and read its
    live claim as lapsed (a duplicate send, never a lost row). The predicate adds no skew slack:
    a lease is never longer than its length even if the clock moved."""

    now: int
    length: int

    def __post_init__(self) -> None:
        _int(self.now, "now")
        if type(self.length) is not int or not 1 <= self.length <= LEASE_FACTOR * _MAX_WORST_CASE_S:
            raise ValueError("lease length must be a positive integer of at most 2**21 seconds")

    @classmethod
    def at(cls, wall: float, send_worst_case_s: float) -> Lease:
        """``wall`` comes from ``gateway.wall()``; a non-finite or out-of-range reading, a bool
        and a worst case that is not a positive finite number are ``ValueError``."""
        for what, value in (("wall", wall), ("send_worst_case_s", send_worst_case_s)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{what} must be a number")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{what} must be finite")
        if not 0 < send_worst_case_s <= _MAX_WORST_CASE_S:
            raise ValueError("send_worst_case_s must be positive and at most 2**20")
        if not -MAX_INSTANT <= wall <= MAX_INSTANT:
            raise ValueError("wall is out of range")
        return cls(math.floor(wall), LEASE_FACTOR * math.ceil(send_worst_case_s))

    @property
    def expires(self) -> int:
        """The ``claim_expires`` of a claim made now."""
        return self.now + self.length


@dataclass(frozen=True)
class QueueRow:
    id: int
    delivery_id: int
    destination: str | None
    kind: str
    title: str | None
    message: str
    link: str | None
    level: str
    source: str
    rule: str | None
    dedupe: str | None
    deliver_after: int
    created_at: int
    claimed_by: str | None
    claim_expires: int | None


@dataclass(frozen=True)
class Evicted:
    """A row dropped for overflow (its ``deliveries`` row is already updated)."""

    queue_id: int
    delivery_id: int
    destination: str
    kind: str
    level: str


@dataclass(frozen=True)
class Change:
    """``applied`` is false when the row is no longer claimed by the recorder (nothing changed)."""

    applied: bool
    evicted: tuple[Evicted, ...] = ()


@dataclass(frozen=True)
class Enqueued:
    row_id: int
    evicted: tuple[Evicted, ...] = ()

    @property
    def row_evicted(self) -> bool:
        """The row just queued was itself the one dropped."""
        return any(e.queue_id == self.row_id for e in self.evicted)


@dataclass(frozen=True)
class Released:
    count: int
    evicted: tuple[Evicted, ...] = ()


# --- SQL (constant) ------------------------------------------------------------------------

# Unclaimed at ``now`` with the longest lease ``length``; parameters ``now``, ``now + length``.
_UNCLAIMED = (
    "(q.claimed_by IS NULL OR q.claim_expires IS NULL"
    " OR q.claim_expires <= ? OR q.claim_expires > ?)"
)
_COLUMNS = (
    "q.id, q.delivery_id, d.destination, q.kind, q.title, q.message, q.link, q.level, q.source,"
    " q.rule, q.dedupe, q.deliver_after, q.created_at, q.claimed_by, q.claim_expires"
)
_FROM = " FROM queue q JOIN deliveries d ON d.id = q.delivery_id"
# an unknown level sorts below ``info`` and goes first
_RANK = (
    "CASE q.level WHEN 'info' THEN 0 WHEN 'warn' THEN 1 WHEN 'action' THEN 2"
    " WHEN 'error' THEN 3 ELSE -1 END"
)

_SELECT_DUE = (
    "SELECT q.id"
    + _FROM
    + " WHERE d.destination = ? AND q.deliver_after <= ? AND "
    + _UNCLAIMED
    + " AND (? IS NULL OR q.kind = ?) ORDER BY q.id LIMIT ?"
)
_COUNT_UNCLAIMED = "SELECT COUNT(*)" + _FROM + " WHERE d.destination = ? AND " + _UNCLAIMED
_SELECT_VICTIMS = (
    "SELECT q.id, q.delivery_id, q.kind, q.level"
    + _FROM
    + " WHERE d.destination = ? AND "
    + _UNCLAIMED
    + " ORDER BY "
    + _RANK
    + ", q.id LIMIT ?"
)


def _row(values: tuple[object, ...]) -> QueueRow:
    return QueueRow(*values)  # type: ignore[arg-type]


def get_row(conn: _Conn, row_id: int) -> QueueRow | None:
    found = conn.execute(
        "SELECT " + _COLUMNS + _FROM + " WHERE q.id = ?", (_id(row_id, "row_id"),)
    ).fetchone()
    return None if found is None else _row(found)


def unclaimed_count(conn: _Conn, destination: str, lease: Lease) -> int:
    """Rows of ``destination`` that are unclaimed now (lapsed claims included)."""
    n = conn.execute(_COUNT_UNCLAIMED, (destination, lease.now, lease.expires)).fetchone()[0]
    return int(n)


# --- deliveries ----------------------------------------------------------------------------


def insert_delivery(
    conn: _Conn,
    *,
    ts: int,
    source: str,
    level: str,
    outcome: str,
    destination: str | None = None,
    hook: str | None = None,
    object_id: int | str | None = None,
    rule: str | None = None,
    reason: str | None = None,
    code: str | None = None,
    attempts: int = 0,
    count: int = 1,
    event_id: str | None = None,
    dedupe: str | None = None,
    labels: str | None = None,
    surfaced: int = 0,
    deliver_after: int | None = None,
    title: str | None = None,
    title_sha256: str | None = None,
    message_sha256: str | None = None,
    cap: int = DELIVERIES_CAP,
) -> int:
    """Insert a ``deliveries`` row and trim the table to ``cap`` in the same transaction, never
    deleting the row just inserted (``trim_deliveries`` with ``keep_id``). Returns its id."""
    _int(ts, "ts")
    if level not in LEVELS:
        raise ValueError("level must be one of info, warn, action, error")
    cur = conn.execute(
        "INSERT INTO deliveries (ts, source, hook, object_id, rule, destination, level, outcome,"
        " reason, code, attempts, count, event_id, dedupe, labels, surfaced, deliver_after,"
        " title, title_sha256, message_sha256)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts,
            source,
            hook,
            object_id,
            rule,
            destination,
            level,
            outcome,
            reason,
            code,
            attempts,
            count,
            event_id,
            dedupe,
            labels,
            surfaced,
            deliver_after,
            title,
            title_sha256,
            message_sha256,
        ),
    )
    new_id = cur.lastrowid
    assert new_id is not None
    trim_deliveries(conn, cap=cap, keep_id=new_id)
    return new_id


# --- the cap -------------------------------------------------------------------------------


def enforce_cap(
    conn: _Conn, destination: str, lease: Lease, *, cap: int = QUEUE_CAP
) -> tuple[Evicted, ...]:
    """Drop the oldest rows of the lowest level among ``destination``'s unclaimed rows until at
    most ``cap`` remain; each victim's queue row is deleted (its text goes with it) and its
    ``deliveries`` row set to ``dropped``, reason ``overflow``, code ``overflow`` and
    ``deliver_after`` cleared. Returns the
    victims in the order dropped."""
    if type(cap) is not int or cap < 0:
        raise ValueError("cap must be a non-negative integer")
    excess = unclaimed_count(conn, destination, lease) - cap
    if excess <= 0:
        return ()
    victims = conn.execute(
        _SELECT_VICTIMS, (destination, lease.now, lease.expires, excess)
    ).fetchall()
    evicted: list[Evicted] = []
    for queue_id, delivery_id, kind, level in victims:
        conn.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
        conn.execute(
            "UPDATE deliveries SET outcome = 'dropped', reason = 'overflow', code = 'overflow',"
            " deliver_after = NULL WHERE id = ?",
            (delivery_id,),
        )
        evicted.append(Evicted(queue_id, delivery_id, destination, kind, level))
    return tuple(evicted)


def _destination_of(conn: _Conn, delivery_id: int) -> str:
    sql = "SELECT destination FROM deliveries WHERE id = ?"
    found = conn.execute(sql, (_id(delivery_id, "delivery_id"),)).fetchone()
    if found is None or not isinstance(found[0], str):
        raise ValueError("delivery_id must name a deliveries row with a destination")
    return found[0]


# --- rows ----------------------------------------------------------------------------------


def enqueue(
    conn: _Conn,
    *,
    delivery_id: int,
    kind: Kind,
    message: str,
    level: str,
    source: str,
    deliver_after: int,
    created_at: int,
    lease: Lease,
    title: str | None = None,
    link: str | None = None,
    rule: str | None = None,
    dedupe: str | None = None,
    claim_token: str | None = None,
    cap: int = QUEUE_CAP,
) -> Enqueued:
    """Store a rendered row for ``delivery_id`` (its destination is the delivery's).

    With ``claim_token`` the row is born claimed by it, until ``lease.expires`` (a send-now row:
    claimed rows are not counted and the cap is not applied). Without, the row is unclaimed and
    the cap is applied at once, which may drop this very row (``row_evicted``).
    """
    if kind not in _KINDS:
        raise ValueError("kind must be held or single")
    if level not in LEVELS:
        raise ValueError("level must be one of info, warn, action, error")
    _int(deliver_after, "deliver_after")
    _int(created_at, "created_at")
    for what, value in (("title", title), ("message", message), ("link", link), ("source", source)):
        _text(value, what)
    if message is None or source is None:
        raise ValueError("message and source must be strings")
    _text(rule, "rule")
    _text(dedupe, "dedupe")
    destination = _destination_of(conn, delivery_id)
    token = None if claim_token is None else _token(claim_token)
    cur = conn.execute(
        "INSERT INTO queue (delivery_id, kind, title, message, link, level, source, rule, dedupe,"
        " deliver_after, created_at, claimed_by, claim_expires)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            delivery_id,
            kind,
            title,
            message,
            link,
            level,
            source,
            rule,
            dedupe,
            deliver_after,
            created_at,
            token,
            None if token is None else lease.expires,
        ),
    )
    row_id = cur.lastrowid
    assert row_id is not None
    if token is not None:
        return Enqueued(row_id)
    return Enqueued(row_id, enforce_cap(conn, destination, lease, cap=cap))


def claim_due(
    conn: _Conn,
    destination: str,
    lease: Lease,
    token: str,
    *,
    kind: Kind | None = None,
    limit: int | None = None,
) -> list[QueueRow]:
    """Claim the due rows of ``destination`` for ``token``: unclaimed (a lapsed claim counts) and
    ``deliver_after <= now``, oldest ``deliver_after`` first, optionally one ``kind`` and at most
    ``limit`` rows. Each gets ``claimed_by = token`` and ``claim_expires = lease.expires``.
    A fresh token per claim fences the previous claimant; never reuse one."""
    _token(token)
    if kind is not None and kind not in _KINDS:
        raise ValueError("kind must be held or single")
    if limit is not None and (type(limit) is not int or not 1 <= limit < (1 << 63)):
        raise ValueError("limit must be a positive integer within int64")
    ids = conn.execute(
        _SELECT_DUE,
        (
            destination,
            lease.now,
            lease.now,
            lease.expires,
            kind,
            kind,
            -1 if limit is None else limit,
        ),
    ).fetchall()
    claimed: list[QueueRow] = []
    for (row_id,) in ids:
        cur = conn.execute(
            "UPDATE queue AS q SET claimed_by = ?, claim_expires = ? WHERE q.id = ? AND "
            + _UNCLAIMED,
            (token, lease.expires, row_id, lease.now, lease.expires),
        )
        if cur.rowcount != 1:  # unreachable inside BEGIN IMMEDIATE
            raise RuntimeError("a row selected for claiming was not claimable")
        row = get_row(conn, row_id)
        assert row is not None
        claimed.append(row)
    return claimed


def complete_row(conn: _Conn, row_id: int, token: str) -> bool:
    """Delete the row after its result was recorded; ``False`` (nothing changed) unless it is
    still claimed by ``token``: a claimant whose lease lapsed and was taken over is fenced."""
    cur = conn.execute(
        "DELETE FROM queue WHERE id = ? AND claimed_by = ?", (_id(row_id, "row_id"), _token(token))
    )
    return cur.rowcount == 1


def requeue_row(
    conn: _Conn,
    row_id: int,
    token: str,
    deliver_after: int,
    lease: Lease,
    *,
    cap: int = QUEUE_CAP,
) -> Change:
    """Give a claimed row back, due at ``deliver_after`` (a ``queued_retry``, or a held row whose
    window is still quiet), then apply the cap. Only the current claimant can; else ``applied``
    is false."""
    _int(deliver_after, "deliver_after")
    _id(row_id, "row_id")
    destination = conn.execute(
        "SELECT d.destination" + _FROM + " WHERE q.id = ? AND q.claimed_by = ?",
        (row_id, _token(token)),
    ).fetchone()
    if destination is None:
        return Change(False)
    if not isinstance(destination[0], str):
        raise ValueError("delivery_id must name a deliveries row with a destination")
    conn.execute(
        "UPDATE queue SET claimed_by = NULL, claim_expires = NULL, deliver_after = ?"
        " WHERE id = ? AND claimed_by = ?",
        (deliver_after, row_id, token),
    )
    return Change(True, enforce_cap(conn, destination[0], lease, cap=cap))


def release_claims(conn: _Conn, token: str, lease: Lease, *, cap: int = QUEUE_CAP) -> Released:
    """A signal ended the claimant before its calls: set ``claim_expires`` of every row claimed by
    ``token`` to now (never later than it was), keeping ``claimed_by`` so the claimant can still
    record a result, then apply the cap to each destination that got unclaimed rows back.
    ``count`` is the rows still carrying ``token`` after the call (including rows whose claim had
    already lapsed), not only those whose expiry changed."""
    _token(token)
    destinations = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT d.destination" + _FROM + " WHERE q.claimed_by = ?", (token,)
        )
    ]
    cur = conn.execute(
        "UPDATE queue SET claim_expires = MIN(COALESCE(claim_expires, ?), ?) WHERE claimed_by = ?",
        (lease.now, lease.now, token),
    )
    evicted: list[Evicted] = []
    for destination in destinations:
        if isinstance(destination, str):
            evicted += enforce_cap(conn, destination, lease, cap=cap)
    return Released(cur.rowcount, tuple(evicted))


def quiet_deliver_after(window: QuietWindow, wall: float) -> int:
    """``deliver_after`` of a row held for ``window`` at ``wall``: the next window end
    (``flush`` calls it again for a due row while the destination is still quiet). ``None`` from
    :func:`window_end` is unreachable for a valid window and is an error, never permission to
    skip the check."""
    end = window_end(window, wall)
    if end is None:
        raise RuntimeError("quiet window has no end within two days")
    return end
