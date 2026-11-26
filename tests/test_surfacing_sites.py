"""Where a failure is surfaced (#27): every site of docs/interface.md (Hooks, Surfacing), through
the real entry point with fake notifiers and a real state directory."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from notice_route import cli
from notice_route import flush as flush_mod
from notice_route.gateway import SYSTEM_TZ_DIRS
from notice_route.notify import CallResult
from notice_route.send import Seams
from tests.alerts_support import DAY, HOUR, Setup
from tests.flush_support import flush, seed
from tests.send_support import NOW, Rig

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = [
    pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database"),
    pytest.mark.usefixtures("send_environment"),
]

DAY0 = "2023-11-14"
HOUR0 = "2023-11-14T22"


def use_flush_seams(monkeypatch: pytest.MonkeyPatch, seams: Seams) -> None:
    real = cli.run_flush
    monkeypatch.setattr(cli, "run_flush", lambda *a, **k: real(*a, **{**k, "seams": seams}))


def use_send_seams(monkeypatch: pytest.MonkeyPatch, seams: Seams) -> None:
    from notice_route import send

    real = send.run_send
    monkeypatch.setattr(cli, "run_send", lambda *a, **k: real(*a, **{**k, "seams": seams}))


# --- failed and unknown ----------------------------------------------------------------------


def test_a_failed_send_surfaces_once_per_code_and_day(rig: Rig) -> None:
    s = Setup(rig, rules=[{"name": "pt", "match": {"level_min": "info"}, "notify": ["chat"]}])
    code, doc, _ = s.fail("one")
    assert code == 12 and doc["destinations"][0]["code"] == "credential_invalid"
    assert s.surfaced() == [("chat", "credential_invalid", DAY0, 1, 0)]
    assert rig.query("SELECT id, surfaced FROM deliveries ORDER BY id") == [(1, 1)]
    s.fail("two")
    assert s.surfaced() == [("chat", "credential_invalid", DAY0, 1, 0)]
    assert rig.query("SELECT id, surfaced FROM deliveries ORDER BY id") == [(1, 1), (2, 0)]
    _, out, _ = rig.run("history", "--json")
    rows = json.loads(out)
    assert {r["id"]: r["surfaced"] for r in rows} == {1: True, 2: False}


def test_an_unknown_end_is_surfaced(rig: Rig) -> None:
    s = Setup(
        rig,
        "outcome_unknown",
        rules=[{"name": "pt", "match": {"level_min": "info"}, "notify": ["chat"]}],
    )
    s.behave("outcome_unknown", unknown=True)
    code, doc, _ = s.fail("one")
    assert doc["destinations"][0]["outcome"] == "unknown" and code == 12
    assert s.surfaced() == [("chat", "outcome_unknown", DAY0, 1, 0)]


def test_a_queued_retry_is_not_surfaced(rig: Rig) -> None:
    s = Setup(rig)
    s.behave("rate_limited", retryable=True)
    s.fail("one")
    assert s.surfaced() == []


def test_a_notifier_that_sends_a_router_word_cannot_use_its_hourly_slot(rig: Rig) -> None:
    s = Setup(rig, "expired")
    s.fail("one")
    assert s.surfaced() == [("chat", "other", DAY0, 1, 0)]


def test_day_boundary_is_one_second(rig: Rig) -> None:
    s = Setup(rig)
    rig.clock._wall = DAY * (NOW // DAY + 1) - 1  # type: ignore[attr-defined]
    s.fail("one")
    s.fail("two")
    rig.clock.jump_wall(1)
    s.fail("three")
    assert [(r[1], r[2]) for r in s.surfaced()] == [
        ("credential_invalid", "2023-11-14"),
        ("credential_invalid", "2023-11-15"),
    ]


def test_the_ninth_distinct_code_of_a_day_is_other_once(rig: Rig) -> None:
    s = Setup(rig)
    for i in range(10):
        s.behave(f"fault_{i}")
        s.fail(f"m{i}")
    codes = [r[1] for r in s.surfaced()]
    assert codes == [f"fault_{i}" for i in range(8)] + ["other"]


def test_an_escaped_call_is_surfaced_unknown_killed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Setup(rig)

    def boom(*_a: Any, **_k: Any) -> CallResult:
        raise RuntimeError("BOOM-TEXT")

    use_send_seams(monkeypatch, Seams(call=boom))
    code, doc, _ = s.fail("one")
    assert code == 12 and doc["destinations"][0]["code"] == "killed"
    assert s.surfaced() == [("chat", "killed", DAY0, 1, 0)]
    assert "BOOM-TEXT" not in rig.state_text()


def test_a_flush_that_ends_a_row_failed_surfaces_it(rig: Rig) -> None:
    s = Setup(rig)
    seed(rig, "chat", attempts=5, outcome="unknown", claim="old", claim_expires=NOW - 1)
    flush(rig)
    assert s.surfaced() == [("chat", "retries_exhausted", DAY0, 1, 0)]
    flush(rig)  # the next flush takes it
    assert s.surfaced() == [("chat", "retries_exhausted", DAY0, 1, 1)]


def test_a_flush_call_that_fails_is_surfaced(rig: Rig) -> None:
    s = Setup(rig)
    seed(rig, "chat")
    code, _, _ = flush(rig, "--destination", "chat")
    assert code == 12
    assert s.surfaced()[0][:3] == ("chat", "credential_invalid", DAY0)


def test_an_escaped_flush_call_is_surfaced(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Setup(rig)
    seed(rig, "chat")

    def boom(*_a: Any, **_k: Any) -> CallResult:
        raise RuntimeError("BOOM")

    use_flush_seams(monkeypatch, Seams(call=boom))
    flush(rig, "--destination", "chat")
    assert s.surfaced() == [("chat", "killed", DAY0, 1, 0)]


def test_a_digest_that_fails_surfaces_once_for_its_first_member(rig: Rig) -> None:
    s = Setup(rig)
    d1, _ = seed(rig, "chat", kind="held", level="warn", message="a")
    d2, _ = seed(rig, "chat", kind="held", level="error", message="b")
    d3, _ = seed(rig, "chat", kind="held", level="warn", message="c")
    flush(rig, "--destination", "chat")
    # the highest level first: the error row
    assert s.surfaced() == [("chat", "credential_invalid", DAY0, d2, 0)]
    assert rig.query("SELECT id FROM deliveries WHERE surfaced = 1") == [(d2,)]
    assert (d1, d3) == (1, 3)


def test_an_escaped_digest_is_surfaced_once(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Setup(rig)
    seed(rig, "chat", kind="held", level="warn", message="a")
    d2, _ = seed(rig, "chat", kind="held", level="error", message="b")

    def boom(*_a: Any, **_k: Any) -> CallResult:
        raise RuntimeError("BOOM")

    use_flush_seams(monkeypatch, Seams(call=boom))
    flush(rig, "--destination", "chat")
    assert s.surfaced() == [("chat", "killed", DAY0, d2, 0)]


# --- env_missing, expired, overflow, rate_capped ---------------------------------------------


def test_flush_surfaces_env_missing_once_per_hour(rig: Rig) -> None:
    s = Setup(rig, destinations={"chat": {"notifier": rig.notifier("chat"), "env": ["CRED_X"]}})
    d, _ = seed(rig, "chat")
    code, doc, _ = flush(rig, env={})
    assert doc["destinations"][0]["state"] == "env_missing"
    flush(rig, env={})
    assert s.surfaced()[0][:4] == ("chat", "env_missing", HOUR0, d)
    assert len(s.surfaced()) == 1
    rig.clock.jump_wall(HOUR)
    flush(rig, env={})
    assert [r[2] for r in s.surfaced()] == [HOUR0, "2023-11-14T23"]


def test_an_idle_destination_without_its_variable_surfaces_nothing(rig: Rig) -> None:
    s = Setup(rig, destinations={"chat": {"notifier": rig.notifier("chat"), "env": ["CRED_X"]}})
    flush(rig, env={})
    assert s.surfaced() == []


def test_flush_surfaces_expired_rows_once_per_hour(rig: Rig) -> None:
    s = Setup(rig)
    old = NOW - DAY - 5
    first, _ = seed(rig, "chat", created_at=old, now=old)
    seed(rig, "chat", created_at=old, now=old)
    flush(rig, "--destination", "chat")
    assert s.surfaced()[0][:4] == ("chat", "expired", HOUR0, first)
    assert len(s.surfaced()) == 1
    assert rig.query("SELECT COUNT(*) FROM deliveries WHERE surfaced = 1") == [(1,)]


def test_prune_surfaces_expired_rows_of_an_enabled_destination_only(rig: Rig) -> None:
    s = Setup(rig, destinations={"off": {"notifier": rig.notifier("off"), "enabled": False}})
    old = NOW - DAY - 5
    a, _ = seed(rig, "chat", created_at=old, now=old)
    seed(rig, "off", created_at=old, now=old)
    seed(rig, "gone", created_at=old, now=old)
    code, _, _ = rig.run("prune", "--json")
    assert code == 0
    assert s.surfaced() == [("chat", "expired", HOUR0, a, 0)]


def test_an_overflow_eviction_is_surfaced_once_per_hour(rig: Rig) -> None:
    s = Setup(rig, rules=[{"name": "pt", "match": {"level_min": "info"}, "notify": ["chat"]}])
    s.behave("rate_limited", retryable=True)
    for i in range(100):
        seed(rig, "chat", level="warn", deliver_after=NOW + 10_000, message=f"q{i}")
    s.fail("one")  # a retry that goes back to the full queue evicts the lowest row
    assert [(r[1], r[2]) for r in s.surfaced()] == [("overflow", HOUR0)]
    rig.query("SELECT 1")
    assert rig.query("SELECT COUNT(*) FROM queue") == [(100,)]


def test_rate_capped_still_surfaces_once_per_hour(rig: Rig) -> None:
    s = Setup(rig, destinations={"chat": {"notifier": rig.notifier("chat"), "max_per_hour": 1}})
    s.fail("one")
    s.fail("two")
    s.fail("three")
    rows = [r for r in s.surfaced() if r[1] == "rate_capped"]
    assert [(r[2]) for r in rows] == [HOUR0] and rows[0][3] == 2


def test_hourly_boundary_is_one_second(rig: Rig) -> None:
    s = Setup(rig, destinations={"chat": {"notifier": rig.notifier("chat"), "max_per_hour": 1}})
    rig.clock._wall = NOW - NOW % HOUR + HOUR - 1  # type: ignore[attr-defined]
    s.fail("one")
    s.fail("two")
    s.fail("three")
    rig.clock.jump_wall(1)
    s.fail("four")
    s.fail("five")
    assert sorted(r[2] for r in s.surfaced() if r[1] == "rate_capped") == [
        "2023-11-14T22", "2023-11-14T23",
    ]  # fmt: skip


def test_failures_in_a_transaction_that_rolls_back_leave_no_row(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Setup(rig)

    def surface_then_fail(conn: sqlite3.Connection, event: Any) -> None:
        from notice_route.send import surface_event

        surface_event(conn, event)
        raise RuntimeError("after the insert")

    use_send_seams(monkeypatch, Seams(surface=surface_then_fail))
    code, _, _ = s.fail("one")
    assert code == 1 and s.surfaced() == []
    assert rig.query("SELECT outcome, surfaced FROM deliveries") == [("sending", 0)]


_ = flush_mod
