"""The per-destination decision order and the hourly counters (#18)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from notice_route import decide
from notice_route.decide import DestinationDecision, Notice, decide_and_record, preview
from notice_route.gateway import FakeClock, Gateway
from notice_route.store import Store, StoreError, open_readonly, open_store
from notice_route.validate import Destination
from tests.decide_support import (
    DAY0,
    NEXT_MORNING,
    NIGHT,
    NOON,
    dest,
    gateway,
    make_config,
    notice,
)

ROOT = Path(__file__).resolve().parent.parent
HOUR = 3600
LEASE = 58  # twice ceil(lock_wait 2 + timeout 10 + extra 17), see report.worst_case_s


@pytest.fixture
def gw(home: Path) -> Gateway:
    return gateway(home)


@pytest.fixture
def store(gw: Gateway) -> Iterator[Store]:
    with open_store(gw) as s:
        yield s


def at(gw: Gateway, wall: float) -> None:
    assert isinstance(gw.clock, FakeClock)
    gw.clock.jump_wall(wall - gw.wall())


def go(
    store: Store, gw: Gateway, d: Destination, n: Notice, names: Sequence[str] = ("d",)
) -> list[DestinationDecision]:
    return decide_and_record(store, gw, make_config(d), n, names)


def one(store: Store, gw: Gateway, d: Destination, n: Notice) -> DestinationDecision:
    (result,) = go(store, gw, d, n)
    return result


def q(store: Store, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    return store.read(sql, params)


def count(store: Store, table: str) -> int:
    return int(q(store, f"SELECT COUNT(*) FROM {table}")[0][0])  # type: ignore[call-overload]


# --- one test per decision of the order ------------------------------------------------------


def test_1_disabled_writes_a_disabled_row_and_nothing_else(store: Store, gw: Gateway) -> None:
    r = one(store, gw, dest(enabled=False), notice("error"))
    assert (r.decision, r.outcome, r.reason, r.deliver_after) == (
        "disabled",
        "disabled",
        None,
        None,
    )
    assert q(store, "SELECT outcome, reason, destination FROM deliveries") == [
        ("disabled", None, "d")
    ]
    assert (count(store, "queue"), count(store, "dedup"), count(store, "rate")) == (0, 0, 0)


def test_2_below_floor_is_dropped_with_a_row(store: Store, gw: Gateway) -> None:
    r = one(store, gw, dest(floor="action"), notice("warn"))
    assert (r.decision, r.outcome, r.reason) == ("drop", "dropped", "below_floor")
    assert q(store, "SELECT outcome, reason FROM deliveries") == [("dropped", "below_floor")]
    assert (count(store, "queue"), count(store, "dedup"), count(store, "rate")) == (0, 0, 0)


def test_2_a_level_at_the_floor_is_not_below_it(store: Store, gw: Gateway) -> None:
    assert one(store, gw, dest(floor="warn"), notice("warn")).decision == "send"


def test_3_info_in_quiet_is_dropped(store: Store, gw: Gateway) -> None:
    at(gw, NIGHT)
    r = one(store, gw, dest(quiet="night"), notice("info"))
    assert (r.decision, r.reason) == ("drop", "quiet")
    assert q(store, "SELECT outcome, reason FROM deliveries") == [("dropped", "quiet")]
    assert (count(store, "dedup"), count(store, "event_ids"), count(store, "rate")) == (0, 0, 0)


def test_3_info_outside_quiet_is_sent(store: Store, gw: Gateway) -> None:
    assert one(store, gw, dest(quiet="night"), notice("info")).decision == "send"


def test_4_redelivery_is_suppressed_and_not_counted(store: Store, gw: Gateway) -> None:
    d = dest()
    first = one(store, gw, d, notice("warn", event_id=17))
    assert first.decision == "send"
    at(gw, NOON + 700)  # past the repeat window of 600 s: only the event id can match
    r = one(store, gw, d, notice("warn", event_id=17))
    assert (r.decision, r.outcome, r.reason, r.delivery_id) == (
        "suppress",
        "suppressed",
        "redelivered",
        None,
    )
    assert count(store, "deliveries") == 1  # a suppressed notification writes no row
    assert q(store, "SELECT count FROM rate") == [(1,)]  # and is not counted


def test_4_the_integer_17_and_the_string_17_differ(store: Store, gw: Gateway) -> None:
    d = dest()
    one(store, gw, d, notice("warn", event_id=17))
    at(gw, NOON + 700)
    assert one(store, gw, d, notice("warn", event_id="17")).decision == "send"


def test_5_repeat_is_suppressed_and_counts_on_the_opener(store: Store, gw: Gateway) -> None:
    d = dest()
    first = one(store, gw, d, notice("warn"))
    for _ in range(3):
        r = one(store, gw, d, notice("warn"))
        assert (r.decision, r.reason, r.delivery_id) == ("suppress", "repeat", None)
    assert q(store, "SELECT id, count FROM deliveries") == [(first.delivery_id, 4)]
    assert q(store, "SELECT count FROM rate") == [(1,)]


def test_5_the_window_is_fixed_and_a_later_notification_opens_a_new_one(
    store: Store, gw: Gateway
) -> None:
    d = dest()
    one(store, gw, d, notice("warn", window=60))
    at(gw, NOON + 59)
    assert one(store, gw, d, notice("warn", window=60)).decision == "suppress"
    at(gw, NOON + 60)  # [first, first + window) is half open
    assert one(store, gw, d, notice("warn", window=60)).decision == "send"


def test_6_warn_and_action_in_quiet_are_held_until_the_window_ends(
    store: Store, gw: Gateway
) -> None:
    at(gw, NIGHT)
    d = dest(quiet="night")
    for level in ("warn", "action"):
        r = one(store, gw, d, notice(level))
        assert (r.decision, r.outcome, r.reason, r.deliver_after) == (
            "defer",
            "deferred",
            "quiet",
            NEXT_MORNING,
        )
        assert r.claim_token is None
    rows = q(store, "SELECT kind, deliver_after, claimed_by, level FROM queue ORDER BY id")
    assert rows == [
        ("held", NEXT_MORNING, None, "warn"),
        ("held", NEXT_MORNING, None, "action"),
    ]
    assert q(store, "SELECT outcome, reason, deliver_after FROM deliveries LIMIT 1") == [
        ("deferred", "quiet", NEXT_MORNING)
    ]
    assert count(store, "rate") == 0  # a held row is not a call


def test_6_error_is_delivered_in_quiet_hours(store: Store, gw: Gateway) -> None:
    at(gw, NIGHT)
    r = one(store, gw, dest(quiet="night"), notice("error"))
    assert r.decision == "send" and r.claim_token is not None


def test_7_env_missing_is_a_single_row_due_now(store: Store, gw: Gateway) -> None:
    r = one(store, gw, dest(env=("ROUTE_VAR",)), notice("warn"))
    assert (r.decision, r.reason, r.deliver_after) == ("defer", "env_missing", NOON)
    assert q(store, "SELECT kind, deliver_after, claimed_by FROM queue") == [("single", NOON, None)]
    assert count(store, "rate") == 0


@pytest.mark.parametrize("value", [None, ""])
def test_7_a_missing_or_empty_variable_is_unset(home: Path, value: str | None) -> None:
    env = {"ROUTE_VAR": value} if value is not None else {}
    gw = gateway(home, env=env)
    with open_store(gw) as store:
        r = one(store, gw, dest(env=("ROUTE_VAR",)), notice("warn"))
    assert r.reason == "env_missing"


def test_7_a_set_variable_sends(home: Path) -> None:
    gw = gateway(home, env={"ROUTE_VAR": "x", "OTHER": "y"})
    with open_store(gw) as store:
        assert one(store, gw, dest(env=("ROUTE_VAR", "OTHER")), notice("warn")).decision == "send"


def test_7_one_unset_variable_of_two_is_enough(home: Path) -> None:
    gw = gateway(home, env={"ROUTE_VAR": "x"})
    with open_store(gw) as store:
        r = one(store, gw, dest(env=("ROUTE_VAR", "OTHER")), notice("warn"))
    assert r.reason == "env_missing"


def test_8_cap_defers_to_the_next_utc_hour(store: Store, gw: Gateway) -> None:
    d = dest(max_per_hour=1)
    assert one(store, gw, d, notice("warn", "a")).decision == "send"
    r = one(store, gw, d, notice("warn", "b"))
    assert (r.decision, r.reason, r.deliver_after) == ("defer", "rate_capped", NOON + HOUR)
    assert q(store, "SELECT kind, deliver_after, claimed_by FROM queue ORDER BY id")[1] == (
        "held",
        NOON + HOUR,
        None,
    )
    assert q(store, "SELECT count FROM rate") == [(1,)]  # a deferral is not counted


def test_9_send_now_is_a_claimed_single_row_and_counts(store: Store, gw: Gateway) -> None:
    r = one(store, gw, dest(), notice("warn", title="T"))
    assert (r.decision, r.outcome, r.reason, r.deliver_after) == ("send", "sending", None, None)
    assert r.claim_expires == NOON + LEASE
    assert q(store, "SELECT kind, deliver_after, claimed_by, claim_expires, title FROM queue") == [
        ("single", NOON, r.claim_token, NOON + LEASE, "T")
    ]
    assert q(store, "SELECT outcome, reason, attempts, deliver_after FROM deliveries") == [
        ("sending", None, 0, None)
    ]
    assert q(store, "SELECT hour, class, count FROM rate") == [(NOON - NOON % HOUR, "other", 1)]


def test_9_every_claim_has_a_fresh_token(store: Store, gw: Gateway) -> None:
    d = dest()
    tokens = {one(store, gw, d, notice("warn", f"m{i}")).claim_token for i in range(5)}
    assert len(tokens) == 5 and None not in tokens


# --- first applicable wins ------------------------------------------------------------------


def test_order_disabled_beats_floor(store: Store, gw: Gateway) -> None:
    r = one(store, gw, dest(enabled=False, floor="error"), notice("info"))
    assert r.decision == "disabled"


def test_order_floor_beats_quiet(store: Store, gw: Gateway) -> None:
    at(gw, NIGHT)
    r = one(store, gw, dest(floor="warn", quiet="night"), notice("info"))
    assert r.reason == "below_floor"


def test_order_quiet_info_beats_redelivery_and_repeat(store: Store, gw: Gateway) -> None:
    d = dest(quiet="night")
    assert one(store, gw, d, notice("info", event_id=5)).decision == "send"  # noon
    at(gw, NIGHT)
    r = one(store, gw, d, notice("info", event_id=5))
    assert r.reason == "quiet"
    assert q(store, "SELECT count FROM deliveries ORDER BY id")[0] == (1,)  # repeat not counted


def test_order_redelivery_beats_repeat(store: Store, gw: Gateway) -> None:
    d = dest()
    one(store, gw, d, notice("warn", event_id=5))
    at(gw, NOON + 10)  # inside the repeat window too
    r = one(store, gw, d, notice("warn", event_id=5))
    assert r.reason == "redelivered"
    assert q(store, "SELECT count FROM deliveries") == [(1,)]  # the repeat check did not run


def test_order_repeat_beats_quiet_hold(store: Store, gw: Gateway) -> None:
    at(gw, NIGHT)
    d = dest(quiet="night")
    assert one(store, gw, d, notice("warn")).reason == "quiet"
    r = one(store, gw, d, notice("warn"))
    assert (r.decision, r.reason) == ("suppress", "repeat")
    assert count(store, "queue") == 1


def test_order_quiet_hold_beats_env_missing(store: Store, gw: Gateway) -> None:
    at(gw, NIGHT)
    r = one(store, gw, dest(quiet="night", env=("ROUTE_VAR",)), notice("warn"))
    assert r.reason == "quiet"


def test_order_env_missing_beats_the_cap(store: Store, gw: Gateway) -> None:
    d = dest(max_per_hour=1)
    one(store, gw, d, notice("warn", "a"))
    r = one(store, gw, dest(max_per_hour=1, env=("ROUTE_VAR",)), notice("warn", "b"))
    assert r.reason == "env_missing"
    assert count(store, "surfaced") == 0


def test_a_dropped_destination_leaves_no_dedup_record(store: Store, gw: Gateway) -> None:
    one(store, gw, dest(floor="error"), notice("warn", event_id=3))
    assert (count(store, "dedup"), count(store, "event_ids")) == (0, 0)
    # so the same notification is judged afresh once the floor allows it
    assert one(store, gw, dest(floor="info"), notice("warn", event_id=3)).decision == "send"


def test_dedup_records_belong_to_destinations_ending_in_6_to_9(store: Store, gw: Gateway) -> None:
    n = notice("warn", event_id=9)
    cases = [
        dest("held", quiet="night"),
        dest("envs", env=("ROUTE_VAR",)),
        dest("capped", max_per_hour=1),
        dest("sent"),
    ]
    at(gw, NIGHT)
    cfg = make_config(*cases)
    # the cap needs one earlier call in the hour: a notification of another text
    decide_and_record(store, gw, cfg, notice("warn", "other"), ["capped"])
    decide_and_record(store, gw, cfg, notice("warn", "other2"), ["capped"])
    names = ["held", "envs", "capped", "sent"]
    results = decide_and_record(store, gw, cfg, n, names)
    assert [r.decision for r in results] == ["defer", "defer", "defer", "send"]
    rows = q(store, "SELECT destination, delivery_id FROM event_ids ORDER BY destination")
    assert [r[0] for r in rows] == ["capped", "envs", "held", "sent"]
    assert all(r[1] is not None for r in rows)
    assert count(store, "dedup") >= 4


def test_one_notification_can_go_to_one_destination_and_wait_for_another(
    store: Store, gw: Gateway
) -> None:
    at(gw, NIGHT)
    cfg = make_config(dest("loud"), dest("quiet", quiet="night"))
    a, b = decide_and_record(store, gw, cfg, notice("warn"), ["loud", "quiet"])
    assert (a.decision, b.decision, b.reason) == ("send", "defer", "quiet")
    assert count(store, "deliveries") == 2


# --- counters -------------------------------------------------------------------------------


def test_hour_start_and_class() -> None:
    assert decide.hour_start(3600) == 3600 and decide.hour_start(7199) == 3600
    assert decide.hour_start(0) == 0 and decide.hour_start(-1) == -3600
    assert [decide.rate_class(x) for x in ("info", "warn", "action", "error")] == [
        "other",
        "other",
        "other",
        "error",
    ]
    with pytest.raises(ValueError):
        decide.rate_class("urgent")


@pytest.mark.parametrize("cap", [1, 2, 3, 10])
def test_the_cap_admits_exactly_cap_calls(store: Store, gw: Gateway, cap: int) -> None:
    d = dest(max_per_hour=cap)
    decisions = [one(store, gw, d, notice("warn", f"m{i}")).decision for i in range(cap + 2)]
    assert decisions == ["send"] * cap + ["defer"] * 2  # limit - 1, limit, limit + 1


def test_cap_boundary_by_one_call(store: Store, gw: Gateway) -> None:
    d = dest(max_per_hour=3)
    results = [one(store, gw, d, notice("warn", f"m{i}")) for i in range(4)]
    assert [r.decision for r in results] == ["send", "send", "send", "defer"]
    assert q(store, "SELECT count FROM rate") == [(3,)]


def test_error_counts_in_its_own_class(store: Store, gw: Gateway) -> None:
    d = dest(max_per_hour=1)
    assert one(store, gw, d, notice("warn", "a")).decision == "send"
    assert one(store, gw, d, notice("warn", "b")).decision == "defer"
    assert one(store, gw, d, notice("error", "c")).decision == "send"  # its own budget
    assert one(store, gw, d, notice("error", "d")).reason == "rate_capped"
    assert q(store, "SELECT class, count FROM rate ORDER BY class") == [("error", 1), ("other", 1)]


def test_info_warn_and_action_share_the_other_class(store: Store, gw: Gateway) -> None:
    d = dest(max_per_hour=2)
    assert one(store, gw, d, notice("info", "a")).decision == "send"
    assert one(store, gw, d, notice("action", "b")).decision == "send"
    assert one(store, gw, d, notice("warn", "c")).reason == "rate_capped"


def test_counters_are_per_destination(store: Store, gw: Gateway) -> None:
    cfg = make_config(dest("a", max_per_hour=1), dest("b", max_per_hour=1))
    first = decide_and_record(store, gw, cfg, notice("warn", "x"), ["a", "b"])
    second = decide_and_record(store, gw, cfg, notice("warn", "y"), ["a", "b"])
    assert [r.decision for r in first] == ["send", "send"]
    assert [r.decision for r in second] == ["defer", "defer"]


def test_the_counter_rolls_over_at_the_utc_hour(store: Store, gw: Gateway) -> None:
    d = dest(max_per_hour=1)
    edge = NOON + HOUR  # 13:00:00
    at(gw, edge - 1)
    assert one(store, gw, d, notice("warn", "a")).decision == "send"
    r = one(store, gw, d, notice("warn", "b"))
    assert (r.reason, r.deliver_after) == ("rate_capped", edge)
    at(gw, edge)  # one second later: a new hour
    assert one(store, gw, d, notice("warn", "c")).decision == "send"
    assert q(store, "SELECT hour, count FROM rate ORDER BY hour") == [(NOON, 1), (edge, 1)]


def test_a_capped_row_is_due_exactly_at_the_hour_it_names(store: Store, gw: Gateway) -> None:
    d = dest(max_per_hour=1)
    at(gw, NOON + HOUR - 1)
    one(store, gw, d, notice("warn", "a"))
    r = one(store, gw, d, notice("warn", "b"))
    assert r.deliver_after == NOON + HOUR


def test_max_per_hour_zero_means_no_cap_and_is_still_counted(store: Store, gw: Gateway) -> None:
    d = dest(max_per_hour=0)
    for i in range(40):
        assert one(store, gw, d, notice("warn", f"m{i}")).decision == "send"
    assert q(store, "SELECT count FROM rate") == [(40,)]


def test_the_first_rate_capped_deferral_per_hour_is_surfaced(store: Store, gw: Gateway) -> None:
    d = dest(max_per_hour=1)
    one(store, gw, d, notice("warn", "a"))
    first = one(store, gw, d, notice("warn", "b"))
    second = one(store, gw, d, notice("warn", "c"))
    assert q(store, "SELECT destination, code, period, delivery_id, processed FROM surfaced") == [
        ("d", "rate_capped", "2023-11-14T12", first.delivery_id, 0)
    ]
    assert q(store, "SELECT id, surfaced FROM deliveries ORDER BY id")[1:] == [
        (first.delivery_id, 1),
        (second.delivery_id, 0),
    ]
    at(gw, NOON + HOUR)  # next hour: send one, then cap again and surface again
    one(store, gw, d, notice("warn", "d"))
    third = one(store, gw, d, notice("warn", "e"))
    rows = q(store, "SELECT period, delivery_id FROM surfaced ORDER BY id")
    assert rows == [("2023-11-14T12", first.delivery_id), ("2023-11-14T13", third.delivery_id)]


def test_a_rate_capped_row_evicted_at_once_is_surfaced_as_overflow_not_rate_capped(
    store: Store, gw: Gateway
) -> None:
    at(gw, NIGHT)
    d = dest(quiet="night", max_per_hour=1)
    for i in range(100):  # a full queue of action rows, held through the night
        assert one(store, gw, d, notice("action", f"m{i}")).decision == "defer"
    at(gw, NOON)
    assert one(store, gw, d, notice("warn", "a")).decision == "send"
    evicted = one(store, gw, d, notice("warn", "b"))  # capped, and the lowest row: dropped
    assert (evicted.decision, evicted.reason) == ("drop", "overflow")
    assert q(store, "SELECT code, delivery_id FROM surfaced") == [
        ("overflow", evicted.delivery_id)
    ]  # the hour's rate_capped slot is not used
    assert q(store, "SELECT surfaced FROM deliveries WHERE id = ?", (evicted.delivery_id,)) == [
        (1,)
    ]
    assert one(store, gw, d, notice("error", "c")).decision == "send"
    kept = one(store, gw, d, notice("error", "e"))  # capped, and kept: the hour's first surfaced
    assert (kept.decision, kept.reason) == ("defer", "rate_capped")
    assert q(store, "SELECT delivery_id FROM surfaced WHERE code = 'rate_capped'") == [
        (kept.delivery_id,)
    ]
    assert q(store, "SELECT surfaced FROM deliveries WHERE id = ?", (kept.delivery_id,)) == [(1,)]


def test_surfacing_is_per_destination(store: Store, gw: Gateway) -> None:
    cfg = make_config(dest("a", max_per_hour=1), dest("b", max_per_hour=1))
    for text in ("x", "y"):
        decide_and_record(store, gw, cfg, notice("warn", text), ["a", "b"])
    assert q(store, "SELECT destination FROM surfaced ORDER BY destination") == [("a",), ("b",)]


def test_claim_call_unit(store: Store) -> None:
    with store.write() as conn:
        kw = {"destination": "d", "level": "warn", "now": NOON, "cap": 2}
        assert [decide.claim_call(conn, **kw) for _ in range(3)] == [True, True, False]  # type: ignore[arg-type]
        assert decide.rate_count(conn, "d", "warn", NOON) == 2
        assert decide.rate_count(conn, "d", "error", NOON) == 0
        assert decide.rate_count(conn, "d", "warn", NOON + HOUR) == 0


def test_give_back_returns_a_count_and_never_goes_below_zero(store: Store) -> None:
    hour = NOON - NOON % HOUR
    with store.write() as conn:
        decide.claim_call(conn, destination="d", level="warn", now=NOON, cap=1)
        assert decide.claim_call(conn, destination="d", level="warn", now=NOON, cap=1) is False
        assert decide.give_back(conn, destination="d", level="info", hour=hour) is True
        assert decide.give_back(conn, destination="d", level="info", hour=hour) is False
        assert decide.rate_count(conn, "d", "warn", NOON) == 0
        assert decide.claim_call(conn, destination="d", level="warn", now=NOON, cap=1) is True
        assert decide.give_back(conn, destination="x", level="warn", hour=hour) is False
        assert decide.give_back(conn, destination="d", level="error", hour=hour) is False


def test_count_call_ignores_the_cap(store: Store) -> None:
    with store.write() as conn:
        for _ in range(5):
            decide.count_call(conn, destination="d", level="warn", now=NOON)
        assert decide.rate_count(conn, "d", "warn", NOON) == 5
        assert decide.claim_call(conn, destination="d", level="warn", now=NOON, cap=5) is False


def test_a_counter_at_int64_max_neither_wraps_nor_becomes_a_float(store: Store) -> None:
    top = (1 << 63) - 1
    with store.write() as conn:
        conn.execute("INSERT INTO rate VALUES ('d', ?, 'other', ?)", (NOON - NOON % HOUR, top))
        assert decide.claim_call(conn, destination="d", level="warn", now=NOON, cap=0) is False
        decide.count_call(conn, destination="d", level="warn", now=NOON)
    row = q(store, "SELECT count, typeof(count) FROM rate")[0]
    assert row == (top, "integer")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"destination": 1},
        {"level": "urgent"},
        {"now": True},
        {"now": 1.5},
        {"now": 1 << 41},
        {"cap": -1},
        {"cap": True},
        {"cap": 1 << 32},
        {"cap": 1.0},
    ],
)
def test_claim_call_rejects_bad_arguments(store: Store, kwargs: dict[str, object]) -> None:
    args: dict[str, object] = {"destination": "d", "level": "warn", "now": NOON, "cap": 3}
    args.update(kwargs)
    with pytest.raises(ValueError), store.write() as conn:
        decide.claim_call(conn, **args)  # type: ignore[arg-type]
    assert count(store, "rate") == 0


def test_deciding_never_deletes_a_counter_row(store: Store, gw: Gateway) -> None:
    """Counter deletion belongs to ``prune`` (#26): a row far in the past or ahead survives."""
    hour = NOON - NOON % HOUR
    hours = [hour - 25 * HOUR, hour - HOUR, hour + 24 * HOUR, hour + 25 * HOUR]
    with store.write() as conn:
        for h in hours:
            conn.execute("INSERT INTO rate VALUES ('d', ?, 'other', 1)", (h,))
    one(store, gw, dest(), notice("warn"))
    with store.write() as conn:
        decide.count_call(conn, destination="d", level="warn", now=NOON)
    assert [r[0] for r in q(store, "SELECT hour FROM rate ORDER BY hour")] == sorted([*hours, hour])


def test_a_counter_ahead_by_a_day_does_not_reset_the_current_hours_cap(
    store: Store, gw: Gateway
) -> None:
    d = dest(max_per_hour=1)
    assert one(store, gw, d, notice("warn", "a")).decision == "send"
    at(gw, NOON + 25 * HOUR)  # a clock that stepped forward: that hour has its own counter
    assert one(store, gw, d, notice("warn", "b")).decision == "send"
    at(gw, NOON)  # and back: the first hour's counter is still there, at its cap
    assert one(store, gw, d, notice("warn", "c")).decision == "defer"


# --- the clock ------------------------------------------------------------------------------


def test_a_wall_clock_that_steps_back_uses_the_hour_it_reads(store: Store, gw: Gateway) -> None:
    d = dest(max_per_hour=1)
    one(store, gw, d, notice("warn", "a"))
    assert one(store, gw, d, notice("warn", "b")).decision == "defer"
    at(gw, NOON - HOUR)  # stepped back one hour: that hour has its own (empty) counter
    assert one(store, gw, d, notice("warn", "c")).decision == "send"
    at(gw, NOON)  # and back again: the original counter is still at the cap
    assert one(store, gw, d, notice("warn", "d")).decision == "defer"


def test_a_jump_forward_a_day_gets_a_fresh_counter(store: Store, gw: Gateway) -> None:
    d = dest(max_per_hour=1)
    one(store, gw, d, notice("warn", "a"))
    at(gw, NOON + 86400)
    assert one(store, gw, d, notice("warn", "b")).decision == "send"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), 1e300, -1e300])
def test_an_unreadable_clock_raises_and_writes_nothing(
    store: Store, gw: Gateway, bad: float
) -> None:
    assert isinstance(gw.clock, FakeClock)
    gw.clock._wall = bad  # a FakeClock refuses to be moved there; the test forces the reading
    with pytest.raises(ValueError):
        one(store, gw, dest(), notice("warn"))
    assert (count(store, "deliveries"), count(store, "rate"), count(store, "queue")) == (0, 0, 0)


def test_a_fractional_wall_clock_is_floored(store: Store, gw: Gateway) -> None:
    at(gw, NOON + 0.999)
    r = one(store, gw, dest(), notice("warn"))
    assert r.claim_expires == NOON + LEASE


def test_the_quiet_window_end_follows_the_floored_instant(store: Store, gw: Gateway) -> None:
    at(gw, NEXT_MORNING - 0.5)  # 05:59:59.5 is still quiet
    r = one(store, gw, dest(quiet="night"), notice("warn"))
    assert r.deliver_after == NEXT_MORNING
    at(gw, NEXT_MORNING)  # the window is half open: 06:00:00 is outside
    assert one(store, gw, dest(quiet="night"), notice("warn", "z")).decision == "send"


def test_the_quiet_window_edges(store: Store, gw: Gateway) -> None:
    d = dest(quiet="night")
    at(gw, DAY0 + 22 * HOUR - 1)
    assert one(store, gw, d, notice("info", "a")).decision == "send"
    at(gw, DAY0 + 22 * HOUR)
    assert one(store, gw, d, notice("info", "b")).reason == "quiet"


# --- atomicity ------------------------------------------------------------------------------


def test_a_failure_rolls_the_whole_transaction_back(
    store: Store, gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    import notice_route.decide as module

    real = module.enqueue
    calls: list[int] = []

    def flaky(*args: object, **kwargs: object) -> object:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "enqueue", flaky)
    cfg = make_config(dest("a"), dest("b"))
    with pytest.raises(RuntimeError):
        decide_and_record(store, gw, cfg, notice("warn", event_id=1), ["a", "b"])
    for table in ("deliveries", "queue", "rate", "dedup", "event_ids", "surfaced"):
        assert count(store, table) == 0, table


def test_bind_failure_is_an_error_not_a_silent_skip(
    store: Store, gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(decide.dedup, "bind_window", lambda *a, **k: False)
    with pytest.raises(RuntimeError):
        one(store, gw, dest(), notice("warn"))
    assert count(store, "deliveries") == 0


def test_a_readonly_store_cannot_decide(home: Path, gw: Gateway) -> None:
    with open_store(gw):
        pass
    ro = open_readonly(gw)
    assert ro is not None
    with ro, pytest.raises(RuntimeError):
        one(ro, gw, dest(), notice("warn"))


# --- the cap on queued rows -----------------------------------------------------------------


def test_a_held_row_below_every_queued_row_is_dropped_for_overflow(
    store: Store, gw: Gateway
) -> None:
    at(gw, NIGHT)
    d = dest(quiet="night")
    for i in range(100):
        assert one(store, gw, d, notice("action", f"m{i}")).decision == "defer"
    r = one(store, gw, d, notice("warn", "late", event_id=4))
    assert (r.decision, r.outcome, r.reason, r.deliver_after) == (
        "drop",
        "dropped",
        "overflow",
        None,
    )
    assert count(store, "queue") == 100
    assert q(
        store, "SELECT outcome, reason, code FROM deliveries WHERE id = ?", (r.delivery_id,)
    ) == [("dropped", "overflow", "overflow")]
    # a dropped delivery releases its records
    assert q(store, "SELECT COUNT(*) FROM event_ids WHERE delivery_id = ?", (r.delivery_id,)) == [
        (0,)
    ]
    assert q(store, "SELECT COUNT(*) FROM dedup WHERE delivery_id = ?", (r.delivery_id,)) == [(0,)]


def test_a_higher_level_evicts_an_older_lower_one(store: Store, gw: Gateway) -> None:
    at(gw, NIGHT)
    d = dest(quiet="night")
    first = one(store, gw, d, notice("warn", "old"))
    for i in range(99):
        one(store, gw, d, notice("warn", f"m{i}"))
    r = one(store, gw, d, notice("action", "new"))
    assert r.decision == "defer"
    assert q(store, "SELECT outcome FROM deliveries WHERE id = ?", (first.delivery_id,)) == [
        ("dropped",)
    ]


def test_send_now_rows_are_claimed_and_never_evicted(store: Store, gw: Gateway) -> None:
    at(gw, NIGHT)
    d = dest(quiet="night")
    sent = [one(store, gw, d, notice("error", f"e{i}")) for i in range(5)]
    for i in range(101):
        one(store, gw, d, notice("action", f"m{i}"))
    assert all(
        q(store, "SELECT outcome FROM deliveries WHERE id = ?", (s.delivery_id,)) == [("sending",)]
        for s in sent
    )
    assert q(store, "SELECT COUNT(*) FROM queue WHERE claimed_by IS NOT NULL") == [(5,)]


# --- stored values --------------------------------------------------------------------------


def test_the_delivery_row_holds_the_rendered_facts(store: Store, gw: Gateway) -> None:
    n = notice(
        "warn",
        "msg",
        title="x" * 300,
        event_id="ev-1",
        object_id=7,
        labels=(("b", "2"), ("a", "1")),
    )
    one(store, gw, dest(), n)
    row = q(
        store,
        "SELECT ts, source, hook, object_id, rule, destination, level, event_id, dedupe, labels,"
        " length(title), title_sha256, message_sha256, count, surfaced FROM deliveries",
    )[0]
    import hashlib

    assert row[:9] == (NOON, "s", "h", 7, "r", "d", "warn", '"ev-1"', n.dedupe)
    assert json.loads(str(row[9])) == ["a=1", "b=2"]
    assert row[10] == 100
    assert row[11] == hashlib.sha256(("x" * 300).encode()).hexdigest()[:16]
    assert row[12] == hashlib.sha256(b"msg").hexdigest()[:16]
    assert row[13:] == (1, 0)


def test_a_pass_through_row_keeps_no_title(store: Store, gw: Gateway) -> None:
    one(store, gw, dest(), notice("warn", title="own title", pass_through=True))
    assert q(store, "SELECT title FROM deliveries") == [(None,)]
    assert q(store, "SELECT title FROM queue") == [("own title",)]


def test_an_empty_title_and_link_are_stored_as_null(store: Store, gw: Gateway) -> None:
    one(store, gw, dest(), notice("warn", title=""))
    assert q(store, "SELECT title, link FROM queue") == [(None, None)]


def test_labels_text() -> None:
    assert decide.labels_text(()) is None
    many = tuple((f"k{i:02}", "v") for i in range(30))[::-1]
    out = json.loads(decide.labels_text(many) or "")
    assert out == [f"k{i:02}=v" for i in range(16)]
    wide = tuple((f"k{i}", "v" * 256) for i in range(10))
    text = decide.labels_text(wide) or ""
    assert len(text) <= 1024 and len(json.loads(text)) == 3
    assert decide.labels_text((("a", "1"), ("a", "1"))) == '["a=1"]'


def test_object_id_beyond_int64_is_stored_as_null(store: Store, gw: Gateway) -> None:
    one(store, gw, dest(), notice("warn", object_id=1 << 70))
    assert q(store, "SELECT object_id FROM deliveries") == [(None,)]


def test_object_id_at_int64_max_is_kept(store: Store, gw: Gateway) -> None:
    one(store, gw, dest(), notice("warn", object_id=(1 << 63) - 1))
    assert q(store, "SELECT object_id FROM deliveries") == [((1 << 63) - 1,)]


@pytest.mark.parametrize(
    "event_id", [True, pytest.param(1 << 70000, id="huge-int"), "has space", "x" * 300, None, 1.5]
)
def test_event_ids_that_are_not_plain_are_ignored(
    store: Store, gw: Gateway, event_id: object
) -> None:
    one(store, gw, dest(), notice("warn", event_id=event_id))  # type: ignore[arg-type]
    assert count(store, "event_ids") == 0
    assert q(store, "SELECT event_id FROM deliveries") == [(None,)]


def test_hostile_text_is_stored_verbatim_through_bound_parameters(
    store: Store, gw: Gateway
) -> None:
    evil = "x'); DROP TABLE deliveries; --"
    uni = "‮\U0001f600\u0000end"
    one(store, gw, dest(), notice("warn", message=evil + uni, title=evil))
    assert count(store, "deliveries") == 1
    assert q(store, "SELECT message, title FROM queue") == [(evil + uni, evil)]


def test_the_event_id_retention_is_converted_once_with_ceil(store: Store, gw: Gateway) -> None:
    cfg = make_config(dest(), retention_event_ids=86400.4)  # a float of seconds
    decide_and_record(store, gw, cfg, notice("warn", event_id=2), ["d"])
    at(gw, NOON + 86400)  # inside ceil(86400.4) = 86401 s
    (r,) = decide_and_record(store, gw, cfg, notice("warn", event_id=2), ["d"])
    assert r.reason == "redelivered"
    at(gw, NOON + 86401 + 1)
    (r,) = decide_and_record(store, gw, cfg, notice("warn", event_id=2), ["d"])
    assert r.decision == "send"


def test_duplicate_destination_names_are_decided_once(store: Store, gw: Gateway) -> None:
    cfg = make_config(dest())
    out = decide_and_record(store, gw, cfg, notice("warn"), ["d", "d"])
    assert len(out) == 1 and count(store, "deliveries") == 1


def test_an_unknown_level_is_refused(store: Store, gw: Gateway) -> None:
    with pytest.raises(ValueError):
        one(store, gw, dest(), notice("urgent"))
    assert count(store, "deliveries") == 0


# --- preview (test and send --dry-run) ------------------------------------------------------


def previewed(
    gw: Gateway, d: object, n: Notice, *, opener: object = None
) -> tuple[list[DestinationDecision], list[int]]:
    opened: list[int] = []
    cfg = make_config(*(d if isinstance(d, tuple) else (d,)))  # type: ignore[arg-type]

    def open_state() -> Store | None:
        opened.append(1)
        return open_readonly(gw)

    result = preview(
        open_state if opener is None else opener,  # type: ignore[arg-type]
        cfg,
        n,
        list(cfg.destinations),
        now=int(gw.wall()),
        environ=gw.environ(),
    )
    return result, opened


def test_preview_does_not_open_the_state_for_steps_1_to_3(gw: Gateway) -> None:
    for d, level in ((dest(enabled=False), "warn"), (dest(floor="error"), "warn")):
        r, opened = previewed(gw, d, notice(level))
        assert opened == [] and r[0].decision in ("disabled", "drop")
    at(gw, NIGHT)
    r, opened = previewed(gw, dest(quiet="night"), notice("info"))
    assert opened == [] and r[0].reason == "quiet"


def test_preview_with_no_database_reads_as_empty(gw: Gateway) -> None:
    r, opened = previewed(gw, dest(), notice("warn"))
    assert opened == [1] and r[0].decision == "send" and r[0].deliver_after is None


def test_preview_opens_the_state_once_for_many_destinations(gw: Gateway) -> None:
    r, opened = previewed(gw, (dest("a"), dest("b"), dest("c")), notice("warn"))
    assert opened == [1] and len(r) == 3


def test_preview_matches_the_real_decision_for_every_step(home: Path) -> None:
    cases: list[tuple[str, Destination, str, float, bool]] = [
        ("disabled", dest(enabled=False), "warn", NOON, False),
        ("floor", dest(floor="action"), "warn", NOON, False),
        ("quiet-info", dest(quiet="night"), "info", NIGHT, False),
        ("quiet-warn", dest(quiet="night"), "warn", NIGHT, False),
        ("env", dest(env=("ROUTE_VAR",)), "warn", NOON, False),
        ("send", dest(), "warn", NOON, False),
        ("repeat", dest(), "warn", NOON + 5, True),
        ("redelivered", dest(), "warn", NOON + 700, True),
        ("capped", dest(max_per_hour=1), "warn", NOON + 700, True),
    ]
    for name, d, level, wall, seeded in cases:
        sub = home / name
        sub.mkdir()
        gw = gateway(sub, NOON)
        with open_store(gw) as store:
            if seeded:
                one(store, gw, d, notice("warn", event_id=1))
            at(gw, wall)
            n = notice(level, event_id=1)
            expected, _ = previewed(gw, d, n)
            before = [tuple(r) for r in q(store, "SELECT * FROM rate")]
            actual = one(store, gw, d, n)
            e = expected[0]
            if actual.reason == "overflow":
                continue
            assert (e.decision, e.reason, e.deliver_after) == (
                actual.decision,
                actual.reason,
                actual.deliver_after if actual.decision == "defer" else None,
            ), name
            assert before is not None


def test_preview_writes_nothing(gw: Gateway, home: Path) -> None:
    with open_store(gw) as store:
        one(store, gw, dest(max_per_hour=1), notice("warn", event_id=1))
    files = sorted((home / ".local" / "state" / "notice-route").glob("*"))
    before = {p.name: p.read_bytes() for p in files}
    for n in (notice("warn", event_id=1), notice("warn", "other"), notice("error", "x")):
        previewed(gw, dest(max_per_hour=1), n)
    after = {
        p.name: p.read_bytes()
        for p in sorted((home / ".local" / "state" / "notice-route").glob("*"))
    }
    assert after == before


def test_preview_sees_a_cap_that_a_writer_reached(gw: Gateway) -> None:
    d = dest(max_per_hour=2)
    with open_store(gw) as store:
        for i in range(2):
            one(store, gw, d, notice("warn", f"m{i}"))
    r, _ = previewed(gw, d, notice("warn", "new"))
    assert (r[0].decision, r[0].reason, r[0].deliver_after) == ("defer", "rate_capped", NOON + HOUR)
    r, _ = previewed(gw, d, notice("error", "new"))
    assert r[0].decision == "send"
    r, _ = previewed(gw, dest(max_per_hour=0), notice("warn", "new"))
    assert r[0].decision == "send"


def test_preview_reads_a_corrupt_database_as_an_error(home: Path, gw: Gateway) -> None:
    state = home / ".local" / "state" / "notice-route"
    state.mkdir(parents=True)
    state.chmod(0o700)
    db = state / "state.db"
    db.write_bytes(b"not a database" * 100)
    db.chmod(0o600)
    with pytest.raises(StoreError):
        previewed(gw, dest(), notice("warn"))


# --- parallel senders -----------------------------------------------------------------------

CHILD = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[5])
from notice_route.decide import decide_and_record
from notice_route.store import open_store, SEND_BUSY_S
from tests.decide_support import dest, gateway, make_config, notice
home, go, cap, tag = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
gw = gateway(Path(home))
cfg = make_config(dest("d", max_per_hour=cap))
deadline = time.monotonic() + 30
while not Path(go).exists() and time.monotonic() < deadline:
    time.sleep(0.005)
with open_store(gw, busy_timeout=20) as store:
    (r,) = decide_and_record(store, gw, cfg, notice("warn", "m" + tag), ["d"])
print(json.dumps(r.decision))
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


def child_env(home: Path) -> dict[str, str]:
    return {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": "/usr/bin:/bin"}


def spawn(
    children: list[subprocess.Popen[str]], home: Path, go: Path, cap: int, tags: Sequence[str]
) -> list[str]:
    for tag in tags:
        children.append(
            subprocess.Popen(
                [sys.executable, "-I", "-c", CHILD, str(home), str(go), str(cap), tag, str(ROOT)],
                env=child_env(home),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    time.sleep(0.5)  # every child polls for the start file
    go.write_text("go")
    out: list[str] = []
    for p in children:
        stdout, stderr = p.communicate(timeout=60)
        assert p.returncode == 0, stderr
        out.append(json.loads(stdout))
    return out


@pytest.mark.parametrize("cap", [1, 3])
def test_eight_parallel_senders_cannot_pass_the_cap_together(
    gw: Gateway,
    home: Path,
    tmp_path: Path,
    children: list[subprocess.Popen[str]],
    cap: int,
) -> None:
    with open_store(gw):
        pass  # migrate first, so the children race on the decision
    results = spawn(children, home, tmp_path / "go", cap, [str(i) for i in range(8)])
    assert results.count("send") == cap and results.count("defer") == 8 - cap
    with open_store(gw) as store:
        assert q(store, "SELECT count FROM rate") == [(cap,)]
        assert count(store, "deliveries") == 8
        assert q(store, "SELECT COUNT(*) FROM queue WHERE claimed_by IS NOT NULL") == [(cap,)]
        assert q(store, "SELECT COUNT(*) FROM queue WHERE kind = 'held'") == [(8 - cap,)]
        assert count(store, "surfaced") == 1  # one surfacing for the hour


def test_eight_parallel_senders_of_one_text_let_exactly_one_through(
    gw: Gateway, home: Path, tmp_path: Path, children: list[subprocess.Popen[str]]
) -> None:
    with open_store(gw):
        pass
    results = spawn(children, home, tmp_path / "go", 30, ["same"] * 8)
    assert results.count("send") == 1 and results.count("suppress") == 7
    with open_store(gw) as store:
        assert q(store, "SELECT count FROM deliveries") == [(8,)]
        assert q(store, "SELECT count FROM rate") == [(1,)]


HANG = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from notice_route import decide
from notice_route.store import open_store
from tests.decide_support import gateway, NOON
gw = gateway(Path(sys.argv[1]))
with open_store(gw) as store:
    with store.write() as conn:
        decide.claim_call(conn, destination="d", level="warn", now=NOON, cap=1)
        print("ready", flush=True)
        time.sleep(60)
"""


def test_a_sender_killed_inside_its_transaction_leaves_no_count(
    gw: Gateway, home: Path, children: list[subprocess.Popen[str]]
) -> None:
    with open_store(gw):
        pass
    p = subprocess.Popen(
        [sys.executable, "-I", "-c", HANG, str(home), str(ROOT)],
        env=child_env(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    children.append(p)
    assert p.stdout is not None and p.stdout.readline().strip() == "ready"
    os.kill(p.pid, signal.SIGKILL)  # the exact process started above
    p.wait(timeout=30)
    with open_store(gw) as store:
        assert count(store, "rate") == 0
        assert one(store, gw, dest(max_per_hour=1), notice("warn")).decision == "send"


def test_a_database_that_cannot_be_opened_is_left_unchanged(home: Path, gw: Gateway) -> None:
    state = home / ".local" / "state" / "notice-route"
    state.mkdir(parents=True)
    state.chmod(0o700)
    db = state / "state.db"
    db.write_bytes(b"garbage" * 200)
    db.chmod(0o600)
    before = db.read_bytes()
    with pytest.raises(StoreError):
        open_store(gw)
    assert db.read_bytes() == before
    assert sorted(p.name for p in state.iterdir() if p.name.startswith("state.db")) == ["state.db"]
