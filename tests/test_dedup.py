"""Dedup: the redelivery and repeat checks and the lifetime of their records (#17)."""

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

import pytest

from notice_route import dedup
from notice_route import queue as q
from notice_route.dryrun import dedupe_digest
from notice_route.gateway import FakeClock, Gateway
from notice_route.store import Store, open_store

NOW = 1_700_000_000
W = 60  # window length used by most tests
RET = 1000  # event id retention used by most tests
D1 = dedupe_digest("s", "h", "info", "t", "m", "")
D2 = dedupe_digest("s", "h", "warn", "t", "m", "")


@pytest.fixture
def gw(home: Path) -> Gateway:
    return Gateway(passwd_home=lambda: str(home), clock=FakeClock())


@pytest.fixture
def store(gw: Gateway) -> Iterator[Store]:
    with open_store(gw) as s:
        yield s


def delivery(conn: sqlite3.Connection, destination: str = "d", outcome: str = "deferred") -> int:
    return q.insert_delivery(
        conn, ts=NOW, source="s", level="info", outcome=outcome, destination=destination
    )


def repeat(
    conn: sqlite3.Connection,
    digest: str = D1,
    now: int = NOW,
    destination: str = "d",
    length: int = W,
) -> dedup.Repeat:
    return dedup.check_repeat(
        conn, destination=destination, digest=digest, now=now, window_length=length
    )


def opened(
    conn: sqlite3.Connection,
    digest: str = D1,
    now: int = NOW,
    destination: str = "d",
    length: int = W,
) -> int:
    """Open a window and bind it to a fresh delivery; returns the delivery id."""
    res = repeat(conn, digest, now, destination, length)
    assert res.opened
    did = delivery(conn, destination)
    assert dedup.bind_window(conn, destination=destination, digest=digest, delivery_id=did)
    return did


def seen(
    conn: sqlite3.Connection,
    event_id: object = 17,
    *,
    digest: str = D1,
    now: int = NOW,
    destination: str = "d",
    source: str = "s",
    hook: str | None = "h",
    retention: int = RET,
) -> bool:
    return dedup.is_redelivered(
        conn,
        destination=destination,
        source=source,
        hook=hook,
        event_id=event_id,
        digest=digest,
        now=now,
        retention=retention,
    )


def record(
    conn: sqlite3.Connection,
    event_id: object = 17,
    *,
    digest: str = D1,
    now: int = NOW,
    destination: str = "d",
    source: str = "s",
    hook: str | None = "h",
    retention: int = RET,
    delivery_id: int | None = None,
    cap: int = 100_000,
) -> bool:
    return dedup.record_event_id(
        conn,
        destination=destination,
        source=source,
        hook=hook,
        event_id=event_id,
        digest=digest,
        now=now,
        retention=retention,
        delivery_id=delivery_id,
        cap=cap,
    )


def count(store: Store, table: str) -> int:
    return int(store.read(f"SELECT COUNT(*) FROM {table}")[0][0])  # type: ignore[call-overload]


def delivery_count(store: Store, did: int) -> int:
    return int(store.read("SELECT count FROM deliveries WHERE id = ?", (did,))[0][0])  # type: ignore[call-overload]


# --- the repeat digest is the one of dryrun --------------------------------------------------


def test_digest_is_the_dry_run_digest_and_has_what_the_spec_lists() -> None:
    base: tuple[str, ...] = ("s", "h", "info", "t", "m", "l")
    assert dedupe_digest(*base) == dedupe_digest(*base)
    for i in range(6):  # each of the six parts matters
        changed: list[str] = list(base)
        changed[i] += "x"
        assert dedupe_digest(*changed) != dedupe_digest(*base), i  # type: ignore[arg-type]
    # level is part of the key: a later warn does not suppress an earlier error
    assert dedupe_digest("s", "h", "warn", "t", "m", "") != dedupe_digest(
        "s", "h", "error", "t", "m", ""
    )
    # NUL joins the parts, so a shifted boundary differs
    assert dedupe_digest("s", "h", "info", "ab", "c", "") != dedupe_digest(
        "s", "h", "info", "a", "bc", ""
    )
    # a channel notice has an empty hook
    assert dedupe_digest("s", None, "info", "t", "m", "") == dedupe_digest(
        "s", "", "info", "t", "m", ""
    )


def test_a_later_warn_does_not_suppress_an_earlier_error(store: Store) -> None:
    err = dedupe_digest("s", "h", "error", "t", "m", "")
    warn = dedupe_digest("s", "h", "warn", "t", "m", "")
    with store.write() as conn:
        opened(conn, err)
        assert repeat(conn, warn).opened


# --- the repeat check ------------------------------------------------------------------------


def test_first_opens_and_second_in_window_is_suppressed_and_counted_on_the_opener(
    store: Store,
) -> None:
    with store.write() as conn:
        did = opened(conn)
        res = repeat(conn, now=NOW + 5)
        assert res == dedup.Repeat(opened=False, opener=did)
        res = repeat(conn, now=NOW + 6)
        assert not res.opened
    assert delivery_count(store, did) == 3  # 1 + two suppressed repeats


@pytest.mark.parametrize(
    ("offset", "suppressed"),
    [(0, True), (1, True), (W - 1, True), (W, False), (W + 1, False)],
)
def test_window_is_half_open_start_inclusive_end_exclusive(
    store: Store, offset: int, suppressed: bool
) -> None:
    with store.write() as conn:
        opened(conn)
        assert (not repeat(conn, now=NOW + offset).opened) is suppressed


def test_window_is_fixed_not_sliding_a_repeater_at_40s_does_not_extend_it(store: Store) -> None:
    with store.write() as conn:
        opened(conn)
        assert not repeat(conn, now=NOW + 40).opened  # suppressed, does not move the window
        assert not repeat(conn, now=NOW + 59).opened
        res = repeat(conn, now=NOW + 60)  # a sliding window would still be open (40 + 60)
        assert res.opened
    rows = store.read("SELECT window_start, window_length FROM dedup")
    assert rows == [(NOW + 60, W)]


def test_window_length_is_stored_at_opening_and_never_changed(store: Store) -> None:
    with store.write() as conn:
        opened(conn, length=10)
        # a rule with a longer window meeting the same key does not change the stored length
        assert not repeat(conn, now=NOW + 9, length=3600).opened
        assert repeat(conn, now=NOW + 10, length=3600).opened  # the opener's 10 s is over
    assert store.read("SELECT window_length FROM dedup") == [(3600,)]


def test_a_shorter_rule_window_does_not_end_an_open_window_early(store: Store) -> None:
    with store.write() as conn:
        opened(conn, length=600)
        assert not repeat(conn, now=NOW + 100, length=1).opened


def test_destinations_have_separate_windows(store: Store) -> None:
    with store.write() as conn:
        opened(conn, destination="a")
        assert repeat(conn, destination="b").opened
        assert not repeat(conn, destination="a").opened


def test_unbound_window_suppresses_without_an_opener_and_counts_nothing(store: Store) -> None:
    with store.write() as conn:
        assert repeat(conn).opened  # not yet bound to a delivery
        res = repeat(conn, now=NOW + 1)
        assert res == dedup.Repeat(opened=False, opener=None)


def test_bind_window_binds_only_an_unbound_window_of_that_key(store: Store) -> None:
    with store.write() as conn:
        assert repeat(conn).opened
        did = delivery(conn)
        assert not dedup.bind_window(conn, destination="d", digest=D2, delivery_id=did)
        assert not dedup.bind_window(conn, destination="x", digest=D1, delivery_id=did)
        assert dedup.bind_window(conn, destination="d", digest=D1, delivery_id=did)
        other = delivery(conn)
        assert not dedup.bind_window(conn, destination="d", digest=D1, delivery_id=other)
    assert store.read("SELECT delivery_id FROM dedup") == [(did,)]


def test_clock_stepped_back_before_the_window_start_does_not_suppress(store: Store) -> None:
    # A window that would last longer than its length from now is not trusted (the narrow
    # reading: duplicate, never a lost notification); the new window replaces it.
    with store.write() as conn:
        opened(conn, now=NOW + 10_000)
        assert repeat(conn, now=NOW).opened
    assert store.read("SELECT window_start FROM dedup") == [(NOW,)]


def test_clock_stepped_forward_ends_the_window_once_and_a_later_step_back_is_untrusted(
    store: Store,
) -> None:
    with store.write() as conn:
        opened(conn)
        assert repeat(conn, now=NOW + 7 * 86400).opened  # a week later: fresh window
        assert not repeat(conn, now=NOW + 7 * 86400 + 1).opened


def test_replaced_window_is_not_released_by_the_old_delivery(store: Store) -> None:
    with store.write() as conn:
        old = opened(conn)
        new = opened(conn, now=NOW + W)
        assert dedup.release(conn, old) == 0  # the old delivery's end leaves the new window
        assert store.read("SELECT delivery_id FROM dedup") == [(new,)]
        assert dedup.release(conn, new) == 1


@pytest.mark.parametrize("bad", [True, 1.5, "1", None, -1, 0, 1 << 41])
def test_check_repeat_rejects_a_bad_window_length(store: Store, bad: object) -> None:
    with store.write() as conn, pytest.raises(ValueError):
        dedup.check_repeat(conn, destination="d", digest=D1, now=NOW, window_length=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [True, 1.5, "1", None, 1 << 41, -(1 << 41)])
def test_check_repeat_rejects_a_bad_instant(store: Store, bad: object) -> None:
    with store.write() as conn, pytest.raises(ValueError):
        dedup.check_repeat(conn, destination="d", digest=D1, now=bad, window_length=W)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad", ["", "abc", "A" * 64, "g" * 64, D1 + "0", D1[:-1], None, 5, D1.upper()]
)
def test_digest_must_be_64_lowercase_hex(store: Store, bad: object) -> None:
    with store.write() as conn:
        with pytest.raises(ValueError):
            dedup.check_repeat(conn, destination="d", digest=bad, now=NOW, window_length=W)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            seen(conn, 1, digest=bad)  # type: ignore[arg-type]


def test_count_does_not_overflow_past_int64(store: Store) -> None:
    top = (1 << 63) - 1
    with store.write() as conn:
        did = opened(conn)
        conn.execute("UPDATE deliveries SET count = ? WHERE id = ?", (top, did))
        assert not repeat(conn, now=NOW + 1).opened
    assert delivery_count(store, did) == top


@pytest.mark.parametrize(
    "dest",
    ["x" * 100_000, "d'; DROP TABLE dedup; --", "dé\u0000✓\U0001f600", "", "%_"],
)
def test_hostile_destination_strings_are_data(store: Store, dest: str) -> None:
    with store.write() as conn:
        assert repeat(conn, destination=dest).opened
        assert not repeat(conn, destination=dest).opened
        assert repeat(conn, destination="d").opened
    assert count(store, "dedup") == 2


def test_lone_surrogate_destination_is_a_value_error_not_a_leak(store: Store) -> None:
    with store.write() as conn, pytest.raises(ValueError):
        repeat(conn, destination="\ud800")


def test_active_window_reads_without_writing(store: Store) -> None:
    with store.write() as conn:
        did = opened(conn)
        win = dedup.active_window(conn, destination="d", digest=D1, now=NOW + 3)
        assert win == dedup.Window(start=NOW, length=W, delivery_id=did)
        assert dedup.active_window(conn, destination="d", digest=D1, now=NOW + W) is None
        assert dedup.active_window(conn, destination="d", digest=D1, now=NOW - 1) is None
        assert dedup.active_window(conn, destination="d", digest=D2, now=NOW) is None
    assert delivery_count(store, did) == 1


def test_active_window_works_on_a_read_only_connection(gw: Gateway, store: Store) -> None:
    from notice_route.store import open_readonly

    with store.write() as conn:
        opened(conn)
    ro = open_readonly(gw)
    assert ro is not None
    with ro:
        found = ro.read("SELECT COUNT(*) FROM dedup")
        assert found == [(1,)]


# --- the redelivery check --------------------------------------------------------------------


def test_redelivery_needs_the_same_id_and_the_same_digest(store: Store) -> None:
    with store.write() as conn:
        assert not seen(conn)
        assert record(conn)
        assert seen(conn)
        assert not seen(conn, digest=D2)  # a different digest: a forged id does not mute
        assert not seen(conn, 18)


def test_integer_17_and_string_17_are_different_ids(store: Store) -> None:
    with store.write() as conn:
        assert record(conn, 17)
        assert not seen(conn, "17")
        assert record(conn, "17")
        assert seen(conn, "17") and seen(conn, 17)
    assert sorted(str(r[0]) for r in store.read("SELECT event_id FROM event_ids")) == ['"17"', "17"]


def test_event_id_text_is_the_json_text_of_a_plain_value() -> None:
    assert dedup.event_id_text(17) == "17"
    assert dedup.event_id_text(-3) == "-3"
    assert dedup.event_id_text("17") == '"17"'
    assert dedup.event_id_text("a.b/c#d-e_f") == '"a.b/c#d-e_f"'
    assert dedup.event_id_text(1 << 70) == str(1 << 70)


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        None,
        1.0,
        1.5,
        float("nan"),
        "",
        " 17",
        "a b",
        'a"b',
        "a\nb",
        "-lead",
        "é",
        "x" * 257,
        ["1"],
        {"a": 1},
        b"17",
        pytest.param(10**5000, id="huge-int"),  # past Python integer-to-text limit
    ],
)
def test_a_non_plain_event_id_is_ignored_and_never_recorded(store: Store, value: object) -> None:
    assert dedup.event_id_text(value) is None
    with store.write() as conn:
        assert not record(conn, value)
        assert not seen(conn, value)
    assert count(store, "event_ids") == 0


def test_a_256_character_plain_id_is_accepted(store: Store) -> None:
    with store.write() as conn:
        assert record(conn, "a" * 256)
        assert seen(conn, "a" * 256)
        assert not seen(conn, "a" * 255)


def test_same_id_for_another_destination_source_or_hook_is_not_a_redelivery(store: Store) -> None:
    with store.write() as conn:
        assert record(conn)
        assert not seen(conn, destination="other")
        assert not seen(conn, source="t")
        assert not seen(conn, hook="g")
        assert not seen(conn, hook=None)
        assert seen(conn)


def test_an_empty_hook_and_none_are_the_same_channel_notice(store: Store) -> None:
    with store.write() as conn:
        assert record(conn, hook=None)
        assert seen(conn, hook="")
        assert seen(conn, hook=None)


def test_the_router_own_alerts_are_not_checked_and_not_recorded(store: Store) -> None:
    with store.write() as conn:
        assert not record(conn, source="notice-route")
        assert not seen(conn, source="notice-route")
        assert record(conn, source="notice-route2")
        assert seen(conn, source="notice-route2")
    assert count(store, "event_ids") == 1


@pytest.mark.parametrize(
    ("offset", "kept"),
    [(0, True), (RET - 1, True), (RET, True), (RET + 1, False), (RET + 2, False)],
)
def test_event_id_is_kept_for_the_retention_boundary(store: Store, offset: int, kept: bool) -> None:
    # "older than retention" is deleted by prune, so exactly retention old is still kept
    with store.write() as conn:
        assert record(conn)
        assert seen(conn, now=NOW + offset) is kept


def test_event_id_recorded_in_the_future_after_a_step_back_is_not_trusted(store: Store) -> None:
    with store.write() as conn:
        assert record(conn, now=NOW + 5000)
        assert not seen(conn, now=NOW)
        assert not seen(conn, now=NOW + 4999)
        assert seen(conn, now=NOW + 5000)


def test_recording_does_not_extend_a_live_record_and_reports_it_was_not_written(
    store: Store,
) -> None:
    with store.write() as conn:
        did = delivery(conn)
        assert record(conn, now=NOW, delivery_id=did)
        later = delivery(conn)
        # a live row means the caller should have suppressed: no exception, no change
        assert not record(conn, now=NOW + 500, delivery_id=later)
    assert store.read("SELECT seen_at, delivery_id FROM event_ids") == [(NOW, did)]


def test_an_expired_row_is_replaced_and_its_lifetime_restarts(store: Store) -> None:
    with store.write() as conn:
        assert record(conn, now=NOW)
        did = delivery(conn)
        assert record(conn, now=NOW + RET + 1, delivery_id=did)
        assert seen(conn, now=NOW + RET + 1)
    assert store.read("SELECT seen_at, delivery_id FROM event_ids") == [(NOW + RET + 1, did)]


def test_retention_is_per_call_so_a_shorter_config_applies_at_once(store: Store) -> None:
    with store.write() as conn:
        assert record(conn, retention=1000)
        assert seen(conn, now=NOW + 500, retention=1000)
        assert not seen(conn, now=NOW + 500, retention=100)


@pytest.mark.parametrize("bad", [True, 0, -1, 1.5, "1", None, 1 << 41])
def test_a_bad_retention_is_a_value_error(store: Store, bad: object) -> None:
    with store.write() as conn:
        with pytest.raises(ValueError):
            seen(conn, retention=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            record(conn, retention=bad)  # type: ignore[arg-type]


def test_hostile_strings_in_source_hook_destination_are_data(store: Store) -> None:
    nasty = "x'); DELETE FROM event_ids; --"
    with store.write() as conn:
        assert record(conn, destination=nasty, source=nasty, hook=nasty)
        assert seen(conn, destination=nasty, source=nasty, hook=nasty)
        assert not seen(conn, destination="d", source=nasty, hook=nasty)
        assert record(conn, destination="é\u0000", source="🙂" * 5000, hook="h")
    assert count(store, "event_ids") == 2


def test_event_id_row_cap_trims_the_oldest_and_keeps_the_one_just_recorded(
    store: Store,
) -> None:
    with store.write() as conn:
        for i in range(5):
            assert record(conn, i, cap=3)
    ids = sorted(str(r[0]) for r in store.read("SELECT event_id FROM event_ids"))
    assert ids == ["2", "3", "4"]  # the oldest two went, the newest stayed


def test_cap_never_trims_the_row_just_recorded_even_with_cap_zero(store: Store) -> None:
    with store.write() as conn:
        assert record(conn, 1, cap=0)
        assert record(conn, 2, cap=0)
    assert [r[0] for r in store.read("SELECT event_id FROM event_ids")] == ["2"]


def test_replacing_an_expired_row_keeps_the_row_through_the_cap(store: Store) -> None:
    with store.write() as conn:
        record(conn, 1, cap=2)
        record(conn, 2, cap=2)
        assert record(conn, 1, now=NOW + RET + 1, cap=2)  # replaces the oldest, in place
        assert seen(conn, 1, now=NOW + RET + 1)


# --- release and lifetime --------------------------------------------------------------------


def test_release_deletes_both_records_of_a_delivery_and_only_that_delivery(store: Store) -> None:
    with store.write() as conn:
        a = opened(conn, D1)
        b = opened(conn, D2)
        assert record(conn, 1, delivery_id=a)
        assert record(conn, 2, delivery_id=b)
        assert dedup.release(conn, a) == 2
        assert dedup.release(conn, a) == 0  # again: nothing, no error
        assert repeat(conn, D1, now=NOW + 1).opened  # judged afresh
        assert not repeat(conn, D2, now=NOW + 1).opened
        assert not seen(conn, 1)
        assert seen(conn, 2)


@pytest.mark.parametrize("bad", [True, 1.5, "1", None, 1 << 63, -(1 << 63) - 1])
def test_release_rejects_a_bad_delivery_id(store: Store, bad: object) -> None:
    with store.write() as conn, pytest.raises(ValueError):
        dedup.release(conn, bad)  # type: ignore[arg-type]


def test_records_stay_when_nothing_releases_them_as_after_sent_or_unknown(store: Store) -> None:
    with store.write() as conn:
        did = opened(conn)
        record(conn, delivery_id=did)
        conn.execute("UPDATE deliveries SET outcome = 'sent' WHERE id = ?", (did,))
    with store.write() as conn:
        assert not repeat(conn, now=NOW + 1).opened
        assert seen(conn, now=NOW + 1)
        conn.execute("UPDATE deliveries SET outcome = 'unknown' WHERE id = ?", (did,))
    with store.write() as conn:
        assert not repeat(conn, now=NOW + 2).opened
        assert seen(conn, now=NOW + 2)


@pytest.mark.parametrize("end", ["failed", "dropped", "expired", "disabled"])
def test_release_after_an_unsuccessful_end_lets_the_next_one_through(
    store: Store, end: str
) -> None:
    with store.write() as conn:
        did = opened(conn)
        record(conn, delivery_id=did)
        conn.execute("UPDATE deliveries SET outcome = ? WHERE id = ?", (end, did))
        assert dedup.release(conn, did) == 2
        assert repeat(conn, now=NOW + 1).opened
        assert not seen(conn, now=NOW + 1)


def test_record_with_an_unknown_delivery_id_is_allowed_no_foreign_key(store: Store) -> None:
    with store.write() as conn:
        assert record(conn, delivery_id=999_999)
        assert dedup.release(conn, 999_999) == 1


# --- eviction of a queued delivery -----------------------------------------------------------


def test_eviction_of_a_delivery_deletes_its_records_in_the_same_transaction(
    store: Store,
) -> None:
    ls = q.Lease(NOW, 20)
    with store.write() as conn:
        digests = [dedupe_digest("s", "h", "info", f"t{i}", "m", "") for i in range(3)]
        dids: list[int] = []
        res = q.Enqueued(0)
        for i, dg in enumerate(digests):
            dids.append(opened(conn, dg))
            assert record(conn, i, digest=dg, delivery_id=dids[-1])
        # three held rows with a cap of 2: the oldest is evicted
        for did in dids:
            res = q.enqueue(
                conn,
                delivery_id=did,
                kind="held",
                message="m",
                level="info",
                source="s",
                deliver_after=NOW,
                created_at=NOW,
                lease=ls,
                cap=2,
            )
        assert [e.delivery_id for e in res.evicted] == [dids[0]]
        assert store.read("SELECT COUNT(*) FROM dedup WHERE delivery_id = ?", (dids[0],)) == [(0,)]
        assert not seen(conn, 0, digest=digests[0])
        assert seen(conn, 1, digest=digests[1]) and seen(conn, 2, digest=digests[2])
        assert repeat(conn, digests[0], now=NOW + 1).opened
    assert count(store, "dedup") == 3


def test_eviction_through_requeue_and_release_claims_also_releases(store: Store) -> None:
    ls = q.Lease(NOW, 20)
    with store.write() as conn:
        d1 = opened(conn, D1)
        record(conn, 1, digest=D1, delivery_id=d1)
        q.enqueue(
            conn,
            delivery_id=d1,
            kind="single",
            message="m",
            level="info",
            source="s",
            deliver_after=NOW,
            created_at=NOW,
            lease=ls,
            claim_token="tok1",
        )
        d2 = delivery(conn)
        q.enqueue(
            conn,
            delivery_id=d2,
            kind="held",
            message="m",
            level="error",
            source="s",
            deliver_after=NOW,
            created_at=NOW,
            lease=ls,
            cap=1,
        )
        # the claimed info row goes back to the queue: now 2 unclaimed, the info one is evicted
        res = q.release_claims(conn, "tok1", ls, cap=1)
        assert [e.delivery_id for e in res.evicted] == [d1]
        assert not seen(conn, 1)
        assert repeat(conn, D1, now=NOW + 1).opened


# --- prune -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("offset", "gone"), [(W - 1, False), (W, True), (W + 1, True)])
def test_prune_windows_deletes_ended_windows(store: Store, offset: int, gone: bool) -> None:
    with store.write() as conn:
        opened(conn)
    with store.write() as conn:
        n = dedup.prune_windows(conn, now=NOW + offset)
    assert n == (1 if gone else 0)
    assert count(store, "dedup") == (0 if gone else 1)


def test_prune_windows_keeps_a_window_that_starts_in_the_future(store: Store) -> None:
    with store.write() as conn:
        opened(conn, now=NOW + 5000)
        assert dedup.prune_windows(conn, now=NOW) == 0


@pytest.mark.parametrize(("offset", "gone"), [(RET - 1, False), (RET, False), (RET + 1, True)])
def test_prune_event_ids_deletes_those_older_than_retention(
    store: Store, offset: int, gone: bool
) -> None:
    with store.write() as conn:
        record(conn)
    with store.write() as conn:
        n = dedup.prune_event_ids(conn, now=NOW + offset, retention=RET)
    assert n == (1 if gone else 0)


def test_prune_deletes_at_most_one_batch_per_call(store: Store) -> None:
    with store.write() as conn:
        for i in range(7):
            record(conn, i)
            repeat(conn, dedupe_digest("s", "h", "info", str(i), "", ""))
    with store.write() as conn:
        assert dedup.prune_event_ids(conn, now=NOW + RET + 1, retention=RET, batch=3) == 3
        assert dedup.prune_windows(conn, now=NOW + W, batch=2) == 2
    with store.write() as conn:
        assert dedup.prune_event_ids(conn, now=NOW + RET + 1, retention=RET, batch=3) == 3
        assert dedup.prune_event_ids(conn, now=NOW + RET + 1, retention=RET, batch=3) == 1
        assert dedup.prune_windows(conn, now=NOW + W, batch=100) == 5


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, None])
def test_prune_batch_must_be_a_positive_int(store: Store, bad: object) -> None:
    with store.write() as conn:
        with pytest.raises(ValueError):
            dedup.prune_windows(conn, now=NOW, batch=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            dedup.prune_event_ids(conn, now=NOW, retention=RET, batch=bad)  # type: ignore[arg-type]


# --- concurrent writers, real processes ------------------------------------------------------

CHILD_REPEAT = """
import json, sys, time
from pathlib import Path
from notice_route import dedup, queue as q
from notice_route.gateway import FakeClock, Gateway
from notice_route.store import open_store
home, go, digest, now = sys.argv[1:5]
gw = Gateway(passwd_home=lambda: home, clock=FakeClock())
deadline = time.monotonic() + 30
while not Path(go).exists() and time.monotonic() < deadline:
    time.sleep(0.005)
with open_store(gw) as store:
    with store.write() as conn:
        res = dedup.check_repeat(
            conn, destination="d", digest=digest, now=int(now), window_length=60
        )
        if res.opened:
            did = q.insert_delivery(
                conn, ts=int(now), source="s", level="info", outcome="deferred", destination="d"
            )
            dedup.bind_window(conn, destination="d", digest=digest, delivery_id=did)
print(json.dumps(res.opened))
"""

CHILD_EVENT = """
import json, sys, time
from pathlib import Path
from notice_route import dedup, queue as q
from notice_route.gateway import FakeClock, Gateway
from notice_route.store import open_store
home, go, digest, now = sys.argv[1:5]
gw = Gateway(passwd_home=lambda: home, clock=FakeClock())
deadline = time.monotonic() + 30
while not Path(go).exists() and time.monotonic() < deadline:
    time.sleep(0.005)
args = dict(destination="d", source="s", hook="h", event_id=17, digest=digest, now=int(now),
            retention=1000)
with open_store(gw) as store:
    with store.write() as conn:
        # no check first: the unique key must make the second writer a no-op, not an error
        wrote = dedup.record_event_id(conn, delivery_id=None, **args)
print(json.dumps(wrote))
"""


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


def _race(
    children: list[subprocess.Popen[str]], home: Path, tmp_path: Path, code: str, n: int
) -> list[object]:
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": "/usr/bin:/bin"}
    go = tmp_path / "go"
    for _ in range(n):
        children.append(
            subprocess.Popen(
                [sys.executable, "-I", "-c", code, str(home), str(go), D1, str(NOW)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    time.sleep(0.5)  # every child is polling for the start file
    go.write_text("go")
    results: list[object] = []
    for p in children:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        results.append(json.loads(out))
    return results


def test_eight_concurrent_repeat_checks_let_exactly_one_through(
    gw: Gateway, home: Path, tmp_path: Path, children: list[subprocess.Popen[str]]
) -> None:
    with open_store(gw):
        pass  # create and migrate first, so the children race on the check, not the migration
    results = _race(children, home, tmp_path, CHILD_REPEAT, 8)
    assert results.count(True) == 1 and results.count(False) == 7
    with open_store(gw) as store:
        assert count(store, "dedup") == 1
        assert count(store, "deliveries") == 1
        assert store.read("SELECT delivery_id FROM dedup")[0][0] is not None


def test_eight_concurrent_event_id_writers_record_once_and_none_raises(
    gw: Gateway, home: Path, tmp_path: Path, children: list[subprocess.Popen[str]]
) -> None:
    with open_store(gw):
        pass
    results = _race(children, home, tmp_path, CHILD_EVENT, 8)
    assert results.count(True) == 1 and results.count(False) == 7
    with open_store(gw) as store:
        assert count(store, "event_ids") == 1
