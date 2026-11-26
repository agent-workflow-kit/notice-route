"""``history`` (docs/interface.md, history): read the ``deliveries`` table, newest first.

A history row is the router's own record, but the database is a file: anything that can write
the state directory can put any text in any column. So every column is checked against the
grammar of ``schemas/cli/history.json`` on its way out; a row with a column that fails its
grammar is left out (and counted in a warning), never repaired and never printed raw. Titles are
the only free text: they are cut to 100 characters here and escaped by the printers (JSON writes
ASCII only; the text form escapes every control and non-ASCII character).

Choices where the interface is silent, all the narrower reading:

- ``--since`` is inclusive (``ts >= since``); a duration counts back from the wall clock at the
  start of the command. An RFC 3339 time must be UTC written with ``Z`` (a fraction is floored).
- Newest is by ``ts`` (the decision time), then by ``id``, both descending.
- A filter value outside its closed grammar is exit 2 (not an empty list): a source, destination
  and label are matched as written, never as a pattern.
- More than 16 distinct ``--label`` values cannot match (a row stores at most 16): the list is
  empty without a query.
- The database is opened read-only; with none, or an empty file, the list is empty.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .gateway import Gateway
from .log import timestamp
from .store import open_readonly

__all__ = [
    "DEFAULT_LIMIT",
    "LEVELS",
    "MAX_LIMIT",
    "OUTCOMES",
    "HistoryFilters",
    "HistoryResult",
    "parse_since",
    "run_history",
]

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_LABELS = 16
_FAR = 1 << 62  # a bound that an int64 column comparison takes

LEVELS = ("info", "warn", "action", "error")
OUTCOMES = (
    "sent",
    "deferred",
    "queued_retry",
    "dropped",
    "disabled",
    "failed",
    "unknown",
    "sending",
    "expired",
)
REASONS = (
    "below_floor",
    "quiet",
    "rate_capped",
    "overflow",
    "lock_busy",
    "env_missing",
    "rule_drop",
    "no_rule",
    "digest",
    "link_refused",
    "keyword_refused",
    "manual",
)

SOURCE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
HOOK = re.compile(r"[a-z][a-z0-9_-]{0,63}")
RULE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
DESTINATION = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#-]{0,255}")
DIGEST = re.compile(r"[0-9a-f]{64}")
LABEL = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}=[A-Za-z0-9._:/#@+=,-]{0,256}")
_DURATION = re.compile(r"([0-9]+)([smhd])")
_TIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]{1,9})?Z"
)
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_since(text: str, now: int) -> int | None:
    """The epoch second ``--since`` names, or ``None`` when ``text`` is neither a duration
    (``[0-9]+(s|m|h|d)``) nor an RFC 3339 UTC time."""
    found = _DURATION.fullmatch(text)
    if found:
        digits, unit = found.groups()
        if len(digits) > 12:  # longer than any age: everything
            return -_FAR
        return max(now - int(digits) * _UNIT[unit], -_FAR)
    found = _TIME.fullmatch(text)
    if not found:
        return None
    try:
        year, month, day, hour, minute, second = (int(x) for x in found.groups())
        moment = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError:  # month 13, hour 24, second 60 (no leap seconds), year 0000
        return None
    return int(moment.timestamp())


@dataclass(frozen=True)
class HistoryFilters:
    since: int | None = None
    source: str | None = None
    destination: str | None = None
    level: str | None = None
    outcome: str | None = None
    labels: tuple[str, ...] = ()
    limit: int = DEFAULT_LIMIT


@dataclass
class HistoryResult:
    rows: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    skipped: int = 0  # rows with a column outside its grammar


_COLUMNS = (
    "d.id, d.ts, d.source, d.hook, d.object_id, d.rule, d.destination, d.level, d.outcome,"
    " d.reason, d.code, d.attempts, d.count, d.event_id, d.dedupe, d.labels, d.surfaced,"
    " d.deliver_after, d.title, d.fallback_of"
)
_LABEL_CLAUSE = (
    "CASE WHEN json_valid(d.labels) THEN"
    " EXISTS (SELECT 1 FROM json_each(d.labels) WHERE json_each.value = ?) ELSE 0 END"
)


def _query(f: HistoryFilters) -> tuple[str, tuple[object, ...]]:
    clauses: list[str] = []
    params: list[object] = []
    for clause, value in (
        ("d.ts >= ?", f.since),
        ("d.source = ?", f.source),
        ("d.destination = ?", f.destination),
        ("d.level = ?", f.level),
        ("d.outcome = ?", f.outcome),
    ):
        if value is not None:
            clauses.append(clause)
            params.append(value)
    for label in f.labels:
        clauses.append(_LABEL_CLAUSE)
        params.append(label)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = (
        "SELECT "
        + _COLUMNS
        + " FROM deliveries d"
        + where
        + " ORDER BY d.ts DESC, d.id DESC LIMIT ?"
    )
    params.append(f.limit)
    return sql, tuple(params)


def run_history(gateway: Gateway, filters: HistoryFilters) -> HistoryResult:
    """The rows, newest first. ``StoreError`` and ``HomeError`` propagate (exit 1 and 2)."""
    result = HistoryResult()
    if len(set(filters.labels)) > MAX_LABELS:
        return result
    store = open_readonly(gateway)
    if store is None:
        return result
    with store:
        # a title or any column may hold bytes that are not UTF-8: replace them, never fail
        store.conn.text_factory = lambda raw: raw.decode("utf-8", "replace")
        sql, params = _query(
            HistoryFilters(
                filters.since,
                filters.source,
                filters.destination,
                filters.level,
                filters.outcome,
                tuple(dict.fromkeys(filters.labels)),
                filters.limit,
            )
        )
        for row in store.read(sql, params):
            doc = _document(row)
            if doc is None:
                result.skipped += 1
            else:
                result.rows.append(doc)
    return result


# --- one row -----------------------------------------------------------------------------------


def _text(value: object, pattern: re.Pattern[str], *, null: bool = False) -> str | None | bool:
    """``value`` when it is a string of the grammar, ``None`` when it is NULL and allowed,
    else ``False``."""
    if value is None:
        return None if null else False
    if isinstance(value, str) and pattern.fullmatch(value):
        return value
    return False


def _int(value: object, low: int, *, null: bool = False) -> int | None | bool:
    if value is None:
        return None if null else False
    if type(value) is int and low <= value <= _FAR:
        return value
    return False


def _document(row: tuple[object, ...]) -> dict[str, Any] | None:
    """The ``--json`` object of one row, or ``None`` when a column is outside its grammar."""
    (
        rid, ts, source, hook, object_id, rule, destination, level, outcome, reason, code,
        attempts, count, event_id, dedupe, labels, surfaced, deliver_after, title, fallback_of,
    ) = row  # fmt: skip
    ok: dict[str, Any] = {
        "id": _int(rid, 1),
        "source": _text(source, SOURCE),
        "hook": _text(hook, HOOK, null=True),
        "rule": _text(rule, RULE, null=True),
        "destination": _text(destination, DESTINATION, null=True),
        "level": level if isinstance(level, str) and level in LEVELS else False,
        "outcome": outcome if isinstance(outcome, str) and outcome in OUTCOMES else False,
        "reason": None
        if reason is None
        else (reason if isinstance(reason, str) and reason in REASONS else False),
        "code": _text(code, CODE, null=True),
        "attempts": _int(attempts, 0),
        "count": _int(count, 1),
        "dedupe": _text(dedupe, DIGEST, null=True),
        "deliver_after": _int(deliver_after, 0, null=True),
        "fallback_of": _int(fallback_of, 1, null=True),
    }
    if any(v is False for v in ok.values()):
        return None
    stamp = _int(ts, 0)
    if stamp is False or stamp is None:
        return None
    try:
        iso = timestamp(stamp)
    except (OverflowError, ValueError):
        return None
    if surfaced not in (0, 1) or type(surfaced) is not int:
        return None
    object_out = _object_id(object_id)
    event_out = _event_id(event_id)
    label_out = _labels(labels)
    if object_out is False or event_out is False or label_out is None:
        return None
    # a rule_drop and a no_rule row have no destination and are dropped (history.json allOf)
    if ok["reason"] in ("rule_drop", "no_rule"):
        if ok["destination"] is not None or ok["outcome"] != "dropped":
            return None
    elif ok["destination"] is None:
        return None
    return {
        "id": ok["id"],
        "ts": iso,
        "source": ok["source"],
        "hook": ok["hook"],
        "object_id": object_out,
        "rule": ok["rule"],
        "destination": ok["destination"],
        "level": ok["level"],
        "outcome": ok["outcome"],
        "reason": ok["reason"],
        "code": ok["code"],
        "attempts": ok["attempts"],
        "count": ok["count"],
        "event_id": event_out,
        "dedupe": ok["dedupe"],
        "labels": label_out,
        "surfaced": bool(surfaced),
        "deliver_after": ok["deliver_after"],
        "title": title[:100] if isinstance(title, str) else None,
        "fallback_of": ok["fallback_of"],
    }


def _object_id(value: object) -> int | str | None | bool:
    """An integer or a plain string; any other stored value is shown as null (the interface:
    "else null"); only a value of the right type outside the grammar is refused."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if -(1 << 63) <= value < 1 << 63 else None
    if isinstance(value, str):
        return value if PLAIN.fullmatch(value) else False
    return None


def _event_id(value: object) -> int | str | None | bool:
    """The stored JSON text of an integer or a plain string."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 300:
        return False
    try:
        decoded = json.loads(value)
    except ValueError:
        return False
    if type(decoded) is int:
        return decoded if -(1 << 63) <= decoded < 1 << 63 else False
    if isinstance(decoded, str) and PLAIN.fullmatch(decoded):
        return decoded
    return False


def _labels(value: object) -> list[str] | None:
    """The stored JSON array of at most 16 labels; a NULL column is no labels; else ``None``."""
    if value is None:
        return []
    if not isinstance(value, str) or len(value) > 1024:
        return None
    try:
        decoded = json.loads(value)
    except ValueError:
        return None
    if not isinstance(decoded, list) or len(decoded) > MAX_LABELS:  # pyright: ignore[reportUnknownArgumentType]
        return None
    out: list[str] = []
    for item in decoded:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, str) or not LABEL.fullmatch(item):
            return None
        out.append(item)
    return out
