"""The ``post-fail`` hook (#27): environment, payload, timeouts, recursion, failure isolation."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from notice_route.gateway import SYSTEM_TZ_DIRS
from tests.alerts_support import ALERT_RULE, SEND_RULE, MakeHook, Setup, hook_errors, hooks_config
from tests.fakes import alive
from tests.flush_support import flush
from tests.send_support import CANARY, Rig

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = [
    pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database"),
    pytest.mark.usefixtures("send_environment"),
]


def with_hooks(rig: Rig, *hooks: Any, **kw: Any) -> Setup:
    return Setup(rig, **hooks_config(*hooks, **kw))


def test_the_hook_gets_the_payload_on_stdin_and_the_documented_environment(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook()
    s = with_hooks(rig, h)
    s.fail("boom", extra=("--label", "team=a"))
    code, _, err = s.flush()
    assert (code, err) == (0, "")
    (run,) = h.runs()
    payload = json.loads(run["stdin"])
    assert hook_errors(payload) == []
    assert payload == {
        "tool": "notice-route", "hook": "post-fail", "object_type": "destination",
        "object_id": "chat", "event_id": 1, "labels": ["team=a"], "code": "credential_invalid",
        "outcome": "failed", "attempts": 1, "notification_source": "s",
        "notification_level": "info", "kind": "slack-notice",
    }  # fmt: skip
    shell_added = {"PWD", "SHLVL", "_", "OLDPWD"}  # what sh itself puts in its children's env
    assert set(run["env"]) - shell_added == {
        "PATH", "LANG", "LC_ALL", "NOTICE_ROUTE_HOOK", "NOTICE_ROUTE_ID",
        "NOTICE_ROUTE_OBJECT_TYPE",
    }  # fmt: skip
    assert run["env"]["NOTICE_ROUTE_HOOK"] == "post-fail"
    assert run["env"]["NOTICE_ROUTE_ID"] == "chat"
    assert run["env"]["NOTICE_ROUTE_OBJECT_TYPE"] == "destination"
    assert run["argv"] == []  # nothing of the payload on the command line
    assert run["cwd"] == "/" and set(run["fds"]) <= {0, 1, 2, 3}
    assert run["pgid"] == run["pid"] or run["sid"] == run["pgid"]  # a group of its own
    assert "credential_invalid" not in json.dumps(run["env"])


def test_the_environment_of_the_router_does_not_reach_the_hook(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook()
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush(env={"AWS_SECRET_THING": CANARY, "OPS_CRED": CANARY, "PATH": "/usr/bin:/bin"})
    assert CANARY not in json.dumps(h.runs()[0]["env"])


def test_hook_env_is_exactly_the_allow_list(rig: Rig) -> None:
    from notice_route.hooks import hook_env

    gw = rig.gateway(env={"PATH": "/usr/bin:/bin", "LANG": "de_DE.ISO-8859-1", "HOME": "/x",
                          "NOTICE_ROUTE_ID": "forged", "OPS_CRED": "v"})  # fmt: skip
    assert hook_env(gw, "chat") == {
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "NOTICE_ROUTE_HOOK": "post-fail", "NOTICE_ROUTE_ID": "chat",
        "NOTICE_ROUTE_OBJECT_TYPE": "destination",
    }  # fmt: skip


def test_each_surfaced_row_runs_each_hook_once(rig: Rig, make_hook: MakeHook) -> None:
    a, b = make_hook("a"), make_hook("b")
    s = with_hooks(rig, a, b)
    s.fail("one")
    s.behave("other_fault")
    s.fail("two")
    s.flush()
    s.flush()
    assert [len(x.runs()) for x in (a, b)] == [2, 2]
    assert sorted(json.loads(r["stdin"])["code"] for r in a.runs()) == [
        "credential_invalid", "other_fault",
    ]  # fmt: skip


def test_no_hook_configured_runs_nothing_and_still_takes_the_row(rig: Rig) -> None:
    s = Setup(rig)
    s.fail("boom")
    assert s.flush()[0] == 0 and s.surfaced()[0][4] == 1


def test_a_hook_for_an_alerts_own_failure_runs_but_no_alert_is_routed(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook()
    s = with_hooks(rig, h)
    s.ops.set(
        exit=12,
        stdout=json.dumps(
            {"error": {"code": "credential_invalid", "retryable": False, "unknown_outcome": False}}
        ),
    )
    s.fail("boom")
    s.flush()
    s.flush()
    sources = [json.loads(r["stdin"])["notification_source"] for r in h.runs()]
    assert sources == ["s", "notice-route"] and len(s.alerts()) == 1


def test_a_failing_hook_never_changes_the_result(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    code, doc, err = s.flush()
    assert code == 0 and doc["outcome"] == "ok"
    assert err.count("a post-fail hook failed (nonzero_exit)") == 1
    assert len(s.ops.events("invoke")) == 1  # the alert was delivered regardless


def test_a_hook_that_cannot_be_started_is_a_warning_only(rig: Rig) -> None:
    s = Setup(rig, hooks={"post-fail": [{"cmd": "/nonexistent/hook-command"}]})
    s.fail("boom")
    code, _, err = s.flush()
    assert code == 0 and "a post-fail hook failed (nonzero_exit)" in err  # sh: not found, 127


def test_a_hook_is_killed_at_its_timeout_and_the_result_is_unchanged(
    rig: Rig, make_hook: MakeHook
) -> None:
    rig.clock.rate = 50.0  # one second of hook time is 20 ms
    h = make_hook(sleep=25, ignore_term=True, grandchild={"lifetime": 25, "hold": True})
    s = with_hooks(rig, h, timeout="2s")
    s.fail("boom")
    t0 = time.monotonic()
    code, _, err = s.flush()
    assert code == 0 and "a post-fail hook failed (timeout)" in err
    assert time.monotonic() - t0 < 10
    run = h.wait_for("run")
    assert not alive(run["pid"], run["start"])  # the TERM-ignoring hook was SIGKILLed
    assert "exit" not in [r["event"] for r in h.records()]
    assert len(s.ops.events("invoke")) == 1


def test_a_hook_with_too_much_output_is_killed(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook(stdout_bytes=300_000, sleep=20)
    s = with_hooks(rig, h)
    s.fail("boom")
    code, _, err = s.flush()
    assert code == 0 and "a post-fail hook failed (overflow)" in err
    assert not alive(h.runs()[0]["pid"], h.runs()[0]["start"])


def test_a_setsid_grandchild_holding_the_pipes_does_not_hold_flush(
    rig: Rig, make_hook: MakeHook
) -> None:
    rig.clock.rate = 20.0
    h = make_hook(grandchild={"lifetime": 25, "hold": True})
    s = with_hooks(rig, h)
    s.fail("boom")
    t0 = time.monotonic()
    code, _, _ = s.flush()
    assert code == 0 and time.monotonic() - t0 < 10


def test_a_hook_that_calls_send_or_flush_is_refused_with_hook_recursion(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(call_router=["send", "--stdin-json", "--json"])
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    assert h.wait_for("router")["exit"] == 20
    h.set(call_router=["flush", "--json"])
    s.fail("again")
    s.behave("another_fault")
    s.fail("third")
    s.flush()
    assert h.wait_for("router", count=2)["exit"] == 20
    assert rig.query("SELECT COUNT(*) FROM deliveries") == [(5,)]  # the hook added nothing


def test_the_recursion_guard_applies_to_a_flush_started_in_the_router_environment(
    rig: Rig,
) -> None:
    Setup(rig)
    code, out, _ = rig.run("flush", "--json", env={"NOTICE_ROUTE_HOOK": "post-fail"})
    assert code == 20 and json.loads(out)["error"]["reason"] == "hook_recursion"


def test_an_async_hook_is_not_waited_for(rig: Rig, make_hook: MakeHook, tmp_path: Path) -> None:
    marker = tmp_path / "done"
    h = make_hook(sleep=1.5, touch=str(marker))
    s = with_hooks(rig, h, is_async=True)
    s.fail("boom")
    proc = rig.spawn("flush", "--json")
    t0 = time.monotonic()
    proc.communicate(timeout=20)
    assert proc.returncode == 0
    assert not marker.exists() and time.monotonic() - t0 < 1.4  # flush ended first
    h.wait_for("exit", timeout=15)
    assert marker.exists()
    (run,) = h.runs()
    assert set(run["env"]) >= {"NOTICE_ROUTE_HOOK"} and hook_errors(json.loads(run["stdin"])) == []


def test_two_flushers_take_a_surfaced_row_once(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook(sleep=0.3)
    s = with_hooks(rig, h)
    s.fail("boom")
    first = rig.spawn("flush", "--json")
    second = rig.spawn("flush", "--json")
    for p in (first, second):
        p.communicate(timeout=30)
    assert len(h.runs()) == 1 and len(s.ops.events("invoke")) == 1 and len(s.alerts()) == 1


def test_two_senders_surface_the_same_period_once(rig: Rig) -> None:
    s = Setup(rig)
    body = json.dumps({"message": "x"}).encode()
    procs = [rig.spawn("send", "--stdin-json", "--json", "--source", "s", stdin=body) for _ in "ab"]
    for p in procs:
        p.communicate(timeout=30)
    assert len(s.surfaced()) == 1
    assert rig.query("SELECT COUNT(*) FROM deliveries WHERE surfaced = 1") == [(1,)]


def test_flush_in_a_dry_run_starts_no_hook(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook()
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush("--dry-run")
    assert h.runs() == []
    _ = flush, ALERT_RULE, SEND_RULE
