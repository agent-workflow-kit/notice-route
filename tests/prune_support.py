"""Helpers for the tests of ``prune`` and ``history`` (#26): seeding every table of a real state
directory with rows of a chosen age, and running the real entry point."""

from __future__ import annotations

import json
from typing import Any

from notice_route.store import open_store
from tests.send_support import NOW, Rig
from tests.test_cli_rules_validate import schema_errors

DAY = 86400
TABLES = ("history", "event_ids", "dedup", "surfaced", "rate", "queue")
_COUNT_SQL = {
    "history": "SELECT COUNT(*) FROM deliveries",
    "event_ids": "SELECT COUNT(*) FROM event_ids",
    "dedup": "SELECT COUNT(*) FROM dedup",
    "surfaced": "SELECT COUNT(*) FROM surfaced",
    "rate": "SELECT COUNT(*) FROM rate",
    "queue": "SELECT COUNT(*) FROM queue",
}


def add_delivery(
    rig: Rig,
    ts: int,
    *,
    outcome: str = "sent",
    destination: str | None = "chat",
    source: str = "s",
    level: str = "info",
    reason: str | None = None,
    **columns: Any,
) -> int:
    """One ``deliveries`` row of timestamp ``ts``; returns its id."""
    values: dict[str, Any] = {
        "ts": ts,
        "source": source,
        "level": level,
        "outcome": outcome,
        "destination": destination,
        "reason": reason,
        **columns,
    }
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    with open_store(rig.gateway()) as store, store.write() as conn:
        cur = conn.execute(f"INSERT INTO deliveries ({names}) VALUES ({marks})", tuple(values.values()))
        assert cur.lastrowid is not None
        return cur.lastrowid


def add_many_deliveries(rig: Rig, count: int, ts: int) -> None:
    with open_store(rig.gateway()) as store, store.write() as conn:
        conn.executemany(
            "INSERT INTO deliveries (ts, source, level, outcome, destination)"
            " VALUES (?, 's', 'info', 'sent', 'chat')",
            [(ts,)] * count,
        )


def add_event_id(rig: Rig, seen_at: int, n: int = 0, delivery_id: int | None = None) -> None:
    with open_store(rig.gateway()) as store, store.write() as conn:
        conn.execute(
            "INSERT INTO event_ids (destination, source, hook, event_id, repeat_digest, seen_at,"
            " delivery_id) VALUES ('chat', 's', '', ?, ?, ?, ?)",
            (str(n), "ab" * 32, seen_at, delivery_id),
        )


def add_many_event_ids(rig: Rig, count: int, seen_at: int) -> None:
    with open_store(rig.gateway()) as store, store.write() as conn:
        conn.executemany(
            "INSERT INTO event_ids (destination, source, hook, event_id, repeat_digest, seen_at)"
            " VALUES ('chat', 's', '', ?, ?, ?)",
            [(str(i), "ab" * 32, seen_at) for i in range(count)],
        )


def add_window(rig: Rig, start: int, length: int, key: str = "a", delivery_id: int | None = None) -> None:
    digest = (key * 64)[:64]
    with open_store(rig.gateway()) as store, store.write() as conn:
        conn.execute(
            "INSERT INTO dedup (destination, key_digest, window_start, window_length, delivery_id)"
            " VALUES ('chat', ?, ?, ?, ?)",
            (digest, start, length, delivery_id),
        )


def add_surfaced(
    rig: Rig, period: str, *, processed: int = 1, code: str = "failed", delivery_id: int | None = None
) -> None:
    with open_store(rig.gateway()) as store, store.write() as conn:
        conn.execute(
            "INSERT INTO surfaced (destination, code, period, delivery_id, processed)"
            " VALUES ('chat', ?, ?, ?, ?)",
            (code, period, delivery_id, processed),
        )


def add_rate(rig: Rig, hour: int, *, destination: str = "chat", cls: str = "other", n: int = 3) -> None:
    with open_store(rig.gateway()) as store, store.write() as conn:
        conn.execute(
            "INSERT INTO rate (destination, hour, class, count) VALUES (?, ?, ?, ?)",
            (destination, hour, cls, n),
        )


def counts(rig: Rig) -> dict[str, int]:
    return {name: rig.query(sql)[0][0] for name, sql in _COUNT_SQL.items()}


def prune(rig: Rig, *extra: str, env: dict[str, str] | None = None) -> tuple[int, dict[str, Any], str]:
    """``prune --json``; the document is validated against its schema."""
    code, out, err = rig.run("prune", "--json", *extra, env=env)
    assert out.endswith("\n") and out.count("\n") == 1, out
    doc = json.loads(out)
    if "error" in doc:
        assert schema_errors(doc, "error.json") == []
    else:
        assert schema_errors(doc, "prune.json") == [], doc
    return code, doc, err


def deleted(**some: int) -> dict[str, int]:
    return {name: some.get(name, 0) for name in TABLES}


__all__ = ["DAY", "NOW", "TABLES"]
