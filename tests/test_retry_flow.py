"""The retry schedule end to end (#25): the real entry points, fake notifiers, a real state
directory and a settable wall clock (``rig.clock.jump_wall``); nothing waits for real time."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from notice_route import retry, send
from notice_route.gateway import SYSTEM_TZ_DIRS
from tests.flush_support import dest, flush, seed
from tests.send_support import MORNING, NOW, Rig
from tests.test_flush import hold

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = [
    pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database"),
    pytest.mark.usefixtures("send_environment"),
]

DAY = 86400
STEPS = [60, 300, 1800, 7200]


def row(rig: Rig) -> tuple[Any, ...]:
    return rig.query("SELECT outcome, reason, code, attempts, deliver_after FROM deliveries")[0]


def run_to_the_end(rig: Rig, kind: str, code: str, cap: int) -> list[int]:
    """Send once, then flush at each due time; returns the ``deliver_after`` after each call."""
    n = rig.notifier(kind, error={"code": code})
    rig.configure({"chat": {"notifier": n, "kind": kind}})
    _, doc, _ = rig.send("hello")
    seen = [doc["destinations"][0]["deliver_after"]]
    for attempt in range(2, cap + 1):
        due = seen[-1]
        rig.clock.jump_wall(due - rig.clock.wall() - 1)  # one second early: nothing is due
        _, early, _ = flush(rig)
        assert dest(early)["state"] == "nothing_due" and len(n.events("invoke")) == attempt - 1
        rig.clock.jump_wall(1)
        flush(rig)
        assert len(n.events("invoke")) == attempt
        if attempt < cap:
            outcome, _, _, attempts, after = row(rig)
            assert (outcome, attempts) == ("queued_retry", attempt)
            seen.append(after)
    return seen


@pytest.mark.parametrize("code", ["rate_limited", "unavailable"])
def test_a_failing_notifier_is_retried_on_the_schedule_and_ends_failed_at_the_cap(
    rig: Rig, code: str
) -> None:
    seen = run_to_the_end(rig, "slack-notice", code, 5)
    assert seen == [NOW + 60, NOW + 60 + 300, NOW + 60 + 300 + 1800, NOW + 60 + 300 + 1800 + 7200]
    outcome, reason, end_code, attempts, after = row(rig)
    assert (outcome, end_code, attempts) == ("failed", "retries_exhausted", 5)
    assert after is None and rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
    assert rig.query("SELECT COUNT(*) FROM dedup") == [(0,)]  # released: a resend is not suppressed
    assert rig.query("SELECT COUNT(*) FROM event_ids") == [(0,)]


def test_a_wsl_notice_destination_gets_three_attempts(rig: Rig) -> None:
    seen = run_to_the_end(rig, "wsl-notice", "unavailable", 3)
    assert seen == [NOW + 60, NOW + 60 + 300]
    assert row(rig)[:4] == ("failed", None, "retries_exhausted", 3)
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]


def test_the_failed_end_is_not_retried_by_a_later_flush(rig: Rig) -> None:
    run_to_the_end(rig, "wsl-notice", "unavailable", 3)
    rig.clock.jump_wall(10 * DAY)
    code, doc, _ = flush(rig)
    assert dest(doc)["state"] == "nothing_due"
    assert rig.query("SELECT COUNT(*) FROM deliveries") == [(1,)]


def test_the_default_schedule_is_the_real_one_and_the_interim_copy_is_gone() -> None:
    assert send.Seams().retry_schedule is retry.retry_schedule
    assert not hasattr(send, "interim_retry_schedule")


# --- calls that count no attempt --------------------------------------------------------------


def test_lock_busy_counts_no_attempt_and_the_schedule_resumes_where_it_was(rig: Rig) -> None:
    n = rig.notifier(error={"code": "rate_limited"})
    rig.configure({"chat": {"notifier": n}}, lock_wait="1s")
    seed(rig, attempts=2, outcome="queued_retry")
    held = hold(rig, "chat.lock")
    try:
        flush(rig)
    finally:
        held.close()
    assert row(rig) == ("queued_retry", "lock_busy", None, 2, NOW + 60)  # not the 30 minute step
    assert n.events("invoke") == []
    rig.clock.jump_wall(60)
    flush(rig)
    assert row(rig)[3] == 3 and row(rig)[4] == NOW + 60 + 1800  # the third attempt's step


def test_lock_busy_in_a_row_never_reaches_the_cap(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}}, lock_wait="1s")
    seed(rig, attempts=4, outcome="queued_retry")  # one attempt left
    held = hold(rig, "chat.lock")
    try:
        for _ in range(7):  # more busy rounds than the cap allows attempts
            flush(rig)
            rig.clock.jump_wall(60)
    finally:
        held.close()
    assert row(rig)[:4] == ("queued_retry", "lock_busy", None, 4)
    flush(rig)
    assert row(rig)[0] == "sent"


def test_env_missing_at_send_waits_without_an_attempt_and_resumes_when_the_variable_appears(
    rig: Rig,
) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "env": ["ROUTE_CRED"]}})
    rig.send("hello")
    assert row(rig) == ("deferred", "env_missing", None, 0, NOW)
    for _ in range(3):
        rig.clock.jump_wall(3600)
        _, doc, _ = flush(rig)
        assert dest(doc)["state"] == "env_missing"
    assert row(rig)[3] == 0 and n.events("invoke") == []
    _, doc, _ = flush(rig, env={"ROUTE_CRED": "v"})
    assert (dest(doc)["state"], dest(doc)["sent"]) == ("done", 1)
    assert row(rig)[0] == "sent" and row(rig)[3] == 1


def test_a_retry_row_keeps_its_attempts_while_the_variable_is_unset(rig: Rig) -> None:
    n = rig.notifier(error={"code": "rate_limited"})
    rig.configure({"chat": {"notifier": n, "env": ["ROUTE_CRED"]}})
    seed(rig, attempts=3, outcome="queued_retry", deliver_after=NOW - 5)
    flush(rig)
    assert row(rig)[3] == 3 and n.events("invoke") == []
    flush(rig, env={"ROUTE_CRED": "v"})
    assert row(rig)[3] == 4 and row(rig)[4] == NOW + 7200  # the fourth attempt's step


def test_an_env_missing_row_past_24_hours_ends_expired_without_a_call(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "env": ["ROUTE_CRED"]}})
    rig.send("hello")
    rig.clock.jump_wall(DAY)
    _, doc, _ = flush(rig)
    assert dest(doc)["state"] == "env_missing" and row(rig)[:2] == ("deferred", "env_missing")
    _, doc, _ = flush(rig, env={"ROUTE_CRED": "v"})
    assert (dest(doc)["expired"], dest(doc)["sent"]) == (1, 0) and n.events("invoke") == []
    assert row(rig)[:4] == ("expired", "env_missing", "expired", 0)


# --- the 24 hour expiry against long delays ---------------------------------------------------


def test_a_retry_due_after_the_expiry_ends_expired_with_its_attempts_and_no_call(rig: Rig) -> None:
    n = rig.notifier(error={"code": "rate_limited"})
    rig.configure({"chat": {"notifier": n}})
    seed(
        rig, attempts=3, outcome="queued_retry", created_at=NOW - DAY + 100, deliver_after=NOW + 50
    )
    rig.clock.jump_wall(100)  # due, and now 24 hours old
    flush(rig)
    assert row(rig)[:4] == ("expired", None, "expired", 3) and n.events("invoke") == []


def test_the_longest_chain_of_delays_ends_before_the_expiry(rig: Rig) -> None:
    assert sum(retry.RETRY_DELAYS_S) < DAY  # a row never waits out its 24 hours between steps


# --- quiet hours and wall-clock steps ----------------------------------------------------------


def test_a_retry_due_in_a_quiet_window_waits_for_the_window_end_with_its_attempts(rig: Rig) -> None:
    n = rig.notifier(error={"code": "rate_limited"})
    rig.configure({"chat": {"notifier": n, "quiet": "night"}})
    seed(rig, level="warn", attempts=2, outcome="queued_retry", deliver_after=NOW - 1)
    flush(rig)
    assert row(rig)[3:] == (2, MORNING) and n.events("invoke") == []
    rig.clock.jump_wall(MORNING - NOW)
    flush(rig)
    assert row(rig)[3:] == (3, MORNING + 1800)  # from the end of the call, not from the window


def test_a_backward_clock_step_waits_and_a_forward_step_makes_the_row_due(rig: Rig) -> None:
    n = rig.notifier(error={"code": "rate_limited"})
    rig.configure({"chat": {"notifier": n}})
    rig.send("hello")
    assert row(rig)[4] == NOW + 60
    rig.clock.jump_wall(-3600)
    _, doc, _ = flush(rig)
    assert dest(doc)["state"] == "nothing_due" and len(n.events("invoke")) == 1
    rig.clock.jump_wall(3600 + 5 * DAY)  # forward past due and past the 24 hours
    flush(rig)
    assert row(rig)[:3] == ("expired", None, "expired")


def test_the_due_time_follows_the_wall_clock_after_a_step(rig: Rig) -> None:
    n = rig.notifier(error={"code": "rate_limited"})
    rig.configure({"chat": {"notifier": n}})
    seed(rig, attempts=1, outcome="queued_retry", deliver_after=NOW - 1)
    rig.clock.jump_wall(-1000)
    flush(rig)  # the wall clock is now behind the stored due time: the row waits
    assert len(n.events("invoke")) == 0
    rig.clock.jump_wall(1000)
    flush(rig)
    assert row(rig)[3:] == (2, NOW + 300)


# --- rows whose call escaped again and again --------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "cap"), [("slack-notice", 5), ("ha-notice", 5), ("generic", 5), ("wsl-notice", 3)]
)
def test_an_escaped_row_at_the_cap_ends_failed_without_a_call_and_one_below_is_resent(
    rig: Rig, kind: str, cap: int
) -> None:
    n = rig.notifier(kind)
    rig.configure({"chat": {"notifier": n, "kind": kind}})
    seed(rig, attempts=cap, outcome="unknown", claim="old", claim_expires=NOW - 1)
    seed(rig, attempts=cap - 1, outcome="unknown", claim="old", claim_expires=NOW - 1)
    flush(rig)
    got = rig.query("SELECT id, outcome, code, attempts FROM deliveries ORDER BY id")
    assert got[0] == (1, "failed", "retries_exhausted", cap)
    assert got[1][1] == "sent" and got[1][3] == cap
    assert len(n.events("invoke")) == 1


# --- digests ----------------------------------------------------------------------------------


def test_a_wsl_digest_exhausts_members_at_three_and_retries_the_rest(rig: Rig) -> None:
    n = rig.notifier("wsl-notice", error={"code": "rate_limited"})
    rig.configure({"chat": {"notifier": n, "kind": "wsl-notice"}})
    seed(rig, kind="held", attempts=2, outcome="queued_retry")  # 3rd call: used up
    seed(rig, kind="held", attempts=0)  # 1st call: next step 60
    flush(rig)
    assert rig.query("SELECT outcome, code, attempts FROM deliveries ORDER BY id") == [
        ("failed", "retries_exhausted", 3),
        ("queued_retry", "rate_limited", 1),
    ]
    assert rig.query("SELECT deliver_after FROM queue") == [(NOW + 60,)]
