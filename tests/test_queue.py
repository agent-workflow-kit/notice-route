"""Queue rows, claims, leases, the 100-row cap and eviction (#16)."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from notice_route import queue as q
from notice_route.gateway import FakeClock, Gateway
from notice_route.queue import Lease
from notice_route.store import Store, open_store
from notice_route.validate import QuietWindow

NOW = 1_700_000_000
L = 20  # lease length used by most tests (send_worst_case_s = 10)


@pytest.fixture
def gw(home: Path) -> Gateway:
    return Gateway(passwd_home=lambda: str(home), clock=FakeClock())


@pytest.fixture
def store(gw: Gateway) -> Iterator[Store]:
    with open_store(gw) as s:
        yield s


def lease(now: int = NOW, length: int = L) -> Lease:
    return Lease(now, length)


def delivery(conn: sqlite3.Connection, destination: str = "d", level: str = "info") -> int:
    return q.insert_delivery(
        conn,
        ts=NOW,
        source="s",
        level=level,
        outcome="deferred",
        destination=destination,
    )


def add(
    conn: sqlite3.Connection,
    destination: str = "d",
    level: str = "info",
    *,
    kind: q.Kind = "held",
    deliver_after: int = NOW,
    message: str = "m",
    token: str | None = None,
    ls: Lease | None = None,
) -> tuple[int, q.Enqueued]:
    did = delivery(conn, destination, level)
    res = q.enqueue(
        conn,
        delivery_id=did,
        kind=kind,
        message=message,
        level=level,
        source="s",
        deliver_after=deliver_after,
        created_at=NOW,
        lease=ls or lease(),
        claim_token=token,
    )
    return did, res


def n_queue(store: Store) -> int:
    return int(store.read("SELECT COUNT(*) FROM queue")[0][0])  # type: ignore[call-overload]


def delivery_state(store: Store, did: int) -> tuple[object, ...]:
    return store.read("SELECT outcome, reason, code FROM deliveries WHERE id = ?", (did,))[0]


# --- Lease -----------------------------------------------------------------------------------


def test_lease_floors_the_clock_and_doubles_the_rounded_up_worst_case() -> None:
    ls = Lease.at(1000.9, 10.2)
    assert (ls.now, ls.length, ls.expires) == (1000, 22, 1022)


@pytest.mark.parametrize("wall", [float("nan"), float("inf"), -float("inf"), 2.0**41, True, "1"])
def test_lease_refuses_a_bad_clock_reading(wall: object) -> None:
    with pytest.raises(ValueError):
        Lease.at(wall, 10)  # type: ignore[arg-type]


@pytest.mark.parametrize("worst", [0, -1, float("nan"), float("inf"), 2.0**21, True, None])
def test_lease_refuses_a_bad_worst_case(worst: object) -> None:
    with pytest.raises(ValueError):
        Lease.at(NOW, worst)  # type: ignore[arg-type]


def test_lease_refuses_huge_ints_as_value_errors() -> None:
    with pytest.raises(ValueError):
        Lease.at(10**400, 10)
    with pytest.raises(ValueError):
        Lease.at(NOW, 10**400)


def test_lease_constructor_refuses_oddities() -> None:
    for now, length in ((True, 5), (2**63, 5), (NOW, 0), (NOW, 2**40), (NOW, True)):
        with pytest.raises(ValueError):
            Lease(now, length)  # type: ignore[arg-type]


# --- deliveries and trimming -----------------------------------------------------------------


def test_insert_delivery_trims_but_keeps_the_new_row_and_rows_with_queue_rows(
    store: Store,
) -> None:
    with store.write() as conn:
        queued, _ = add(conn)
        older = [
            q.insert_delivery(conn, ts=1, source="s", level="info", outcome="sent", cap=100)
            for _ in range(3)
        ]
    with store.write() as conn:
        new = q.insert_delivery(conn, ts=2, source="s", level="info", outcome="sent", cap=1)
    ids = {r[0] for r in store.read("SELECT id FROM deliveries")}
    assert queued in ids and new in ids  # protected and just inserted
    assert not ids & set(older)  # the oldest unprotected went (batch limit permitting)


def test_insert_delivery_refuses_bad_level_and_ts(store: Store) -> None:
    with store.write() as conn:
        with pytest.raises(ValueError):
            q.insert_delivery(conn, ts=1, source="s", level="loud", outcome="sent")
        with pytest.raises(ValueError):
            q.insert_delivery(conn, ts=True, source="s", level="info", outcome="sent")  # type: ignore[arg-type]


# --- enqueue ---------------------------------------------------------------------------------


def test_enqueue_stores_the_rendered_row_unclaimed(store: Store) -> None:
    with store.write() as conn:
        did = delivery(conn, "dest", "warn")
        res = q.enqueue(
            conn,
            delivery_id=did,
            kind="single",
            title="t",
            message="msg",
            link="https://example.org/x",
            level="warn",
            source="src",
            rule="r",
            dedupe="abc",
            deliver_after=NOW + 5,
            created_at=NOW,
            lease=lease(),
        )
        row = q.get_row(conn, res.row_id)
    assert row == q.QueueRow(
        res.row_id, did, "dest", "single", "t", "msg", "https://example.org/x", "warn", "src",
        "r", "abc", NOW + 5, NOW, None, None,
    )  # fmt: skip
    assert not res.row_evicted and res.evicted == ()


def test_enqueue_with_a_claim_token_is_born_claimed_and_not_counted(store: Store) -> None:
    with store.write() as conn:
        _, res = add(conn, kind="single", token="tok")
        row = q.get_row(conn, res.row_id)
        assert row is not None
        assert (row.claimed_by, row.claim_expires) == ("tok", NOW + L)
        assert q.unclaimed_count(conn, "d", lease()) == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "digest"},
        {"level": "loud"},
        {"deliver_after": True},
        {"deliver_after": 2**63},
        {"deliver_after": 1.5},
        {"created_at": -(2**41)},
        {"claim_token": ""},
        {"claim_token": "x" * 129},
        {"delivery_id": 10**9},
        {"message": 5},
        {"title": 5},
        {"link": b"x"},
        {"source": None},
        {"source": 7},
    ],
)
def test_enqueue_refuses_bad_arguments_and_writes_nothing(
    store: Store, kwargs: dict[str, object]
) -> None:
    with store.write() as conn:
        did = delivery(conn)
    base: dict[str, object] = {
        "delivery_id": did, "kind": "held", "message": "m", "level": "info", "source": "s",
        "deliver_after": NOW, "created_at": NOW, "lease": lease(),
    }  # fmt: skip
    with pytest.raises(ValueError):
        with store.write() as conn:
            q.enqueue(conn, **{**base, **kwargs})  # type: ignore[arg-type]
    assert n_queue(store) == 0


def test_enqueue_refuses_a_delivery_without_destination(store: Store) -> None:
    with store.write() as conn:
        did = q.insert_delivery(conn, ts=1, source="s", level="info", outcome="dropped")
    with pytest.raises(ValueError):
        with store.write() as conn:
            q.enqueue(
                conn, delivery_id=did, kind="held", message="m", level="info", source="s",
                deliver_after=1, created_at=1, lease=lease(),
            )  # fmt: skip


HOSTILE = [
    "'; DROP TABLE queue; --",
    "x' OR '1'='1",
    "\x00nul\x00inside",
    "‮\u0000\U0001f600 é ﻿",
    "?" * 3 + ":name @p $q %s {0}",
    "a" * 100_000,
]


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_text_round_trips_as_data(store: Store, text: str) -> None:
    with store.write() as conn:
        did = delivery(conn, destination=text if len(text) < 100 else "d")
        res = q.enqueue(
            conn, delivery_id=did, kind="held", title=text, message=text, link=text, level="info",
            source=text, rule=text, dedupe=text, deliver_after=NOW, created_at=NOW, lease=lease(),
        )  # fmt: skip
    with store.write() as conn:
        dest = conn.execute("SELECT destination FROM deliveries WHERE id = ?", (did,)).fetchone()[0]
        rows = q.claim_due(conn, dest, lease(), "tok")
    assert [r.id for r in rows] == [res.row_id]
    assert (rows[0].title, rows[0].message, rows[0].link) == (text, text, text)
    assert (rows[0].source, rows[0].rule, rows[0].dedupe) == (text, text, text)
    assert set(r[0] for r in store.read("SELECT name FROM sqlite_master")) >= {
        "queue",
        "deliveries",
    }


def test_an_oversized_value_fails_and_rolls_the_whole_transaction_back(store: Store) -> None:
    with pytest.raises(sqlite3.Error):
        with store.write() as conn:
            add(conn, message="ok")
            add(conn, message="y" * (3 << 20))
    assert n_queue(store) == 0
    assert store.read("SELECT COUNT(*) FROM deliveries") == [(0,)]


def test_a_lone_surrogate_is_refused_not_stored(store: Store) -> None:
    with pytest.raises(ValueError):
        with store.write() as conn:
            add(conn, message="\ud800")
    assert n_queue(store) == 0


def test_an_error_after_eviction_rolls_the_eviction_back(store: Store) -> None:
    with store.write() as conn:
        for _ in range(100):
            add(conn, level="warn")
    with pytest.raises(RuntimeError):
        with store.write() as conn:
            _, res = add(conn, level="info")
            assert res.row_evicted
            raise RuntimeError("after")
    assert n_queue(store) == 100
    assert store.read("SELECT COUNT(*) FROM deliveries WHERE outcome = 'dropped'") == [(0,)]


# --- the cap ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("rows", "dropped"), [(99, 0), (100, 0), (101, 1)])
def test_cap_boundary(store: Store, rows: int, dropped: int) -> None:
    with store.write() as conn:
        results = [add(conn)[1] for _ in range(rows)]
    assert n_queue(store) == rows - dropped
    assert sum(len(r.evicted) for r in results) == dropped
    n_dropped = store.read("SELECT COUNT(*) FROM deliveries WHERE outcome = 'dropped'")[0][0]
    assert n_dropped == dropped


def test_a_row_just_queued_at_the_lowest_level_is_the_one_dropped(store: Store) -> None:
    with store.write() as conn:
        for _ in range(100):
            add(conn, level="warn")
        did, res = add(conn, level="info")
    assert res.row_evicted and res.evicted[0].level == "info"
    assert delivery_state(store, did) == ("dropped", "overflow", "overflow")
    assert q.get_row(store.conn, res.row_id) is None


def test_the_oldest_of_the_lowest_level_goes_and_the_new_row_stays(store: Store) -> None:
    with store.write() as conn:
        first, a = add(conn, level="info")
        for _ in range(99):
            add(conn, level="error")
        new, b = add(conn, level="info")  # 101 rows: two infos, the older one goes
    assert [e.queue_id for e in b.evicted] == [a.row_id]
    assert not b.row_evicted
    assert delivery_state(store, first) == ("dropped", "overflow", "overflow")
    assert delivery_state(store, new)[0] == "deferred"


@pytest.mark.parametrize("levels", [("info", "warn", "action", "error")])
def test_levels_are_dropped_lowest_first(store: Store, levels: tuple[str, ...]) -> None:
    order: list[str] = []
    with store.write() as conn:
        for level in reversed(levels):  # error first: the oldest rows are the highest
            for _ in range(25):
                add(conn, level=level)
        for _ in range(100):
            order += [e.level for e in add(conn, level="error")[1].evicted]
    assert order == ["info"] * 25 + ["warn"] * 25 + ["action"] * 25 + ["error"] * 25


def test_only_errors_overflow_the_oldest_error_goes(store: Store) -> None:
    with store.write() as conn:
        ids = [add(conn, level="error")[1].row_id for _ in range(100)]
        _, res = add(conn, level="error")
    assert [e.queue_id for e in res.evicted] == [ids[0]]


def test_one_enforce_drops_every_excess_row_and_records_why(store: Store) -> None:
    with store.write() as conn:
        dids = []
        for _ in range(105):  # bypass the cap with a huge cap, then enforce the real one
            did = delivery(conn)
            dids.append(did)
            q.enqueue(
                conn, delivery_id=did, kind="held", message="plain text", level="info",
                source="s", deliver_after=NOW, created_at=NOW, lease=lease(), cap=1000,
            )  # fmt: skip
        evicted = q.enforce_cap(conn, "d", lease())
    assert [e.delivery_id for e in evicted] == dids[:5]
    assert n_queue(store) == 100
    for did in dids[:5]:
        assert delivery_state(store, did) == ("dropped", "overflow", "overflow")
    assert store.read(
        "SELECT COUNT(*) FROM queue WHERE delivery_id IN (?, ?)", tuple(dids[:2])
    ) == [(0,)]


def test_cap_is_per_destination(store: Store) -> None:
    with store.write() as conn:
        for _ in range(100):
            add(conn, "a")
        for _ in range(100):
            add(conn, "b")
        _, res = add(conn, "a")
    assert len(res.evicted) == 1 and res.evicted[0].destination == "a"
    assert store.read(
        "SELECT COUNT(*) FROM queue q JOIN deliveries d ON d.id = q.delivery_id"
        " WHERE d.destination = 'b'"
    ) == [(100,)]


def test_claimed_rows_are_neither_counted_nor_evicted(store: Store) -> None:
    with store.write() as conn:
        claimed = [add(conn, kind="single", token="t", level="info")[1].row_id for _ in range(50)]
        for _ in range(100):
            _, res = add(conn, level="error")
            assert res.evicted == ()
        assert q.unclaimed_count(conn, "d", lease()) == 100
        _, res = add(conn, level="error")
        assert len(res.evicted) == 1 and res.evicted[0].queue_id not in claimed
    assert all(q.get_row(store.conn, c) is not None for c in claimed)


def test_a_lapsed_claim_counts_as_unclaimed_and_can_be_evicted(store: Store) -> None:
    with store.write() as conn:
        lapsed = add(conn, token="gone", level="info")[1].row_id  # claim_expires NOW + L
        for _ in range(100):
            add(conn, level="error")  # the first ones: live claim, so 100 unclaimed fit
    later = lease(NOW + L)  # the claim's expiry second
    with store.write() as conn:
        assert q.unclaimed_count(conn, "d", later) == 101
        evicted = q.enforce_cap(conn, "d", later)
    assert [e.queue_id for e in evicted] == [lapsed]


def test_queue_rows_keep_their_deliveries_rows_from_trimming(store: Store) -> None:
    with store.write() as conn:
        queued = [add(conn)[0] for _ in range(5)]
    with store.write() as conn:
        for _ in range(6):
            q.insert_delivery(conn, ts=1, source="s", level="info", outcome="dropped", cap=0)
    ids = {r[0] for r in store.read("SELECT id FROM deliveries")}
    assert set(queued) <= ids


def test_an_evicted_rows_delivery_is_no_longer_protected_from_trimming(store: Store) -> None:
    with store.write() as conn:
        for _ in range(100):
            add(conn, level="warn")
        did, res = add(conn, level="info")
    assert res.row_evicted
    with store.write() as conn:
        q.insert_delivery(conn, ts=1, source="s", level="info", outcome="sent", cap=1)
    assert store.read("SELECT COUNT(*) FROM deliveries WHERE id = ?", (did,)) == [(0,)]


# --- claims and leases -----------------------------------------------------------------------


def test_claim_due_takes_only_due_unclaimed_rows_of_the_destination_oldest_row_id_first(
    store: Store,
) -> None:
    with store.write() as conn:
        late = add(conn, deliver_after=NOW + 1)[1].row_id
        a = add(conn, deliver_after=NOW - 5)[1].row_id
        b = add(conn, deliver_after=NOW - 10)[1].row_id
        other = add(conn, "other")[1].row_id
        live = add(conn, deliver_after=NOW - 20, token="x")[1].row_id
    with store.write() as conn:
        rows = q.claim_due(conn, "d", lease(), "me")
    assert [r.id for r in rows] == [a, b]
    assert all((r.claimed_by, r.claim_expires) == ("me", NOW + L) for r in rows)
    with store.write() as conn:
        assert all(q.get_row(conn, i).claimed_by != "me" for i in (late, other, live))  # type: ignore[union-attr]
        assert q.claim_due(conn, "d", lease(), "again") == []


def test_claim_due_kind_and_limit(store: Store) -> None:
    with store.write() as conn:
        s1 = add(conn, kind="single")[1].row_id
        add(conn, kind="held")
        add(conn, kind="held")
    with store.write() as conn:
        assert [r.id for r in q.claim_due(conn, "d", lease(), "t1", kind="single")] == [s1]
        assert len(q.claim_due(conn, "d", lease(), "t2", kind="held", limit=1)) == 1
        assert len(q.claim_due(conn, "d", lease(), "t3", kind="held")) == 1
    for bad in ({"kind": "x"}, {"limit": 0}, {"limit": True}):
        with pytest.raises(ValueError):
            with store.write() as conn:
                q.claim_due(conn, "d", lease(), "t", **bad)  # type: ignore[arg-type]


def test_a_claim_is_live_until_its_expiry_second_then_the_row_is_due_again(store: Store) -> None:
    with store.write() as conn:
        add(conn)
        assert len(q.claim_due(conn, "d", lease(NOW), "a")) == 1
        assert q.claim_due(conn, "d", lease(NOW + L - 1), "b") == []
        taken = q.claim_due(conn, "d", lease(NOW + L), "b")
    assert [r.claimed_by for r in taken] == ["b"]
    assert taken[0].claim_expires == NOW + 2 * L


def test_a_clock_that_moved_backwards_does_not_lengthen_a_lease(store: Store) -> None:
    with store.write() as conn:
        row = add(conn, deliver_after=0)[1].row_id
        q.claim_due(conn, "d", lease(NOW), "a")  # expires NOW + L
    with store.write() as conn:
        # the clock went back 1000 s: the claim would last L + 1000 s from now, more than a lease
        taken = q.claim_due(conn, "d", lease(NOW - 1000), "b")
    assert [r.id for r in taken] == [row]
    assert taken[0].claim_expires == NOW - 1000 + L  # and the new lease is capped from "now"


def test_a_small_backward_step_also_lapses_the_claim_rather_than_extending_it(store: Store) -> None:
    with store.write() as conn:
        add(conn, deliver_after=0)
        q.claim_due(conn, "d", lease(NOW), "a")
        assert q.claim_due(conn, "d", lease(NOW - 1), "b") != []  # expiry NOW+L > (NOW-1)+L


def test_a_clock_that_moved_forward_expires_the_claim_early_never_late(store: Store) -> None:
    with store.write() as conn:
        add(conn)
        q.claim_due(conn, "d", lease(NOW), "a")
        assert q.claim_due(conn, "d", lease(NOW + 10 * L), "b") != []


def test_a_far_future_expiry_written_by_a_bad_process_is_a_lapsed_claim(store: Store) -> None:
    with store.write() as conn:
        row = add(conn, token="x")[1].row_id
        conn.execute("UPDATE queue SET claim_expires = ? WHERE id = ?", (2**62, row))
        assert [r.id for r in q.claim_due(conn, "d", lease(), "me")] == [row]


def test_a_claimant_without_an_expiry_is_a_lapsed_claim(store: Store) -> None:
    with store.write() as conn:
        row = add(conn, token="x")[1].row_id
        conn.execute("UPDATE queue SET claim_expires = NULL WHERE id = ?", (row,))
        assert [r.id for r in q.claim_due(conn, "d", lease(), "me")] == [row]


# --- fencing ---------------------------------------------------------------------------------


def test_a_result_for_a_row_taken_over_after_its_lease_lapsed_is_not_applied(store: Store) -> None:
    with store.write() as conn:
        row = add(conn)[1].row_id
        q.claim_due(conn, "d", lease(NOW), "stale")
    with store.write() as conn:
        assert [r.id for r in q.claim_due(conn, "d", lease(NOW + L), "fresh")] == [row]
    with store.write() as conn:
        assert q.complete_row(conn, row, "stale") is False
        assert q.requeue_row(conn, row, "stale", NOW + 99, lease(NOW + L)) == q.Change(False)
        got = q.get_row(conn, row)
        assert got is not None and got.claimed_by == "fresh" and got.deliver_after == NOW
    with store.write() as conn:
        assert q.complete_row(conn, row, "fresh") is True
        assert q.complete_row(conn, row, "fresh") is False  # gone
    assert n_queue(store) == 0


def test_a_lapsed_claim_nobody_took_can_still_be_recorded_by_its_claimant(store: Store) -> None:
    with store.write() as conn:
        row = add(conn)[1].row_id
        q.claim_due(conn, "d", lease(NOW), "a")
    with store.write() as conn:
        assert q.complete_row(conn, row, "a") is True


def test_complete_and_requeue_refuse_a_bad_token(store: Store) -> None:
    with store.write() as conn:
        row = add(conn)[1].row_id
        for bad in ("", None, 5):
            with pytest.raises(ValueError):
                q.complete_row(conn, row, bad)  # type: ignore[arg-type]
        # an unclaimed row has no claimant: no token (not even an empty one) matches
        assert q.complete_row(conn, row, "x") is False


def test_requeue_clears_the_claim_sets_the_time_and_applies_the_cap(store: Store) -> None:
    with store.write() as conn:
        low = add(conn, kind="single", token="t", level="info")[1].row_id
        for _ in range(100):
            add(conn, level="warn")
        res = q.requeue_row(conn, low, "t", NOW + 60, lease())
        assert res.applied and [e.queue_id for e in res.evicted] == [low]  # lowest level, 101st
        assert q.get_row(conn, low) is None


def test_requeue_keeps_the_row_when_it_is_not_the_lowest(store: Store) -> None:
    with store.write() as conn:
        for _ in range(100):
            add(conn, level="info")
        row = add(conn, kind="single", token="t", level="error")[1].row_id
        res = q.requeue_row(conn, row, "t", NOW + 60, lease())
        got = q.get_row(conn, row)
    assert res.applied and len(res.evicted) == 1 and res.evicted[0].queue_id != row
    assert got is not None and (got.claimed_by, got.claim_expires, got.deliver_after) == (
        None, None, NOW + 60,
    )  # fmt: skip


def test_release_sets_claim_expires_to_now_for_the_tokens_rows_only(store: Store) -> None:
    with store.write() as conn:
        mine = [add(conn, kind="single", token="me")[1].row_id for _ in range(2)]
        theirs = add(conn, kind="single", token="them")[1].row_id
    ls = lease(NOW + 3)
    with store.write() as conn:
        rel = q.release_claims(conn, "me", ls)
        assert rel.count == 2 and rel.evicted == ()
        assert [q.get_row(conn, i).claim_expires for i in mine] == [NOW + 3] * 2  # type: ignore[union-attr]
        assert q.get_row(conn, theirs).claim_expires == NOW + L  # type: ignore[union-attr]
    with store.write() as conn:
        assert len(q.claim_due(conn, "d", ls, "next")) == 2  # due again at once


def test_release_never_lengthens_and_handles_a_missing_expiry(store: Store) -> None:
    with store.write() as conn:
        a = add(conn, token="me")[1].row_id
        b = add(conn, token="me")[1].row_id
        conn.execute("UPDATE queue SET claim_expires = ? WHERE id = ?", (NOW - 50, a))
        conn.execute("UPDATE queue SET claim_expires = NULL WHERE id = ?", (b,))
        q.release_claims(conn, "me", lease())
        assert q.get_row(conn, a).claim_expires == NOW - 50  # type: ignore[union-attr]
        assert q.get_row(conn, b).claim_expires == NOW  # type: ignore[union-attr]


def test_release_applies_the_cap_to_the_rows_it_gives_back(store: Store) -> None:
    with store.write() as conn:
        for _ in range(100):
            add(conn, level="warn")
        low = add(conn, kind="single", token="me", level="info")[1].row_id
    with store.write() as conn:
        rel = q.release_claims(conn, "me", lease())
    assert [e.queue_id for e in rel.evicted] == [low]
    assert n_queue(store) == 100


def test_release_and_requeue_evict_only_the_over_cap_destination(store: Store) -> None:
    with store.write() as conn:
        for _ in range(100):
            add(conn, "full", level="warn")
        for _ in range(99):
            add(conn, "ok", level="warn")
        low_full = add(conn, "full", kind="single", token="me", level="info")[1].row_id
        add(conn, "ok", kind="single", token="me", level="info")
        rel = q.release_claims(conn, "me", lease())
        assert [e.queue_id for e in rel.evicted] == [low_full]
        assert all(e.destination == "full" for e in rel.evicted)
        assert q.unclaimed_count(conn, "ok", lease()) == 100  # untouched: at the cap, not over it
    with store.write() as conn:
        for _ in range(100):
            add(conn, "full2", level="warn")
        add(conn, "ok2", level="warn")
        a = add(conn, "full2", kind="single", token="t", level="info")[1].row_id
        b = add(conn, "ok2", kind="single", token="u", level="info")[1].row_id
        assert q.requeue_row(conn, a, "t", NOW, lease()).evicted[0].queue_id == a
        res = q.requeue_row(conn, b, "u", NOW, lease())
        assert res.applied and res.evicted == ()
        assert q.get_row(conn, b) is not None


def test_requeue_refuses_a_delivery_without_a_destination_and_rolls_back(store: Store) -> None:
    with store.write() as conn:
        row = add(conn, kind="single", token="t")[1].row_id
    with pytest.raises(ValueError):
        with store.write() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("UPDATE deliveries SET destination = NULL")
            q.requeue_row(conn, row, "t", NOW + 60, lease())
    got = q.get_row(store.conn, row)
    assert got is not None and got.claimed_by == "t"


@pytest.mark.parametrize("bad", [True, 1.5, "1", 2**63, -(2**63) - 1])
def test_row_and_delivery_ids_and_limit_are_validated(store: Store, bad: object) -> None:
    with store.write() as conn:
        for call in (
            lambda: q.get_row(conn, bad),  # type: ignore[arg-type]
            lambda: q.complete_row(conn, bad, "t"),  # type: ignore[arg-type]
            lambda: q.requeue_row(conn, bad, "t", NOW, lease()),  # type: ignore[arg-type]
            lambda: q.claim_due(conn, "d", lease(), "t", limit=bad),  # type: ignore[arg-type]
        ):
            with pytest.raises(ValueError):
                call()
        with pytest.raises(ValueError):
            q.enqueue(
                conn, delivery_id=bad, kind="held", message="m", level="info",  # type: ignore[arg-type]
                source="s", deliver_after=NOW, created_at=NOW, lease=lease(),
            )  # fmt: skip


def test_an_unencodable_token_is_a_value_error(store: Store) -> None:
    with store.write() as conn:
        with pytest.raises(ValueError):
            q.complete_row(conn, 1, "\ud800")


def test_eviction_clears_deliver_after_of_the_dropped_delivery(store: Store) -> None:
    with store.write() as conn:
        for _ in range(100):
            add(conn, level="warn")
        did = delivery(conn, level="info")
        conn.execute("UPDATE deliveries SET deliver_after = ? WHERE id = ?", (NOW + 5, did))
        q.enqueue(
            conn, delivery_id=did, kind="held", message="m", level="info", source="s",
            deliver_after=NOW + 5, created_at=NOW, lease=lease(),
        )  # fmt: skip
    assert delivery_state(store, did) == ("dropped", "overflow", "overflow")
    assert store.read("SELECT deliver_after FROM deliveries WHERE id = ?", (did,)) == [(None,)]


# --- quiet windows ---------------------------------------------------------------------------


def _window() -> QuietWindow:
    zone = ZoneInfo("UTC")
    return QuietWindow("w", "UTC", zone, (0, 1, 2, 3, 4, 5, 6), 22 * 60, 6 * 60)


def test_quiet_deliver_after_is_the_next_window_end(store: Store) -> None:
    ts = 1_700_000_000  # 2023-11-14T22:13:20Z, inside 22:00-06:00
    end = q.quiet_deliver_after(_window(), ts)
    assert end == 1_700_000_000 - (22 * 3600 + 13 * 60 + 20) + 86400 + 6 * 3600
    assert end % 60 == 0 and end > ts


def test_quiet_deliver_after_with_no_end_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(q, "window_end", lambda w, t: None)
    with pytest.raises(RuntimeError):
        q.quiet_deliver_after(_window(), NOW)


# --- real processes --------------------------------------------------------------------------

CHILD_CLAIM = """
import json, pathlib, sys, time
from notice_route.gateway import Gateway
from notice_route.queue import Lease, claim_due
from notice_route.store import open_store
home, go, token, now, limit = sys.argv[1:6]
while not pathlib.Path(go).exists():
    time.sleep(0.005)
got = []
with open_store(Gateway(passwd_home=lambda: home)) as store:
    while True:
        with store.write() as conn:
            rows = claim_due(conn, "d", Lease(int(now), 20), token, limit=int(limit))
        if not rows:
            break
        got += [r.id for r in rows]
print(json.dumps(got))
"""

CHILD_CLAIM_AND_HANG = """
import pathlib, sys, time
from notice_route.gateway import Gateway
from notice_route.queue import Lease, claim_due
from notice_route.store import open_store
home, marker, token, now = sys.argv[1:5]
with open_store(Gateway(passwd_home=lambda: home)) as store:
    with store.write() as conn:
        n = len(claim_due(conn, "d", Lease(int(now), 20), token))
pathlib.Path(marker).write_text(str(n))
time.sleep(120)
"""

CHILD_ENQUEUE = """
import pathlib, sys, time
from notice_route.gateway import Gateway
from notice_route.queue import Lease, enqueue, insert_delivery
from notice_route.store import open_store
home, go, n, level = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
while not pathlib.Path(go).exists():
    time.sleep(0.005)
with open_store(Gateway(passwd_home=lambda: home)) as store:
    for _ in range(n):
        with store.write() as conn:
            did = insert_delivery(conn, ts=1, source="s", level=level, outcome="deferred",
                                  destination="d")
            enqueue(conn, delivery_id=did, kind="held", message="m", level=level, source="s",
                    deliver_after=1, created_at=1, lease=Lease(1000, 20))
"""


def _env(home: Path) -> dict[str, str]:
    return {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": "/usr/bin:/bin"}


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen[str]]]:
    started: list[subprocess.Popen[str]] = []
    yield started
    for p in started:  # exactly the processes this test started, by their handles
        if p.poll() is None:
            os.kill(p.pid, signal.SIGKILL)
        p.wait()
        for stream in (p.stdout, p.stderr):
            if stream:
                stream.close()


def _spawn(
    children: list[subprocess.Popen[str]], home: Path, code: str, *args: str
) -> subprocess.Popen[str]:
    p = subprocess.Popen(
        [sys.executable, "-I", "-c", code, str(home), *args],
        env=_env(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    children.append(p)
    return p


def _wait_for(path: Path, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()


def test_eight_senders_and_three_flushers_claim_every_row_exactly_once(
    gw: Gateway, home: Path, tmp_path: Path, children: list[subprocess.Popen[str]]
) -> None:
    total = 66
    with open_store(gw) as store:
        with store.write() as conn:
            ids = [add(conn, kind="single", deliver_after=0)[1].row_id for _ in range(total)]
        go = tmp_path / "go"
        procs = [
            _spawn(children, home, CHILD_CLAIM, str(go), f"p{i}", str(NOW), "1" if i < 8 else "7")
            for i in range(11)
        ]
        time.sleep(0.4)  # every child is polling for the start file
        go.write_text("go")
        claimed: list[int] = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            assert p.returncode == 0, err
            claimed += json.loads(out)
        assert sorted(claimed) == ids  # every row once: none lost, none twice
        owners = store.read("SELECT claimed_by FROM queue")
        assert all(o[0] is not None for o in owners)


def test_two_claimers_racing_for_one_row_exactly_one_wins(
    gw: Gateway, home: Path, tmp_path: Path, children: list[subprocess.Popen[str]]
) -> None:
    with open_store(gw) as store:
        with store.write() as conn:
            row = add(conn, kind="single", deliver_after=0)[1].row_id
        go = tmp_path / "go"
        procs = [_spawn(children, home, CHILD_CLAIM, str(go), f"p{i}", str(NOW), "1") for i in "ab"]
        time.sleep(0.3)
        go.write_text("go")
        won = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
        assert sorted(sum(won, [])) == [row] and sorted(map(len, won)) == [0, 1]


def test_a_killed_claimer_lapses_and_the_row_is_resent_once_and_the_old_claim_is_fenced(
    gw: Gateway, home: Path, tmp_path: Path, children: list[subprocess.Popen[str]]
) -> None:
    with open_store(gw) as store:
        with store.write() as conn:
            row = add(conn, kind="single", deliver_after=0)[1].row_id
        marker = tmp_path / "marker"
        child = _spawn(children, home, CHILD_CLAIM_AND_HANG, str(marker), "doomed", str(NOW))
        _wait_for(marker)
        assert marker.read_text() == "1"
        os.kill(child.pid, signal.SIGKILL)  # between the call and the record
        child.wait(timeout=10)
        with store.write() as conn:
            assert q.claim_due(conn, "d", lease(NOW + L - 1), "early") == []  # lease not over
            assert q.get_row(conn, row) is not None  # still there: resent after the lease lapses
            again = q.claim_due(conn, "d", lease(NOW + L), "rescuer")
            assert [r.id for r in again] == [row]
            assert q.claim_due(conn, "d", lease(NOW + L), "third") == []  # exactly one resend
        with store.write() as conn:
            assert q.complete_row(conn, row, "doomed") is False
            assert q.complete_row(conn, row, "rescuer") is True


def test_concurrent_enqueuers_at_the_cap_lose_no_row_and_keep_exactly_100(
    gw: Gateway, home: Path, tmp_path: Path, children: list[subprocess.Popen[str]]
) -> None:
    go = tmp_path / "go"
    plan = [("info", 15)] * 4 + [("error", 15)] * 4  # 60 info + 60 error
    procs = [_spawn(children, home, CHILD_ENQUEUE, str(go), str(n), lvl) for lvl, n in plan]
    time.sleep(0.4)
    go.write_text("go")
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
    with open_store(gw) as store:
        assert n_queue(store) == 100
        dropped = store.read("SELECT COUNT(*) FROM deliveries WHERE outcome = 'dropped'")[0][0]
        assert dropped == 20  # 120 rows, 100 kept; none lost, none double counted
        survivors = store.read(
            "SELECT COUNT(*) FROM queue q JOIN deliveries d ON d.id = q.delivery_id"
            " WHERE d.outcome = 'deferred'"
        )
        assert survivors == [(100,)]
        # the lowest level went first: every drop is an info row (60 infos, 20 drops)
        assert store.read("SELECT COUNT(*) FROM queue WHERE level = 'error'") == [(60,)]
        assert store.read(
            "SELECT COUNT(*) FROM deliveries WHERE outcome = 'dropped' AND level = 'info'"
        ) == [(20,)]


def test_a_constraint_error_in_a_write_leaves_no_partial_queue_state(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with store.write() as conn:
            add(conn)
            conn.execute("INSERT INTO queue (delivery_id) VALUES (NULL)")  # NOT NULL
    assert n_queue(store) == 0
    assert store.read("SELECT COUNT(*) FROM deliveries") == [(0,)]
