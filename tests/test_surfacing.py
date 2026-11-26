"""The surfacing limits (#27): periods, once per slot, the 8-code limit and ``other``."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from notice_route.schema import MIGRATIONS
from notice_route.surfacing import (
    day_period,
    hour_period,
    surface_daily,
    surface_evicted,
    surface_hourly,
    surface_outcome,
)

T = int(datetime(2023, 11, 14, 22, 59, 59, tzinfo=UTC).timestamp())
HOUR_END = T + 1  # 23:00:00
DAY_END = int(datetime(2023, 11, 15, 0, 0, 0, tzinfo=UTC).timestamp())


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:", isolation_level=None)
    for step in MIGRATIONS[0]:
        assert isinstance(step, str)
        c.execute(step)
    c.execute(
        "INSERT INTO deliveries (id, ts, source, level, outcome, destination)"
        " VALUES (1, 0, 's', 'info', 'failed', 'chat')"
    )
    yield c
    c.close()


def rows(c: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return c.execute("SELECT destination, code, period FROM surfaced ORDER BY id").fetchall()


def test_periods() -> None:
    assert day_period(T) == "2023-11-14" and day_period(DAY_END) == "2023-11-15"
    assert hour_period(T) == "2023-11-14T22" and hour_period(HOUR_END) == "2023-11-14T23"
    assert hour_period(DAY_END - 1) == "2023-11-14T23"


def test_daily_once_per_code_and_day_and_marks_the_delivery_once(conn: sqlite3.Connection) -> None:
    assert surface_daily(conn, "chat", "auth_failed", 1, T)
    assert not surface_daily(conn, "chat", "auth_failed", 1, T + 0)
    assert conn.execute("SELECT surfaced FROM deliveries").fetchone() == (1,)
    assert rows(conn) == [("chat", "auth_failed", "2023-11-14")]


def test_daily_boundary_is_the_utc_midnight(conn: sqlite3.Connection) -> None:
    assert surface_daily(conn, "chat", "c", 1, DAY_END - 1)
    assert not surface_daily(conn, "chat", "c", 1, DAY_END - 1)
    assert surface_daily(conn, "chat", "c", 1, DAY_END)


def test_hourly_boundary_is_the_utc_hour(conn: sqlite3.Connection) -> None:
    assert surface_hourly(conn, "chat", "expired", 1, T)
    assert not surface_hourly(conn, "chat", "expired", 1, T)
    assert surface_hourly(conn, "chat", "expired", 1, HOUR_END)
    assert surface_hourly(conn, "other-dest", "expired", 1, T)
    assert [r[2] for r in rows(conn)] == ["2023-11-14T22", "2023-11-14T23", "2023-11-14T22"]


def test_hourly_rejects_a_daily_code(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        surface_hourly(conn, "chat", "auth_failed", 1, T)


def test_ninth_code_becomes_other_once(conn: sqlite3.Connection) -> None:
    for i in range(8):
        assert surface_daily(conn, "chat", f"c{i}", 1, T)
    assert surface_daily(conn, "chat", "c8", 1, T)
    assert not surface_daily(conn, "chat", "c9", 1, T)
    assert not surface_daily(conn, "chat", "c0", 1, T)  # a known code stays once
    assert sorted(c for _, c, _ in rows(conn)) == sorted([*(f"c{i}" for i in range(8)), "other"])
    assert surface_daily(conn, "chat", "c9", 1, DAY_END)  # the next day starts over


def test_hourly_codes_do_not_count_toward_the_eight(conn: sqlite3.Connection) -> None:
    for code in ("rate_capped", "env_missing", "overflow", "expired"):
        assert surface_hourly(conn, "chat", code, 1, T)
    for i in range(8):
        assert surface_daily(conn, "chat", f"c{i}", 1, T)
    assert surface_daily(conn, "chat", "c8", 1, T)  # as other
    assert rows(conn)[-1][1] == "other"


def test_limit_is_per_destination(conn: sqlite3.Connection) -> None:
    for i in range(8):
        surface_daily(conn, "a", f"c{i}", 1, T)
    assert surface_daily(conn, "b", "c8", 1, T)
    assert rows(conn)[-1] == ("b", "c8", "2023-11-14")


@pytest.mark.parametrize(
    "code", [None, "expired", "overflow", "rate_capped", "env_missing", "BAD!"]
)
def test_failed_with_a_reserved_or_missing_code_is_other(
    conn: sqlite3.Connection, code: str | None
) -> None:
    assert surface_outcome(conn, "chat", "failed", code, 1, T)
    assert rows(conn) == [("chat", "other", "2023-11-14")]


def test_outcome_keys(conn: sqlite3.Connection) -> None:
    assert surface_outcome(conn, "chat", "deferred", "rate_capped", 1, T)
    assert surface_outcome(conn, "chat", "dropped", "overflow", 1, T)
    assert surface_outcome(conn, "chat", "expired", "expired", 1, T)
    assert surface_outcome(conn, "chat", "unknown", "killed", 1, T)
    assert not surface_outcome(conn, "chat", "sent", None, 1, T)
    assert not surface_outcome(conn, "chat", "dropped", "quiet", 1, T)
    assert [c for _, c, _ in rows(conn)] == ["rate_capped", "overflow", "expired", "killed"]


def test_evicted_is_one_overflow_for_the_first(conn: sqlite3.Connection) -> None:
    assert not surface_evicted(conn, "chat", [], T)
    assert surface_evicted(conn, "chat", [1, 2, 3], T)
    assert conn.execute("SELECT delivery_id FROM surfaced").fetchall() == [(1,)]
