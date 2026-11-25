"""Quiet-hour membership and the next window end (#15), over UTC epoch seconds.

Expectations are pinned to stable rules: the United States and European DST rules, fixed offsets
and a handful of zones whose rules have not changed in years. Every zone comes from the system
database through ``Gateway.load_zone`` (the one place allowed to read it).
"""

from __future__ import annotations

import functools
import math
import os
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from notice_route.gateway import FakeClock, Gateway, ZoneError
from notice_route.validate import QuietWindow
from notice_route.window import MAX_EPOCH, MIN_EPOCH, in_window, window_end

_GATEWAY = Gateway()
try:
    _GATEWAY.load_zone("UTC")
    _HAVE_ZONES = True
except ZoneError:
    _HAVE_ZONES = False

if not _HAVE_ZONES:
    if os.environ.get("CI"):
        pytest.fail("CI needs a system time zone database", pytrace=False)
    pytest.skip("no system time zone database", allow_module_level=True)

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
ALL = tuple(range(7))


@pytest.fixture(autouse=True)
def _private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("TZ", raising=False)


def hm(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def win(
    tz: str, start: str, end: str, days: tuple[int, ...] = ALL, *, zone: ZoneInfo | None = None
) -> QuietWindow:
    return QuietWindow("w", tz, zone or _GATEWAY.load_zone(tz), days, hm(start), hm(end))


def utc(text: str) -> int:
    """``2026-03-08T07:00`` or ``...:30`` (seconds optional) as UTC epoch seconds."""
    parsed = datetime.fromisoformat(text)
    return int(parsed.replace(tzinfo=UTC).timestamp())


# ---- membership in UTC -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("when", "inside"),
    [
        ("2026-01-05T21:59", False),  # a minute before start
        ("2026-01-05T22:00", True),  # start is inclusive
        ("2026-01-05T23:58", True),
        ("2026-01-05T23:59", False),  # end is exclusive
        ("2026-01-06T00:00", False),
    ],
)
def test_a_window_that_starts_before_its_end_is_half_open(when: str, inside: bool) -> None:
    assert in_window(win("UTC", "22:00", "23:59"), utc(when)) is inside


@pytest.mark.parametrize(
    ("when", "inside"),
    [
        ("2026-01-05T08:59", False),
        ("2026-01-05T09:00", True),
        ("2026-01-05T16:59", True),
        ("2026-01-05T17:00", False),  # end is exclusive
        ("2026-01-06T09:00", False),  # Tuesday is not in days
        ("2026-01-12T12:00", True),  # the next Monday
    ],
)
def test_same_day_window_uses_its_days(when: str, inside: bool) -> None:
    assert in_window(win("UTC", "09:00", "17:00", (MON,)), utc(when)) is inside


@pytest.mark.parametrize(
    ("when", "inside"),
    [
        ("2026-01-09T21:59", False),  # Friday before start
        ("2026-01-09T22:00", True),  # Friday at start
        ("2026-01-09T23:59", True),
        ("2026-01-10T00:00", True),  # Saturday midnight belongs to Friday's window
        ("2026-01-10T05:59", True),
        ("2026-01-10T06:00", False),  # end is exclusive
        ("2026-01-10T22:00", False),  # Saturday is not in days
        ("2026-01-09T05:59", False),  # Friday morning: Thursday is not in days
        ("2026-01-09T00:00", False),
        ("2026-01-11T03:00", False),  # Sunday: Saturday is not in days
    ],
)
def test_a_window_ending_next_day_uses_the_previous_weekday(when: str, inside: bool) -> None:
    assert in_window(win("UTC", "22:00", "06:00", (FRI,)), utc(when)) is inside


def test_the_week_wraps_from_sunday_to_monday() -> None:
    window = win("UTC", "22:00", "06:00", (SUN,))
    assert in_window(window, utc("2026-01-12T03:00")) is True  # Monday after Sunday
    assert in_window(window, utc("2026-01-12T06:00")) is False
    assert in_window(window, utc("2026-01-11T23:00")) is True  # Sunday itself
    assert in_window(window, utc("2026-01-06T03:00")) is False  # Tuesday: Monday not in days


def test_consecutive_days_join_into_one_stretch_with_a_daily_gap() -> None:
    window = win("UTC", "22:00", "06:00", (FRI, SAT))
    assert in_window(window, utc("2026-01-10T12:00")) is False  # Saturday noon: the gap
    assert in_window(window, utc("2026-01-10T23:00")) is True
    assert in_window(window, utc("2026-01-11T05:00")) is True  # Sunday morning, Saturday's tail
    assert in_window(window, utc("2026-01-11T06:00")) is False


def test_a_window_covering_all_but_one_minute() -> None:
    window = win("UTC", "00:01", "00:00")
    assert in_window(window, utc("2026-01-07T00:00")) is False
    assert in_window(window, utc("2026-01-07T00:01")) is True
    assert in_window(window, utc("2026-01-07T23:59")) is True
    assert in_window(window, utc("2026-01-08T00:00")) is False


def test_a_window_over_the_whole_day_but_the_last_minute() -> None:
    window = win("UTC", "00:00", "23:59")
    assert in_window(window, utc("2026-01-07T00:00")) is True
    assert in_window(window, utc("2026-01-07T23:58:59")) is True
    assert in_window(window, utc("2026-01-07T23:59")) is False


def test_seconds_do_not_change_the_minute() -> None:
    window = win("UTC", "09:00", "17:00")
    assert in_window(window, utc("2026-01-07T16:59:59")) is True
    assert in_window(window, utc("2026-01-07T17:00:00")) is False


# ---- integer and float seconds, epoch edges, bad inputs ------------------------------------------


def test_float_and_int_seconds_agree() -> None:
    window = win("UTC", "09:00", "17:00")
    base = utc("2026-01-07T17:00")
    assert in_window(window, base - 0.001) is True
    assert in_window(window, float(base)) is False
    assert in_window(window, base - 1) is True
    assert in_window(window, float(base - 1)) is True


def test_negative_fractions_floor_toward_the_past() -> None:
    window = win("UTC", "23:59", "00:30")  # the minute before the epoch
    assert in_window(window, -0.5) is True  # 1969-12-31T23:59:59.5
    assert in_window(window, 0.0) is True  # 1970-01-01T00:00
    assert in_window(window, 30 * 60.0) is False  # 00:30


def test_the_epoch_is_thursday_midnight() -> None:
    assert in_window(win("UTC", "00:00", "01:00", (THU,)), 0) is True
    assert in_window(win("UTC", "00:00", "01:00", (WED,)), 0) is False
    assert in_window(win("UTC", "23:00", "01:00", (WED,)), 0) is True  # Wednesday's tail
    assert in_window(win("UTC", "23:00", "01:00", (WED,)), -1) is True  # Wednesday 23:59:59


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_instants_are_refused(bad: float) -> None:
    window = win("UTC", "09:00", "17:00")
    with pytest.raises(ValueError, match="finite"):
        in_window(window, bad)
    with pytest.raises(ValueError, match="finite"):
        window_end(window, bad)


@pytest.mark.parametrize("bad", [True, False, "0", None, b"1", 1j])
def test_non_numbers_and_booleans_are_refused(bad: Any) -> None:
    window = win("UTC", "09:00", "17:00")
    with pytest.raises(TypeError):
        in_window(window, bad)
    with pytest.raises(TypeError):
        window_end(window, bad)


@pytest.mark.parametrize("bad", [MIN_EPOCH - 1, MAX_EPOCH + 1, 10**30, -(10**30), 1e300])
def test_instants_outside_the_supported_range_are_refused(bad: float) -> None:
    window = win("Pacific/Kiritimati", "09:00", "17:00")  # +14:00: the worst offset
    with pytest.raises(ValueError, match="range"):
        in_window(window, bad)
    with pytest.raises(ValueError, match="range"):
        window_end(window, bad)


@pytest.mark.parametrize("tz", ["Pacific/Kiritimati", "Pacific/Pago_Pago", "America/Adak"])
def test_the_range_edges_themselves_work_in_the_extreme_zones(tz: str) -> None:
    window = win(tz, "09:00", "17:00")
    assert in_window(window, MIN_EPOCH) in (True, False)
    assert in_window(window, MAX_EPOCH) in (True, False)
    assert window_end(window, MAX_EPOCH) is not None
    assert window_end(window, MIN_EPOCH) is not None


def test_far_future_uses_the_last_rule_of_the_zone() -> None:
    # Year 2400 is a leap year; 2400-03-10 is a Sunday. New York is on EDT in July.
    window = win("America/New_York", "08:00", "09:00")
    assert in_window(window, utc("2400-07-04T12:00")) is True  # 08:00 EDT
    assert in_window(window, utc("2400-07-04T13:00")) is False  # 09:00 EDT
    assert in_window(window, utc("2400-01-04T13:00")) is True  # 08:00 EST


def test_far_past_uses_a_deterministic_offset() -> None:
    # Before the first transition of a zone the local mean time applies; membership must
    # still follow the offset the database gives and be stable between calls.
    window = win("America/New_York", "00:00", "12:00")
    first = [in_window(window, utc(f"1850-06-01T{h:02d}:00")) for h in range(24)]
    assert first == [in_window(window, utc(f"1850-06-01T{h:02d}:00")) for h in range(24)]
    assert sum(first) == 12


def test_leap_day_weekday_is_by_the_local_date() -> None:
    # 2028-02-29 is a Tuesday; 2028-03-01 a Wednesday.
    window = win("UTC", "22:00", "06:00", (TUE,))
    assert in_window(window, utc("2028-02-29T23:00")) is True
    assert in_window(window, utc("2028-03-01T05:59")) is True
    assert in_window(window, utc("2028-03-01T06:00")) is False
    assert in_window(win("UTC", "22:00", "06:00", (WED,)), utc("2028-02-29T23:00")) is False
    # 2100 is not a leap year: 2100-03-01 follows 2100-02-28 (a Sunday, then Monday).
    window = win("UTC", "22:00", "06:00", (SUN,))
    assert in_window(window, utc("2100-03-01T03:00")) is True
    assert in_window(window, utc("2100-02-28T23:00")) is True


# ---- zones that are not whole hours ------------------------------------------------------------


@pytest.mark.parametrize(
    ("tz", "offset_minutes"),
    [
        ("Asia/Kolkata", 330),
        ("Asia/Kathmandu", 345),
        ("Australia/Eucla", 525),
        ("Pacific/Chatham", None),  # 12:45 or 13:45; only checked for self-consistency
        ("America/St_Johns", None),  # -3:30 or -2:30
    ],
)
def test_odd_offsets_shift_the_boundary(tz: str, offset_minutes: int | None) -> None:
    window = win(tz, "09:00", "17:00")
    if offset_minutes is not None:
        local_9 = utc("2026-01-07T09:00") - offset_minutes * 60
        assert in_window(window, local_9 - 60) is False
        assert in_window(window, local_9) is True
        assert in_window(window, local_9 + 8 * 3600 - 60) is True
        assert in_window(window, local_9 + 8 * 3600) is False
    else:
        # Walk one day; exactly 8 hours of the day are inside, in a contiguous run
        start = utc("2026-01-07T00:00")
        flags = [in_window(window, start + m * 60) for m in range(-1440, 2880)]
        assert sum(flags[1440 : 1440 + 1440]) in (480, 480 + 0)
        runs = sum(1 for a, b in zip(flags, flags[1:], strict=False) if a != b)
        assert runs <= 6


def test_lord_howe_has_a_thirty_minute_dst_shift() -> None:
    window = win("Australia/Lord_Howe", "02:00", "03:00")
    # Southern summer: +11:00 until the first Sunday of April 2026 (April 5, 02:00 -> 01:30).
    assert in_window(window, utc("2026-01-10T15:00")) is True  # 02:00 at +11
    assert in_window(window, utc("2026-01-10T15:59")) is True
    assert in_window(window, utc("2026-01-10T16:00")) is False
    # Winter +10:30
    assert in_window(window, utc("2026-07-10T15:30")) is True  # 02:00 at +10:30
    assert in_window(window, utc("2026-07-10T16:29")) is True
    assert in_window(window, utc("2026-07-10T16:30")) is False


def test_lord_howe_fall_back_repeats_a_half_hour() -> None:
    # 2026-04-05 02:00 LHDT (+11) becomes 01:30 LHST (+10:30); 01:30-02:00 happens twice.
    # 14:30 UTC on Apr 4 is 01:30 at +11; 15:00 UTC is 01:30 again at +10:30.
    window = win("Australia/Lord_Howe", "01:45", "02:00")
    assert in_window(window, utc("2026-04-04T14:44")) is False
    assert in_window(window, utc("2026-04-04T14:45")) is True  # 01:45 at +11
    assert in_window(window, utc("2026-04-04T14:59")) is True  # 01:59 at +11
    assert in_window(window, utc("2026-04-04T15:00")) is False  # 01:30 at +10:30
    assert in_window(window, utc("2026-04-04T15:15")) is True  # 01:45 at +10:30
    assert in_window(window, utc("2026-04-04T15:30")) is False  # 02:00 at +10:30


def test_casablanca_follows_its_own_offset_in_summer() -> None:
    # Outside Ramadan the zone is at +01:00 all year (tzdata models Ramadan as negative DST).
    window = win("Africa/Casablanca", "09:00", "10:00")
    assert in_window(window, utc("2026-07-15T08:00")) is True
    assert in_window(window, utc("2026-07-15T09:00")) is False
    assert in_window(window, utc("2026-07-15T07:59")) is False


# ---- DST: spring forward in New York (2026-03-08 02:00 EST -> 03:00 EDT, 07:00Z) -----------------


def test_spring_forward_a_start_in_the_gap_begins_at_the_first_real_minute() -> None:
    window = win("America/New_York", "02:30", "04:00")
    assert in_window(window, utc("2026-03-08T06:59")) is False  # 01:59 EST
    assert in_window(window, utc("2026-03-08T07:00")) is True  # 03:00 EDT, already past 02:30
    assert in_window(window, utc("2026-03-08T07:59")) is True
    assert in_window(window, utc("2026-03-08T08:00")) is False  # 04:00 EDT


def test_spring_forward_an_end_in_the_gap_ends_at_the_jump() -> None:
    window = win("America/New_York", "01:00", "02:30")
    assert in_window(window, utc("2026-03-08T05:59")) is False  # 00:59 EST
    assert in_window(window, utc("2026-03-08T06:00")) is True  # 01:00 EST
    assert in_window(window, utc("2026-03-08T06:59")) is True  # 01:59 EST
    assert in_window(window, utc("2026-03-08T07:00")) is False  # 03:00 EDT, past 02:30


def test_spring_forward_a_window_across_midnight_keeps_its_weekday() -> None:
    window = win("America/New_York", "23:00", "03:30", (SAT,))
    assert in_window(window, utc("2026-03-08T04:00")) is True  # Sat 23:00 EST
    assert in_window(window, utc("2026-03-08T06:59")) is True  # Sun 01:59 EST
    assert in_window(window, utc("2026-03-08T07:00")) is True  # Sun 03:00 EDT, before 03:30
    assert in_window(window, utc("2026-03-08T07:29")) is True  # 03:29 EDT
    assert in_window(window, utc("2026-03-08T07:30")) is False  # 03:30 EDT
    assert in_window(window, utc("2026-03-09T03:00")) is False  # Monday: Sunday not in days


def test_spring_forward_a_window_wholly_inside_the_gap_never_matches_that_day() -> None:
    window = win("America/New_York", "02:10", "02:50")
    day = utc("2026-03-08T00:00")
    assert not any(in_window(window, day + m * 60) for m in range(0, 24 * 60))


# ---- DST: fall back in New York (2026-11-01 02:00 EDT -> 01:00 EST, 06:00Z) ---------------------


def test_fall_back_an_hour_of_the_window_happens_twice() -> None:
    window = win("America/New_York", "01:00", "02:00")
    assert in_window(window, utc("2026-11-01T04:59")) is False  # 00:59 EDT
    assert in_window(window, utc("2026-11-01T05:00")) is True  # 01:00 EDT
    assert in_window(window, utc("2026-11-01T05:59")) is True
    assert in_window(window, utc("2026-11-01T06:00")) is True  # 01:00 EST: the fold
    assert in_window(window, utc("2026-11-01T06:59")) is True
    assert in_window(window, utc("2026-11-01T07:00")) is False  # 02:00 EST


def test_fall_back_an_end_in_the_fold_ends_the_window_twice() -> None:
    window = win("America/New_York", "01:00", "01:30")
    assert in_window(window, utc("2026-11-01T05:29")) is True  # 01:29 EDT
    assert in_window(window, utc("2026-11-01T05:30")) is False  # 01:30 EDT
    assert in_window(window, utc("2026-11-01T05:59")) is False  # 01:59 EDT
    assert in_window(window, utc("2026-11-01T06:00")) is True  # 01:00 EST: back in
    assert in_window(window, utc("2026-11-01T06:29")) is True
    assert in_window(window, utc("2026-11-01T06:30")) is False  # 01:30 EST


def test_fall_back_a_start_in_the_fold_starts_the_window_twice() -> None:
    window = win("America/New_York", "01:30", "03:00")
    assert in_window(window, utc("2026-11-01T05:29")) is False  # 01:29 EDT
    assert in_window(window, utc("2026-11-01T05:30")) is True  # 01:30 EDT
    assert in_window(window, utc("2026-11-01T05:59")) is True
    assert in_window(window, utc("2026-11-01T06:00")) is False  # 01:00 EST, before start
    assert in_window(window, utc("2026-11-01T06:29")) is False
    assert in_window(window, utc("2026-11-01T06:30")) is True  # 01:30 EST
    assert in_window(window, utc("2026-11-01T07:59")) is True  # 02:59 EST
    assert in_window(window, utc("2026-11-01T08:00")) is False  # 03:00 EST


def test_fall_back_a_window_across_midnight_keeps_its_weekday() -> None:
    window = win("America/New_York", "22:00", "02:30", (SAT,))
    assert in_window(window, utc("2026-11-01T02:00")) is True  # Sat 22:00 EDT
    assert in_window(window, utc("2026-11-01T06:30")) is True  # Sun 01:30 EST
    assert in_window(window, utc("2026-11-01T07:29")) is True  # Sun 02:29 EST
    assert in_window(window, utc("2026-11-01T07:30")) is False  # Sun 02:30 EST
    assert in_window(window, utc("2026-11-02T00:00")) is False


def test_europe_london_spring_and_autumn() -> None:
    # 2026-03-29 01:00Z: 01:00 GMT -> 02:00 BST. 2026-10-25 01:00Z: 02:00 BST -> 01:00 GMT.
    window = win("Europe/London", "01:30", "02:30")
    assert in_window(window, utc("2026-03-29T00:59")) is False  # 00:59 GMT
    assert in_window(window, utc("2026-03-29T01:00")) is True  # 02:00 BST, past 01:30
    assert in_window(window, utc("2026-03-29T01:29")) is True  # 02:29 BST
    assert in_window(window, utc("2026-03-29T01:30")) is False  # 02:30 BST
    assert in_window(window, utc("2026-10-25T00:30")) is True  # 01:30 BST
    assert in_window(window, utc("2026-10-25T01:00")) is False  # 01:00 GMT
    assert in_window(window, utc("2026-10-25T01:30")) is True  # 01:30 GMT
    assert in_window(window, utc("2026-10-25T02:30")) is False  # 02:30 GMT


def test_southern_hemisphere_dst_runs_the_other_way() -> None:
    # Sydney: DST ends 2026-04-05 03:00 AEDT -> 02:00 AEST (16:00Z on Apr 4).
    window = win("Australia/Sydney", "02:00", "03:00")
    assert in_window(window, utc("2026-04-04T15:00")) is True  # 02:00 AEDT
    assert in_window(window, utc("2026-04-04T15:59")) is True
    assert in_window(window, utc("2026-04-04T16:00")) is True  # 02:00 AEST again
    assert in_window(window, utc("2026-04-04T16:59")) is True
    assert in_window(window, utc("2026-04-04T17:00")) is False  # 03:00 AEST


# ---- wall-clock steps and the injected clock --------------------------------------------------


def test_membership_follows_the_wall_clock_only() -> None:
    clock = FakeClock(wall=float(utc("2026-01-09T23:00")), monotonic=5.0)
    window = win("UTC", "22:00", "06:00", (FRI,))
    assert in_window(window, clock.wall()) is True
    clock.advance_monotonic(10 * 3600)  # monotonic time is irrelevant
    assert in_window(window, clock.wall()) is True
    clock.jump_wall(8 * 3600)  # forward step: Saturday 07:00
    assert in_window(window, clock.wall()) is False
    clock.jump_wall(-8 * 3600 - 3 * 3600)  # backwards step: Friday 20:00
    assert in_window(window, clock.wall()) is False
    clock.jump_wall(3 * 3600)  # and forward again: Friday 23:00
    assert in_window(window, clock.wall()) is True


def test_a_backwards_step_gives_the_same_answer_for_the_same_instant() -> None:
    clock = FakeClock(wall=float(utc("2026-01-10T05:59:30")))
    window = win("America/New_York", "22:00", "01:00", (FRI, SAT))
    before = (in_window(window, clock.wall()), window_end(window, clock.wall()))
    clock.jump_wall(7200)
    clock.jump_wall(-7200)
    assert (in_window(window, clock.wall()), window_end(window, clock.wall())) == before


# ---- window_end -----------------------------------------------------------------------------------


def test_window_end_is_the_first_whole_minute_outside() -> None:
    window = win("UTC", "22:00", "06:00", (FRI,))
    assert window_end(window, utc("2026-01-09T23:15:30")) == utc("2026-01-10T06:00")
    assert window_end(window, utc("2026-01-10T05:59:59.5")) == utc("2026-01-10T06:00")
    assert window_end(window, utc("2026-01-10T05:59")) == utc("2026-01-10T06:00")


def test_window_end_is_strictly_after_now() -> None:
    window = win("UTC", "22:00", "06:00", (FRI,))
    # now already outside: the next whole minute, which is outside too
    assert window_end(window, utc("2026-01-10T06:00")) == utc("2026-01-10T06:01")
    assert window_end(window, utc("2026-01-10T06:00:00.25")) == utc("2026-01-10T06:01")
    assert window_end(window, 0) == 60  # the epoch itself is outside (a Thursday)


def test_window_end_returns_whole_minutes_as_int() -> None:
    window = win("UTC", "22:00", "06:00")
    got = window_end(window, utc("2026-01-09T23:15:30.75"))
    assert type(got) is int
    assert got % 60 == 0
    assert got == utc("2026-01-10T06:00")


def test_window_end_with_negative_fractions() -> None:
    window = win("UTC", "23:00", "00:00")
    assert window_end(window, -0.5) == 0  # 23:59:59.5 on Dec 31 1969 ends at the epoch
    assert window_end(window, -60.0) == 0


def test_window_end_across_spring_forward_with_an_end_in_the_gap() -> None:
    window = win("America/New_York", "22:00", "02:30")
    now = utc("2026-03-08T04:00")  # Saturday 23:00 EST
    assert in_window(window, now) is True
    assert window_end(window, now) == utc("2026-03-08T07:00")  # 03:00 EDT is the first outside


def test_window_end_across_fall_back_waits_for_the_second_pass() -> None:
    window = win("America/New_York", "22:00", "02:30")
    now = utc("2026-11-01T03:00")  # Saturday 23:00 EDT
    assert window_end(window, now) == utc("2026-11-01T07:30")  # 02:30 EST, not 01:30


def test_window_end_with_an_end_in_the_fold_is_the_first_pass() -> None:
    window = win("America/New_York", "01:00", "01:30")
    assert window_end(window, utc("2026-11-01T05:00")) == utc("2026-11-01T05:30")
    assert window_end(window, utc("2026-11-01T06:00")) == utc("2026-11-01T06:30")


def test_window_end_when_the_window_dips_out_for_one_minute() -> None:
    window = win("UTC", "00:01", "00:00")
    assert window_end(window, utc("2026-01-07T12:00")) == utc("2026-01-08T00:00")


def test_window_end_is_deterministic_and_pure() -> None:
    window = win("America/New_York", "22:00", "06:00")
    now = utc("2026-03-07T23:00")
    assert [window_end(window, now) for _ in range(3)] == [window_end(window, now)] * 3


class _FrozenZone(tzinfo):
    """A zone that always reads the same local time: the window never ends (a stub)."""

    def __init__(self) -> None:
        self.calls = 0

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "frozen"

    def fromutc(self, dt: datetime) -> datetime:
        self.calls += 1
        return datetime(2026, 1, 5, 23, 0, tzinfo=self)  # Monday 23:00 forever


def test_a_search_is_bounded_to_two_days_and_gives_up_with_none() -> None:
    zone = _FrozenZone()
    window = QuietWindow("w", "Frozen", cast(ZoneInfo, zone), ALL, hm("22:00"), hm("06:00"))
    assert in_window(window, 1_800_000_000) is True
    zone.calls = 0
    assert window_end(window, 1_800_000_000) is None
    assert zone.calls <= 2 * 24 * 60 + 1


@pytest.mark.parametrize(
    "bad",
    [
        {"days": ()},
        {"days": (7,)},
        {"days": (-1,)},
        {"days": (1, 1)},
        {"start": -1},
        {"end": 1440},
        {"start": 1440},
        {"start": 60, "end": 60},
        {"start": True},
    ],
)
def test_a_malformed_window_is_refused(bad: dict[str, Any]) -> None:
    fields: dict[str, Any] = {"days": ALL, "start": 60, "end": 120}
    fields.update(bad)
    window = QuietWindow(
        "w", "UTC", _GATEWAY.load_zone("UTC"), fields["days"], fields["start"], fields["end"]
    )
    with pytest.raises(ValueError, match="window"):
        in_window(window, 0)
    with pytest.raises(ValueError, match="window"):
        window_end(window, 0)


# ---- the property test: every minute of a year against an independent reference -------------------

_YEAR_START = utc("2026-01-01T00:00")
_YEAR_MINUTES = 365 * 24 * 60
_ORIGIN = datetime(2025, 12, 1)  # the reference counts naive local minutes from here
_SPAN_DAYS = 31 + 365 + 31 + 2

# (start, end, days): same-day, crossing midnight, every day, one day, a start in a New York
# spring-forward gap, an end in the gap, an end in the fall-back fold, a start in the fold.
_CONFIGS: list[tuple[str, str, tuple[int, ...]]] = [
    ("09:00", "17:00", ALL),
    ("22:00", "06:00", ALL),
    ("22:30", "07:15", (FRI, SAT)),
    ("02:30", "04:00", (SUN, MON)),  # start in a spring-forward gap
    ("01:00", "02:30", ALL),  # end in a gap (and 01:00-02:30 spans the fall-back fold)
    ("00:30", "01:30", ALL),  # end in a fold
    ("01:30", "03:00", (SUN,)),  # start in a fold
]
_ZONES = ["America/New_York", "UTC", "Asia/Kolkata"]


def _reference_paint(days: tuple[int, ...], start: int, end: int) -> bytearray:
    """Mark the naive local minutes of every window interval, built from calendar dates.

    Interval ``[day + start, day + end)`` for each listed day, ending the next day when
    ``end`` is not later than ``start``. It never looks at a weekday of the instant tested.
    """
    painted = bytearray(_SPAN_DAYS * 1440)
    for day_index in range(_SPAN_DAYS):
        day = _ORIGIN + timedelta(days=day_index)
        if day.weekday() not in days:
            continue
        begin = day_index * 1440 + start
        finish = day_index * 1440 + (end if start < end else 1440 + end)
        painted[begin : min(finish, len(painted))] = b"\x01" * (min(finish, len(painted)) - begin)
    return painted


def _local_minute_index(zone: ZoneInfo, instant: int) -> int:
    local = (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=instant)).astimezone(zone)
    return (local.replace(tzinfo=None) - _ORIGIN) // timedelta(minutes=1)


@pytest.fixture(scope="module")
def local_indexes() -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for tz in _ZONES:
        zone = _GATEWAY.load_zone(tz)
        result[tz] = [
            _local_minute_index(zone, _YEAR_START + m * 60) for m in range(_YEAR_MINUTES + 1500)
        ]
    return result


@functools.cache
def _transition_minutes(tz: str) -> tuple[int, ...]:
    """Year minute offsets within 3 hours of any change of UTC offset in the year."""
    zone = _GATEWAY.load_zone(tz)
    found: list[int] = []
    previous = None
    for m in range(_YEAR_MINUTES + 1500):
        stamp = _YEAR_START + m * 60
        local = (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=stamp)).astimezone(zone)
        offset = local.utcoffset()
        if previous is not None and offset != previous:
            found.extend(range(max(0, m - 180), m + 180))
        previous = offset
    return tuple(found)


@pytest.mark.parametrize("tz", _ZONES)
@pytest.mark.parametrize(("start", "end", "days"), _CONFIGS)
def test_every_minute_of_2026_matches_the_reference(
    tz: str, start: str, end: str, days: tuple[int, ...], local_indexes: dict[str, list[int]]
) -> None:
    window = win(tz, start, end, days)
    painted = _reference_paint(days, hm(start), hm(end))
    indexes = local_indexes[tz]
    # offset of the first year minute into the painted span: December 2025 is month 0
    expected = [bool(painted[i]) for i in indexes[:_YEAR_MINUTES]]
    got = [in_window(window, _YEAR_START + m * 60) for m in range(_YEAR_MINUTES)]
    mismatches = [m for m, (a, b) in enumerate(zip(got, expected, strict=True)) if a != b]
    assert mismatches == []
    assert any(got) and not all(got)


@pytest.mark.parametrize("tz", _ZONES)
@pytest.mark.parametrize(("start", "end", "days"), _CONFIGS)
def test_window_end_is_the_first_outside_minute_and_within_two_days(
    tz: str, start: str, end: str, days: tuple[int, ...], local_indexes: dict[str, list[int]]
) -> None:
    window = win(tz, start, end, days)
    painted = _reference_paint(days, hm(start), hm(end))
    indexes = local_indexes[tz]
    count = _YEAR_MINUTES + 1500
    inside = [bool(painted[i]) for i in indexes]
    # reference: for each minute m, the first minute k > m that is outside (a backward pass)
    first_outside_after = [0] * count
    nxt = -1
    for m in range(count - 1, -1, -1):
        first_outside_after[m] = nxt
        if not inside[m]:
            nxt = m
    samples = set(range(0, _YEAR_MINUTES, 997))
    samples.update(m for m in _transition_minutes(tz) if m < _YEAR_MINUTES)
    for m in sorted(samples):
        now = _YEAR_START + m * 60
        for fraction in (0, 29):  # on the minute and 29 seconds into it
            got = window_end(window, now + fraction)
            assert got is not None
            assert got % 60 == 0
            assert 0 < got - (now + fraction) <= 2 * 86400
            assert got == _YEAR_START + first_outside_after[m] * 60
            assert in_window(window, got) is False
            if inside[m]:
                assert all(in_window(window, t) for t in range(now + 60 - fraction, got, 60)) or (
                    got - now > 3600 * 30
                )
