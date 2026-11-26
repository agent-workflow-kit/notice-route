"""The digest of due ``held`` rows (#24): docs/interface.md, Deferral and digest.

:func:`build` is pure: queue rows in, the notification out. Title ``N held notifications``; one
line per row ``<level> <source>: <head>``, ``<head>`` being the stored title (or the rule name when
the row has none), `` - `` and the first line of the stored message, cut together to
:data:`HEAD_CHARS` code points; lines ordered by level from the highest, then oldest (lowest row
id: ids are never reused and do not follow the wall clock) first; at most :data:`MAX_LINES` lines,
then ``and K more``; the highest level; no link (a digest has two rows or more, and the row's link
is kept only for exactly one row); no dedupe key.

Stored text is untrusted: a row's title, message, source and rule are sanitized again here
(:func:`notice_route.render.sanitize`, one line) and are never templated, so ``{{x}}`` in a row
stays literal. Cuts count code points, and the text is stripped of trailing spaces after each cut.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace

from .gateway import Gateway
from .outcome import Applied, Outcome, apply_outcome
from .payload import LEVELS
from .queue import Lease, QueueRow
from .render import sanitize
from .send import Seams, SurfaceEvent, with_retry
from .store import Store
from .validate import Destination

__all__ = [
    "HEAD_CHARS",
    "MAX_LINES",
    "MESSAGE_CHARS",
    "Digest",
    "DigestResult",
    "build",
    "record_digest",
    "record_escaped_digest",
]

HEAD_CHARS = 120
MAX_LINES = 10
MESSAGE_CHARS = 4000  # the cap of any rendered message
_RANK = {level: rank for rank, level in enumerate(LEVELS)}


@dataclass(frozen=True)
class Digest:
    """``rows`` are all the folded rows in line order (the first is the digest's first row)."""

    title: str
    message: str
    level: str
    rows: tuple[QueueRow, ...]
    link: None = None


def _line(row: QueueRow) -> str:
    name = sanitize(row.title or row.rule or "", False)
    first = sanitize(row.message, False)
    head = " - ".join(p for p in (name, first) if p)[:HEAD_CHARS].rstrip(" ")
    source = sanitize(row.source, False)
    return f"{row.level} {source}: {head}".rstrip(" ")


def build(rows: Sequence[QueueRow]) -> Digest:
    """The digest of two or more rows with valid levels (anything else is a ``ValueError``)."""
    if len(rows) < 2:
        raise ValueError("a digest folds two rows or more")
    if any(r.level not in _RANK for r in rows):
        raise ValueError("level must be one of info, warn, action, error")
    ordered = sorted(rows, key=lambda r: (-_RANK[r.level], r.id))
    lines = [_line(r) for r in ordered[:MAX_LINES]]
    if len(ordered) > MAX_LINES:
        lines.append(f"and {len(ordered) - MAX_LINES} more")
    message = "\n".join(lines)[:MESSAGE_CHARS].rstrip(" ")
    return Digest(f"{len(ordered)} held notifications", message, ordered[0].level, tuple(ordered))


# --- recording -------------------------------------------------------------------------------

_ENDS = ("sent", "failed", "unknown", "disabled")
_KEEPS_REASON = ("lock_busy", "link_refused", "keyword_refused")  # what a requeue says itself


@dataclass(frozen=True)
class DigestResult:
    """``applied[i]`` is the result of member ``i`` (``applied`` false: its claim was lost, so
    nothing was recorded for it). ``evicted_ids`` are the deliveries the 100-row cap dropped
    while the members went back."""

    applied: tuple[Applied, ...]
    evicted_ids: frozenset[int]


def record_digest(
    store: Store,
    gateway: Gateway,
    seams: Seams,
    worst: float,
    dest: Destination,
    members: Sequence[QueueRow],
    token: str,
    outcome: Outcome,
    count_hour: int,
) -> DigestResult:
    """Apply one classified digest result to every member in one transaction (all or nothing: a
    failure leaves every member claimed, and they lapse and are sent again), each fenced by the
    shared ``token``. A member is recorded as if it had been sent alone, with these differences:
    its reason is ``digest``; a ``queued_retry`` that is not a refusal leaves it ``held`` and,
    for a result that counts an attempt, every member gets the schedule of the highest attempt
    count among the members that still have attempts left; the hourly count the digest took is
    given back once, through the first member still claimed; a ``failed`` or ``unknown`` end is
    surfaced once, for the first member (highest level, oldest) whose result was recorded."""

    def write() -> DigestResult:
        # may run twice (see ``with_retry``): ``seams.surface`` writes only through ``conn``
        with store.write() as conn:
            lease = Lease.at(gateway.wall(), worst)
            base = seams.retry_schedule(dest.kind, lease.now)
            retry_at = base
            plain_retry = (
                outcome.outcome == "queued_retry"
                and outcome.refusal is None
                and outcome.reason != "lock_busy"
            )
            if plain_retry:
                counts = [_attempts(conn, m) + (1 if outcome.counted else 0) for m in members]
                alive = [a for a in counts if base(a) is not None]
                later = base(max(alive)) if alive else None
                retry_at = lambda a: None if base(a) is None else later  # noqa: E731
            applied: list[Applied] = []
            pending: int | None = count_hour
            surfaced = False
            for m in members:
                a = apply_outcome(
                    conn,
                    row=m,
                    token=token,
                    outcome=outcome,
                    lease=lease,
                    retry_at=retry_at,
                    count_hour=pending,
                    requeue_kind="held",
                )
                if a.applied:
                    pending = None
                    a = _reason_digest(conn, m, a)
                    if not surfaced and a.outcome in ("failed", "unknown"):
                        surfaced = True
                        assert a.outcome is not None
                        seams.surface(
                            conn,
                            SurfaceEvent(
                                m.delivery_id,
                                dest.name,
                                dest.kind,
                                m.source,
                                m.level,
                                a.outcome,
                                a.code,
                                a.attempts,
                            ),
                        )
                applied.append(a)
            evicted = frozenset(e.delivery_id for a in applied for e in a.evicted)
            return DigestResult(tuple(applied), evicted)

    return with_retry(gateway, members[0], write)


def _attempts(conn: sqlite3.Connection, row: QueueRow) -> int:
    found = conn.execute(
        "SELECT attempts FROM deliveries WHERE id = ?", (row.delivery_id,)
    ).fetchone()
    return int(found[0])


def _reason_digest(conn: sqlite3.Connection, row: QueueRow, a: Applied) -> Applied:
    """A member that ended, or went back to be retried, because of the digest has reason
    ``digest``. A requeue that says why itself (``lock_busy``, a refusal) and a row the cap dropped
    keep their own."""
    ends = a.outcome in _ENDS or (a.outcome == "queued_retry" and a.reason not in _KEEPS_REASON)
    if not ends:
        return a
    conn.execute(
        "UPDATE deliveries SET reason = 'digest' WHERE id = ? AND outcome = ?",
        (row.delivery_id, a.outcome),
    )
    return replace(a, reason="digest")


def record_escaped_digest(store: Store, members: Sequence[QueueRow], token: str) -> list[bool]:
    """An exception escaped the call: every member is ``unknown``/``killed`` with one more attempt
    and stays claimed (it lapses and ``flush`` resends: at least once). One transaction; a member
    whose claim is gone is skipped (``False``)."""
    out: list[bool] = []
    with store.write() as conn:
        for m in members:
            cur = conn.execute(
                "UPDATE deliveries SET outcome = 'unknown', code = 'killed',"
                " attempts = attempts + 1, deliver_after = NULL WHERE id = ? AND EXISTS"
                " (SELECT 1 FROM queue WHERE id = ? AND claimed_by = ?)",
                (m.delivery_id, m.id, token),
            )
            out.append(cur.rowcount == 1)
    return out
