"""The queue helpers ``flush`` steps 4 to 7 use (#23): the lease predicate through public
functions (due rows, claiming one row, expiry, quiet drops and postponing)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from notice_route import queue as q
from notice_route.gateway import FakeClock, Gateway
from notice_route.queue import Lease
from notice_route.store import Store, open_store

NOW = 1_700_000_000
L = 20


@pytest.fixture
def gw(home: Path) -> Gateway:
    return Gateway(passwd_home=lambda: str(home), clock=FakeClock())


@pytest.fixture
def store(gw: Gateway) -> Iterator[Store]:
    with open_store(gw) as s:
        yield s


def lease(now: int = NOW) -> Lease:
    return Lease(now, L)


def add(
    conn: sqlite3.Connection,
    destination: str = "d",
    level: str = "info",
    *,
    kind: q.Kind = "single",
    deliver_after: int = NOW,
    created_at: int = NOW,
    claim: str | None = None,
    ls: Lease | None = None,
) -> tuple[int, int]:
    did = q.insert_delivery(
        conn, ts=NOW, source="s", level=level, outcome="deferred", destination=destination
    )
    res = q.enqueue(
        conn,
        delivery_id=did,
        kind=kind,
        message="m",
        level=level,
        source="s",
        deliver_after=deliver_after,
        created_at=created_at,
        lease=ls or lease(),
        claim_token=claim,
        cap=1000,
    )
    return did, res.row_id


# --- due_rows --------------------------------------------------------------------------------


def test_due_rows_lists_unclaimed_due_rows_oldest_first_without_changing_them(store: Store) -> None:
    with store.write() as conn:
        _, a = add(conn)
        _, later = add(conn, deliver_after=NOW + 1)
        _, claimed = add(conn, claim="t")
        _, b = add(conn, kind="held")
        _, other = add(conn, "other")
        rows = q.due_rows(conn, "d", lease())
        assert [r.id for r in rows] == [a, b]
        assert {later, claimed, other}.isdisjoint(r.id for r in rows)
        assert [r.id for r in q.due_rows(conn, "d", lease(), kind="held")] == [b]
        assert [r.id for r in q.due_rows(conn, "d", lease(), limit=1)] == [a]
        assert all(r.claimed_by is None for r in q.due_rows(conn, "d", lease()))


def test_a_lapsed_claim_is_due_and_a_live_one_is_not(store: Store) -> None:
    with store.write() as conn:
        _, live = add(conn, claim="t", ls=lease(NOW))  # expires NOW + L
        _, lapsed = add(conn, claim="u", ls=lease(NOW - 100))
        ids = [r.id for r in q.due_rows(conn, "d", lease())]
    assert ids == [lapsed] and live not in ids


def test_due_rows_after_id_starts_past_the_given_row_id(store: Store) -> None:
    with store.write() as conn:
        _, a = add(conn)
        _, b = add(conn)
        _, c = add(conn)
        assert [r.id for r in q.due_rows(conn, "d", lease(), after_id=a)] == [b, c]
        assert [r.id for r in q.due_rows(conn, "d", lease(), after_id=b, limit=1)] == [c]
        assert q.due_rows(conn, "d", lease(), after_id=c) == []


# --- claim_row -------------------------------------------------------------------------------


def test_claim_row_takes_an_unclaimed_row_with_a_fresh_claim_id(store: Store) -> None:
    with store.write() as conn:
        _, a = add(conn)
        row = q.claim_row(conn, a, lease(), "claim1")
        assert row is not None
        assert (row.claimed_by, row.claim_expires) == ("claim1", NOW + L)
        assert q.claim_row(conn, a, lease(), "claim2") is None  # a live claim is not taken
        assert q.get_row(conn, a).claimed_by == "claim1"  # type: ignore[union-attr]


def test_claim_row_takes_over_a_lapsed_claim_and_fences_the_old_claim_id(store: Store) -> None:
    with store.write() as conn:
        _, a = add(conn, claim="old", ls=lease(NOW - 100))
        row = q.claim_row(conn, a, lease(), "new")
        assert row is not None and row.claimed_by == "new"
        assert q.complete_row(conn, a, "old") is False
        assert q.complete_row(conn, a, "new") is True


def test_claim_row_of_a_missing_row_is_none(store: Store) -> None:
    with store.write() as conn:
        assert q.claim_row(conn, 12345, lease(), "t") is None


# --- expiry ----------------------------------------------------------------------------------


def test_expire_unclaimed_ends_rows_24_hours_old_or_more_and_keeps_the_rest(store: Store) -> None:
    day = 86400
    with store.write() as conn:
        d_old, old = add(conn, created_at=NOW - day)
        d_older, older = add(conn, created_at=NOW - day - 5, kind="held")
        _, young = add(conn, created_at=NOW - day + 1)
        _, other = add(conn, "other", created_at=NOW - 2 * day)
        _, claimed = add(conn, created_at=NOW - 2 * day, claim="t")  # a live claim is never expired
        expired = q.expire_unclaimed(conn, lease(), destination="d")
        assert [(e.queue_id, e.delivery_id, e.destination) for e in expired] == [
            (old, d_old, "d"), (older, d_older, "d"),
        ]  # fmt: skip
        assert q.get_row(conn, old) is None and q.get_row(conn, older) is None
        assert all(q.get_row(conn, i) is not None for i in (young, other, claimed))
        got = conn.execute(
            "SELECT outcome, code, deliver_after FROM deliveries WHERE id = ?", (d_old,)
        ).fetchone()
        assert got == ("expired", "expired", None)


def test_expired_rows_carry_their_source_and_attempts(store: Store) -> None:
    with store.write() as conn:
        did, _ = add(conn, created_at=NOW - 90000)
        conn.execute("UPDATE deliveries SET attempts = 3 WHERE id = ?", (did,))
        (e,) = q.expire_unclaimed(conn, lease(), destination="d")
    assert (e.source, e.attempts) == ("s", 3)


def test_count_expirable_counts_old_unclaimed_rows_even_when_not_due(store: Store) -> None:
    day = 86400
    with store.write() as conn:
        add(conn, created_at=NOW - day, deliver_after=NOW + 500)
        add(conn, created_at=NOW - day)
        add(conn, created_at=NOW - day + 1)
        add(conn, created_at=NOW - 2 * day, claim="t")  # a live claim is never expired
        add(conn, "other", created_at=NOW - 2 * day)
        assert q.count_expirable(conn, "d", lease()) == 2
        assert len(q.expire_unclaimed(conn, lease(), destination="d")) == 2


def test_expire_unclaimed_without_a_destination_covers_all_of_them(store: Store) -> None:
    with store.write() as conn:
        add(conn, "a", created_at=NOW - 90000)
        add(conn, "b", created_at=NOW - 90000)
        assert {e.destination for e in q.expire_unclaimed(conn, lease())} == {"a", "b"}


def test_expire_unclaimed_takes_a_lapsed_claim_and_releases_dedup(store: Store) -> None:
    with store.write() as conn:
        did, row = add(conn, created_at=NOW - 90000, claim="t", ls=lease(NOW - 100))
        conn.execute(
            "INSERT INTO dedup (destination, key_digest, window_start, window_length, delivery_id)"
            " VALUES ('d', 'x', ?, 60, ?)",
            (NOW - 10, did),
        )
        assert [e.queue_id for e in q.expire_unclaimed(conn, lease())] == [row]
        assert conn.execute("SELECT COUNT(*) FROM dedup").fetchone() == (0,)


# --- drop and postpone -----------------------------------------------------------------------


def test_drop_unclaimed_ends_the_row_dropped_with_the_reason(store: Store) -> None:
    with store.write() as conn:
        did, row = add(conn)
        assert q.drop_unclaimed(conn, row, lease(), reason="quiet") is True
        assert q.get_row(conn, row) is None
        assert conn.execute(
            "SELECT outcome, reason, code, deliver_after FROM deliveries WHERE id = ?", (did,)
        ).fetchone() == ("dropped", "quiet", None, None)
        assert q.drop_unclaimed(conn, row, lease(), reason="quiet") is False


def test_drop_unclaimed_leaves_a_live_claim_alone(store: Store) -> None:
    with store.write() as conn:
        did, row = add(conn, claim="t")
        assert q.drop_unclaimed(conn, row, lease(), reason="quiet") is False
        assert q.get_row(conn, row) is not None


def test_postpone_unclaimed_moves_deliver_after_and_keeps_the_stored_reason(store: Store) -> None:
    with store.write() as conn:
        did, row = add(conn)
        conn.execute("UPDATE deliveries SET reason = 'link_refused' WHERE id = ?", (did,))
        assert q.postpone_unclaimed(conn, row, lease(), NOW + 600) is True
        got = q.get_row(conn, row)
        assert got is not None and got.deliver_after == NOW + 600 and got.kind == "single"
        assert conn.execute(
            "SELECT outcome, reason, deliver_after FROM deliveries WHERE id = ?", (did,)
        ).fetchone() == ("deferred", "link_refused", NOW + 600)


def test_postpone_unclaimed_leaves_a_live_claim_alone(store: Store) -> None:
    with store.write() as conn:
        _, row = add(conn, claim="t")
        assert q.postpone_unclaimed(conn, row, lease(), NOW + 600) is False
        assert q.get_row(conn, row).deliver_after == NOW  # type: ignore[union-attr]


def test_count_due_counts_by_kind(store: Store) -> None:
    with store.write() as conn:
        add(conn)
        add(conn)
        add(conn, kind="held")
        add(conn, deliver_after=NOW + 1)
        add(conn, claim="t")
        assert q.count_due(conn, "d", lease()) == {"single": 2, "held": 1}
