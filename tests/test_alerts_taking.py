"""Taking surfaced rows (#27): partial progress, a faulty row, and which queue row is cited."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from notice_route import alerts, surfacing
from notice_route.gateway import SYSTEM_TZ_DIRS
from notice_route.store import StoreError, open_store
from tests.alerts_support import DAY, SEND_RULE, MakeHook, Setup, hooks_config
from tests.flush_support import flush, seed
from tests.send_support import NOW, Rig

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = [
    pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database"),
    pytest.mark.usefixtures("send_environment"),
]


def five_failures(s: Setup) -> None:
    for i in range(5):
        s.behave(f"fault_{i}")
        s.fail(f"m{i}")


def counting_route(monkeypatch: pytest.MonkeyPatch, fail_on: int, error: Exception) -> None:
    real = alerts._route  # pyright: ignore[reportPrivateUsage]
    calls: list[int] = []

    def route(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) == fail_on:
            raise error
        real(*args, **kwargs)

    monkeypatch.setattr(alerts, "_route", route)


def test_a_store_error_on_the_third_row_keeps_the_jobs_of_the_two_committed(
    rig: Rig, make_hook: MakeHook, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_hook()
    s = Setup(rig, **hooks_config(h))
    five_failures(s)
    counting_route(monkeypatch, 3, StoreError("busy"))
    code, _, _ = s.flush()
    assert code == 1  # the state database could not be written
    assert [r[4] for r in s.surfaced()] == [1, 1, 0, 0, 0]
    assert sorted(json.loads(r["stdin"])["code"] for r in h.runs()) == ["fault_0", "fault_1"]
    assert len(s.ops.events("invoke")) == 2  # their alerts were delivered and logged too


def test_a_fault_in_the_routing_of_a_row_does_not_block_the_later_rows(
    rig: Rig, make_hook: MakeHook, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_hook()
    s = Setup(rig, **hooks_config(h))
    five_failures(s)
    counting_route(monkeypatch, 2, RuntimeError("fault"))
    code, _, err = s.flush()
    assert code == 0  # the fault does not change the exit code
    assert err.count("a failure alert could not be routed") == 1  # one fixed-word warning
    assert "fault" not in err.replace("a failure alert could not be routed", "")
    assert [r[4] for r in s.surfaced()] == [1, 1, 1, 1, 1]
    assert len(h.runs()) == 5  # the faulty row's hook still ran
    assert len(s.ops.events("invoke")) == 4  # but it has no alert
    s.flush()
    assert len(h.runs()) == 5 and len(s.ops.events("invoke")) == 4  # nothing is retaken


def env_setup(rig: Rig) -> Setup:
    return Setup(rig, destinations={"chat": {"notifier": rig.notifier("chat"), "env": ["CRED_X"]}})


def test_env_missing_with_only_a_row_that_is_not_due_surfaces_nothing(rig: Rig) -> None:
    s = env_setup(rig)
    seed(rig, "chat", deliver_after=NOW + 10_000)
    flush(rig, env={})
    assert s.surfaced() == []


def test_env_missing_skips_a_claimed_row(rig: Rig) -> None:
    s = env_setup(rig)
    seed(rig, "chat", claim="tok", claim_expires=NOW + 5)
    flush(rig, env={})
    assert s.surfaced() == []


def test_env_missing_prefers_a_row_that_is_no_alert(rig: Rig) -> None:
    s = env_setup(rig)
    seed(rig, "chat", source="notice-route")
    normal, _ = seed(rig, "chat")
    flush(rig, env={})
    assert [(r[1], r[3]) for r in s.surfaced()] == [("env_missing", normal)]


def test_env_missing_falls_back_to_an_alert_row(rig: Rig) -> None:
    s = env_setup(rig)
    only, _ = seed(rig, "chat", source="notice-route")
    flush(rig, env={})
    assert [(r[1], r[3]) for r in s.surfaced()] == [("env_missing", only)]


def test_the_expired_row_cited_is_not_an_alert_when_another_exists(rig: Rig) -> None:
    s = Setup(rig)
    old = NOW - DAY - 5
    seed(rig, "chat", source="notice-route", created_at=old, now=old)
    normal, _ = seed(rig, "chat", created_at=old, now=old)
    flush(rig, "--destination", "chat")
    assert [(r[1], r[3]) for r in s.surfaced()] == [("expired", normal)]


def test_the_overflow_row_cited_is_not_an_alert_when_another_exists(rig: Rig) -> None:
    s = Setup(rig)
    alert, _ = seed(rig, "chat", source="notice-route")
    normal, _ = seed(rig, "chat")
    with open_store(rig.gateway()) as store, store.write() as conn:
        surfacing.surface_evicted(conn, "chat", [alert, normal], NOW)
    assert [(r[1], r[3]) for r in s.surfaced()] == [("overflow", normal)]


def test_an_unrouted_alert_row_is_in_history_and_is_pruned_like_any_other(rig: Rig) -> None:
    s = Setup(rig, rules=[SEND_RULE], retention={"history": "1d", "event_ids": "1d"})
    s.fail("boom")
    s.flush()
    _, out, _ = rig.run("history", "--json")
    rows = [r for r in json.loads(out) if r["source"] == "notice-route"]
    assert [(r["outcome"], r["reason"], r["surfaced"]) for r in rows] == [
        ("dropped", "no_rule", False)
    ]
    rig.clock.jump_wall(3 * DAY)
    code, _, _ = rig.run("prune", "--json")
    assert code == 0
    assert s.alerts() == []
