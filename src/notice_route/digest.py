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

from collections.abc import Sequence
from dataclasses import dataclass

from .payload import LEVELS
from .queue import QueueRow
from .render import sanitize

__all__ = ["HEAD_CHARS", "MAX_LINES", "MESSAGE_CHARS", "Digest", "build"]

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
