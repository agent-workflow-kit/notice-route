"""The retry schedule (#25): docs/interface.md, Retries and outcomes."""

from __future__ import annotations

import math
from typing import Any

import pytest

from notice_route import retry

NOW = 1_700_000_000
FIVE = [60, 300, 1800, 7200, None]
THREE = [60, 300, None]


@pytest.mark.parametrize("kind", ["slack-notice", "ha-notice", "generic"])
@pytest.mark.parametrize("attempts", [1, 2, 3, 4, 5])
def test_the_five_attempt_kinds_follow_the_table_row_by_row(kind: str, attempts: int) -> None:
    got = retry.retry_schedule(kind, NOW)(attempts)
    expected = FIVE[attempts - 1]
    assert got == (None if expected is None else NOW + expected)


@pytest.mark.parametrize("attempts", [1, 2, 3])
def test_wsl_notice_has_three_attempts(attempts: int) -> None:
    got = retry.retry_schedule("wsl-notice", NOW)(attempts)
    expected = THREE[attempts - 1]
    assert got == (None if expected is None else NOW + expected)


@pytest.mark.parametrize("kind", ["slack-notice", "ha-notice", "generic", "wsl-notice"])
def test_every_attempt_beyond_the_cap_is_exhausted_even_a_huge_one(kind: str) -> None:
    cap = retry.attempt_cap(kind)
    at = retry.retry_schedule(kind, NOW)
    for attempts in (cap, cap + 1, cap + 2, 10**6, 2**63, 2**200):
        assert at(attempts) is None


@pytest.mark.parametrize("kind", ["slack-notice", "wsl-notice"])
def test_each_attempt_with_a_step_has_one_and_the_rest_none(kind: str) -> None:
    """flush asks ``at(n)``, n >= 1, to tell an exhausted claimed row: None from the cap on."""
    cap = retry.attempt_cap(kind)
    at = retry.retry_schedule(kind, NOW)
    assert [at(a) is not None for a in range(1, cap + 3)] == [True] * (cap - 1) + [False] * 3


def test_the_caps_are_five_and_three_for_wsl_notice() -> None:
    assert [retry.attempt_cap(k) for k in ("slack-notice", "ha-notice", "generic")] == [5, 5, 5]
    assert retry.attempt_cap("wsl-notice") == 3


def test_an_unknown_kind_gets_the_default_cap() -> None:
    assert retry.attempt_cap("future-notice") == 5
    assert retry.retry_schedule("future-notice", NOW)(1) == NOW + 60


@pytest.mark.parametrize("kind", ["slack-notice", "wsl-notice"])
def test_the_delays_grow_and_a_due_time_is_never_in_the_past(kind: str) -> None:
    at = retry.retry_schedule(kind, NOW)
    steps = [at(a) for a in range(1, retry.attempt_cap(kind))]
    assert all(s is not None and s > NOW for s in steps)
    assert steps == sorted(steps)  # type: ignore[type-var]


def test_the_due_times_are_absolute_and_follow_the_now_given() -> None:
    assert retry.retry_schedule("slack-notice", 0)(1) == 60
    assert retry.retry_schedule("slack-notice", NOW + 5)(2) == NOW + 5 + 300
    huge = 2**62
    assert retry.retry_schedule("slack-notice", huge)(4) == huge + 7200


def test_the_schedule_is_pure_no_state_no_jitter() -> None:
    at = retry.retry_schedule("slack-notice", NOW)
    assert [at(2) for _ in range(5)] == [NOW + 300] * 5
    assert retry.retry_schedule("slack-notice", NOW)(2) == at(2)


@pytest.mark.parametrize(
    "bad", [0, -1, -(10**30), True, False, 1.0, 2.5, math.nan, math.inf, "1", None, b"1"]
)
def test_a_bad_attempt_count_is_refused(bad: Any) -> None:
    at = retry.retry_schedule("slack-notice", NOW)
    with pytest.raises((TypeError, ValueError)):
        at(bad)


@pytest.mark.parametrize("bad", [True, 1.5, math.nan, "1", None, -1])
def test_a_bad_clock_value_is_refused(bad: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        retry.retry_schedule("slack-notice", bad)


@pytest.mark.parametrize("bad", [None, 5, b"slack-notice", ["slack-notice"]])
def test_a_bad_kind_is_refused(bad: Any) -> None:
    with pytest.raises(TypeError):
        retry.retry_schedule(bad, NOW)
    with pytest.raises(TypeError):
        retry.attempt_cap(bad)
