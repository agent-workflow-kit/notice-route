"""``send`` end to end through the real CLI entry with fake notifiers (#22): every outcome,
exit code and output rule of docs/interface.md (send, Output, Exit codes, Log)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from notice_route.gateway import SYSTEM_TZ_DIRS
from tests.fakes import Notifier
from tests.send_support import MORNING, NOW, Rig

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database")


@pytest.fixture(autouse=True)
def private_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.delenv("NOTICE_ROUTE_HOOK", raising=False)


@pytest.fixture
def rig(
    home: Path,
    tmp_path: Path,
    make_notifier: Callable[..., Notifier],
    capsys: pytest.CaptureFixture[str],
) -> Iterator[Rig]:
    r = Rig(home, tmp_path, make_notifier, capsys)
    yield r
    r.finish()


def only(doc: dict[str, Any]) -> dict[str, Any]:
    assert len(doc["destinations"]) == 1
    return doc["destinations"][0]


def test_sent(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}})
    code, doc, err = rig.send("hello", level="warn")
    assert code == 0 and err == ""
    d = only(doc)
    assert (d["name"], d["outcome"], d["attempts"], d["code"]) == ("chat", "sent", 1, None)
    assert doc["outcome"] == "ok" and doc["reason"] is None and doc["level"] == "warn"
    assert d["notifier_outcome"] == "sent"
    invoke = n.events("invoke")
    assert len(invoke) == 1
    assert invoke[0]["argv"][-6:] == [
        "send", "--stdin-json", "--json", "--actor", "auto:notice-route",
        f"--dedupe-key={doc['dedupe']}",
    ]  # fmt: skip
    assert json.loads(n.events("stdin")[0]["text"]) == {"message": "hello", "level": "warn"}
    assert rig.query("SELECT outcome, attempts, code FROM deliveries") == [("sent", 1, None)]
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
    assert rig.query("SELECT count FROM rate") == [(1,)]


def test_queued_retry_has_a_due_time_and_an_attempt(rig: Rig) -> None:
    n = rig.notifier(error={"code": "rate_limited"})
    rig.configure({"chat": {"notifier": n}})
    code, doc, _ = rig.send("hello")
    d = only(doc)
    assert code == 0 and doc["outcome"] == "ok"
    assert (d["outcome"], d["code"], d["attempts"], d["deliver_after"]) == (
        "queued_retry", "rate_limited", 1, NOW + 60,
    )  # fmt: skip
    assert rig.query("SELECT kind, deliver_after, claimed_by FROM queue") == [
        ("single", NOW + 60, None)
    ]


def test_failed_is_partial_and_releases_the_dedup_records(rig: Rig) -> None:
    n = rig.notifier(error={"code": "credential_invalid"})
    rig.configure({"chat": {"notifier": n}})
    code, doc, _ = rig.send("hello")
    d = only(doc)
    assert code == 12 and doc["outcome"] == "partial"
    assert (d["outcome"], d["code"]) == ("failed", "credential_invalid")
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
    code, doc, _ = rig.send("hello")  # judged afresh: called again, not a repeat
    assert only(doc)["outcome"] == "failed" and len(n.events("invoke")) == 2


def test_unknown_keeps_the_records_so_a_repeat_is_suppressed(rig: Rig) -> None:
    n = rig.notifier(error={"code": "outcome_unknown"})
    rig.configure({"chat": {"notifier": n}})
    code, doc, _ = rig.send("hello")
    assert code == 12 and (only(doc)["outcome"], only(doc)["code"]) == (
        "unknown",
        "outcome_unknown",
    )
    code, doc, _ = rig.send("hello")
    d = only(doc)
    assert code == 0 and (d["outcome"], d["reason"], d["count"]) == ("suppressed", "repeat", 2)
    assert len(n.events("invoke")) == 1


def test_a_notifier_that_is_disabled_is_a_successful_no_op(rig: Rig) -> None:
    n = rig.notifier(disabled=True)
    rig.configure({"chat": {"notifier": n}})
    code, doc, _ = rig.send("hello")
    d = only(doc)
    assert code == 0 and doc["outcome"] == "ok" and doc["reason"] is None
    assert (d["outcome"], d["code"], d["notifier_outcome"]) == ("disabled", None, "disabled")


def test_env_missing_is_queued_due_now_and_not_called(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "env": ["ROUTE_CRED"]}})
    code, doc, err = rig.send("hello")
    d = only(doc)
    assert code == 0 and (d["outcome"], d["reason"], d["deliver_after"]) == (
        "deferred", "env_missing", NOW,
    )  # fmt: skip
    assert n.events("invoke") == []
    assert "no recent flush" in err
    assert rig.query("SELECT kind, deliver_after, claimed_by FROM queue") == [("single", NOW, None)]
    _, _, quiet_err = rig.send("other", extra=("--quiet",))
    assert quiet_err == ""


def test_a_named_variable_is_passed_and_never_printed_or_stored(rig: Rig) -> None:
    from tests.send_support import CANARY

    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "env": ["ROUTE_CRED"]}})
    code, doc, err = rig.send("hello", env={"ROUTE_CRED": CANARY})
    assert code == 0 and only(doc)["outcome"] == "sent"
    assert n.events("invoke")[0]["env"]["ROUTE_CRED"] == CANARY
    assert CANARY not in json.dumps(doc) + err + rig.state_text()


def test_quiet_window_per_level(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "quiet": "night"}})
    _, doc, _ = rig.send("a", level="info")
    assert (only(doc)["outcome"], only(doc)["reason"]) == ("dropped", "quiet")
    _, doc, _ = rig.send("b", level="warn")
    d = only(doc)
    assert (d["outcome"], d["reason"], d["deliver_after"]) == ("deferred", "quiet", MORNING)
    assert rig.query("SELECT kind FROM queue") == [("held",)]
    _, doc, _ = rig.send("c", level="error")
    assert only(doc)["outcome"] == "sent"
    assert len(n.events("invoke")) == 1


def test_below_floor_is_dropped(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "floor": "warn"}})
    code, doc, _ = rig.send("a", level="info")
    assert code == 0 and (only(doc)["outcome"], only(doc)["reason"]) == ("dropped", "below_floor")


def test_rate_capped_is_deferred_to_the_next_hour_and_surfaced(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "max_per_hour": 1}})
    assert rig.send("one")[1]["destinations"][0]["outcome"] == "sent"
    code, doc, _ = rig.send("two")
    d = only(doc)
    assert code == 0 and (d["outcome"], d["reason"], d["deliver_after"]) == (
        "deferred", "rate_capped", NOW - NOW % 3600 + 3600,
    )  # fmt: skip
    assert rig.query("SELECT code FROM surfaced") == [("rate_capped",)]
    assert len(n.events("invoke")) == 1


def test_a_producer_redelivery_is_suppressed_as_redelivered(rig: Rig) -> None:
    n = rig.notifier()
    rule = {
        "name": "h",
        "match": {"source": "agent-spawn"},
        "notify": ["chat"],
        "level": "warn",
        "message": "state {{state}}",
    }
    rig.configure({"chat": {"notifier": n}}, rules=[rule])
    body = json.dumps(
        {"tool": "agent-spawn", "hook": "post-state-change", "event_id": 17, "state": "done"}
    ).encode()
    for expected in ("sent", "suppressed"):
        code, out, _ = rig.run("send", "--stdin-json", "--json", stdin=body)
        d = json.loads(out)["destinations"][0]
        assert code == 0 and d["outcome"] == expected
    assert (d["reason"], d["count"]) == ("redelivered", None)
    assert len(n.events("invoke")) == 1


RULES_DROP = [
    {"name": "quiet-source", "match": {"source": "noisy"}, "drop": True},
    {"name": "only", "match": {"source": "s"}, "notify": ["chat"], "level": "info", "message": "x"},
]


def test_a_drop_rule_and_no_rule_write_history_and_a_log_line(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}}, rules=RULES_DROP)
    code, doc, _ = rig.send("a", source="noisy")
    assert code == 0 and doc["outcome"] == "ok" and doc["reason"] == "rule_drop"
    assert doc["rule"] == "quiet-source" and doc["destinations"] == [] and doc["dedupe"] is None
    code, doc, _ = rig.send("a", source="stranger")
    assert code == 22 and doc["outcome"] == "no_op" and doc["reason"] == "no_rule"
    assert doc["rule"] is None and doc["level"] is None
    assert rig.query("SELECT rule, destination, outcome, reason FROM deliveries ORDER BY id") == [
        ("quiet-source", None, "dropped", "rule_drop"),
        (None, None, "dropped", "no_rule"),
    ]
    lines = rig.log_lines()
    assert [(x["destination"], x["reason"], x["outcome"]) for x in lines] == [
        (None, "rule_drop", "dropped"),
        (None, "no_rule", "dropped"),
    ]
    assert n.events("invoke") == []


def test_unconfigured_and_disabled_write_no_state(rig: Rig, home: Path) -> None:
    code, doc, err = rig.send("a")
    assert (code, doc["outcome"], doc["reason"]) == (22, "no_op", "unconfigured")
    assert "no config" in err and doc["destinations"] == []
    assert not (home / ".local").exists()
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}}, enabled=False)
    code, doc, _ = rig.send("a")
    assert (code, doc["outcome"], doc["reason"]) == (22, "no_op", "disabled")
    assert not (home / ".local").exists()


def test_all_disabled_is_exit_22_after_the_decision(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "enabled": False}})
    code, doc, _ = rig.send("a")
    assert (code, doc["outcome"], doc["reason"]) == (22, "no_op", "all_disabled")
    assert only(doc)["outcome"] == "disabled" and doc["dedupe"] is not None
    assert rig.query("SELECT outcome, destination FROM deliveries") == [("disabled", "chat")]
    assert n.events("invoke") == []


def test_several_destinations_with_a_partial_failure(rig: Rig) -> None:
    good, bad = rig.notifier("good"), rig.notifier("bad", error={"code": "credential_invalid"})
    rig.configure(
        {
            "a": {"notifier": good},
            "b": {"notifier": bad},
            "c": {"notifier": good, "quiet": "night"},
        }
    )
    code, doc, _ = rig.send("hi", level="warn")
    assert code == 12 and doc["outcome"] == "partial"
    got = [(d["name"], d["outcome"], d["code"] or d["reason"]) for d in doc["destinations"]]
    assert got == [
        ("a", "sent", None),
        ("b", "failed", "credential_invalid"),
        ("c", "deferred", "quiet"),
    ]
    assert len(good.events("invoke")) == 1 and len(bad.events("invoke")) == 1


def test_calls_to_different_destinations_run_in_parallel(rig: Rig) -> None:
    a, b = rig.notifier("a", sleep=0.8), rig.notifier("b", sleep=0.8)
    rig.configure({"a": {"notifier": a}, "b": {"notifier": b}})
    code, doc, _ = rig.send("hi")
    assert code == 0 and [d["outcome"] for d in doc["destinations"]] == ["sent", "sent"]
    assert doc["duration_ms"] < 1500  # two sequential calls would take 1.6 s and more
