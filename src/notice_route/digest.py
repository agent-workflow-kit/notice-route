"""The digest of due ``held`` rows (#24): docs/interface.md, Deferral and digest.

:func:`build` is pure: queue rows in, the notification out. Title ``N held notifications``; one
line per row ``<level> <source>: <head>``, ``<head>`` being the stored title (or the rule name when
the row has none), `` - `` and the first non-empty line of the stored message, cut together to
:data:`HEAD_CHARS` code points (a cut leaves no trailing space, dangling `` -`` or U+200D); lines
ordered by level from the highest, then oldest (lowest row id: ids are never reused and do not
follow the wall clock) first; at most :data:`MAX_LINES` lines,
then ``and K more``; the highest level; no link (a digest has two rows or more, and the row's link
is kept only for exactly one row); no dedupe key.

Stored text is untrusted: a row's title, message, source and rule are sanitized again here
(:func:`notice_route.render.sanitize`, one line) and are never templated, so ``{{x}}`` in a row
stays literal. Cuts count code points, and the text is stripped of trailing spaces after each cut.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from .decide import give_back
from .gateway import Gateway
from .outcome import Applied, Outcome, apply_outcome
from .payload import LEVELS
from .queue import Lease, QueueRow
from .render import sanitize
from .send import Seams, SurfaceEvent, evicted_events, with_retry
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


def _first_line(message: str) -> str:
    """The first line of ``message`` that is not blank once sanitized (empty when none is)."""
    return next((x for x in sanitize(message, True).split("\n") if x), "")


def _cut_head(head: str) -> str:
    """Cut to :data:`HEAD_CHARS` code points, then drop what the cut can leave dangling: spaces,
    the separator's ``-`` after a space, and a zero-width joiner."""
    head = head[:HEAD_CHARS]
    while True:
        cut = head.rstrip(" ").removesuffix("\u200d")
        if cut.endswith(" -"):
            cut = cut[:-1]
        if cut == head:
            return head
        head = cut


def _line(row: QueueRow) -> str:
    name = sanitize(row.title or row.rule or "", False)
    first = _first_line(row.message)
    head = _cut_head(" - ".join(p for p in (name, first) if p))
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
_KEEPS_REASON = ("lock_busy",)  # a requeue of this call that says why itself (a refusal does too)


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
    given back once, in the class of the digest's level, when at least one member was recorded;
    a ``failed`` or ``unknown`` end is surfaced once, for the first member (highest level,
    oldest) whose result was recorded."""

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
                counts = [
                    max(a, 0) + (1 if outcome.counted else 0)
                    for a in _attempts(conn, members, token)
                ]
                alive = [a for a in counts if base(a) is not None]
                later = base(max(alive)) if alive else None
                retry_at = lambda a: None if base(a) is None else later  # noqa: E731
            applied: list[Applied] = []
            surfaced = False
            for m in members:
                a = apply_outcome(
                    conn,
                    row=m,
                    token=token,
                    outcome=outcome,
                    lease=lease,
                    retry_at=retry_at,
                    requeue_kind="held" if outcome.outcome == "queued_retry" else "single",
                )
                if a.applied:
                    a = _reason_digest(conn, m, a, outcome)
                    if a.outcome == "failed" and seams.fallback is not None:
                        route = seams.fallback(conn, dest, m, lease)
                        if route is not None:
                            a = replace(a, fallback=route)
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
                                lease.now,
                            ),
                        )
                applied.append(a)
            for a in applied:  # rows the cap dropped when members went back
                if a.applied:
                    for event in evicted_events(conn, dest, a.evicted, lease.now):
                        seams.surface(conn, event)
            if any(a.applied for a in applied) and (
                outcome.outcome == "deferred" or outcome.reason == "lock_busy"
            ):  # no call was made: the digest's one count goes back, in the digest's class
                give_back(conn, destination=dest.name, level=members[0].level, hour=count_hour)
            evicted = frozenset(e.delivery_id for a in applied for e in a.evicted)
            return DigestResult(tuple(applied), evicted)

    return with_retry(gateway, members[0], write)


def _attempts(conn: sqlite3.Connection, members: Sequence[QueueRow], token: str) -> list[int]:
    """The attempts of the members whose claim ``token`` still holds (a lost member, or one whose
    rows are gone, is left out: nothing is recorded for it)."""
    out: list[int] = []
    for m in members:
        found = conn.execute(
            "SELECT d.attempts FROM queue q JOIN deliveries d ON d.id = q.delivery_id"
            " WHERE q.id = ? AND q.claimed_by = ?",
            (m.id, token),
        ).fetchone()
        if found is not None:
            out.append(int(found[0]))
    return out


def _reason_digest(
    conn: sqlite3.Connection, row: QueueRow, a: Applied, outcome: Outcome
) -> Applied:
    """A member that ended, or went back to be retried, because of the digest has reason
    ``digest``. A requeue that says why itself (this call's ``lock_busy`` or refusal) and a row
    the cap dropped keep their own; the decision is on this call's result, not on the reason the
    row had stored."""
    says_itself = outcome.reason in _KEEPS_REASON or outcome.refusal is not None
    ends = a.outcome in _ENDS or (a.outcome == "queued_retry" and not says_itself)
    if not ends:
        return a
    conn.execute(
        "UPDATE deliveries SET reason = 'digest' WHERE id = ? AND outcome = ?",
        (row.delivery_id, a.outcome),
    )
    return replace(a, reason="digest")


def record_escaped_digest(
    store: Store,
    members: Sequence[QueueRow],
    token: str,
    surface: Callable[[sqlite3.Connection, QueueRow, int], None] | None = None,
) -> list[int | None]:
    """An exception escaped the call: every member is ``unknown``/``killed``, reason ``digest``,
    with one more attempt and stays claimed (it lapses and ``flush`` resends: at least once). One
    transaction; the result is each member's new attempts, or ``None`` for a member whose claim is
    gone (skipped). ``surface(conn, member, attempts)`` runs once, inside the transaction, for the
    first member that was recorded."""
    out: list[int | None] = []
    done = False
    with store.write() as conn:
        for m in members:
            cur = conn.execute(
                "UPDATE deliveries SET outcome = 'unknown', code = 'killed', reason = 'digest',"
                " attempts = attempts + 1, deliver_after = NULL WHERE id = ? AND EXISTS"
                " (SELECT 1 FROM queue WHERE id = ? AND claimed_by = ?)",
                (m.delivery_id, m.id, token),
            )
            if cur.rowcount != 1:
                out.append(None)
                continue
            row = conn.execute(
                "SELECT attempts FROM deliveries WHERE id = ?", (m.delivery_id,)
            ).fetchone()
            out.append(int(row[0]))
            if surface is not None and not done:
                done = True
                surface(conn, m, int(row[0]))
    return out
