"""Failure alerts through the rules (#27): flush takes each surfaced row once, routes the
``post-fail`` payload as source ``notice-route`` and delivers it in the same run."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from notice_route.gateway import SYSTEM_TZ_DIRS
from tests.alerts_support import ALERT_RULE, DAY, HOUR, SEND_RULE, Setup
from tests.flush_support import checked_log, flush, seed
from tests.send_support import CANARY, NOW, Rig

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = [
    pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database"),
    pytest.mark.usefixtures("send_environment"),
]

TEXT = {
    "title": "Channel chat failed",
    "message": "slack-notice: credential_invalid, failed after 1 attempts (s)",
    "level": "warn",
}


def test_the_next_flush_routes_the_alert_and_delivers_it_in_the_same_run(rig: Rig) -> None:
    s = Setup(rig)
    s.fail("boom")
    assert s.ops.events("invoke") == []  # send never routes the alert
    code, doc, _ = s.flush()
    assert code == 0
    assert s.delivered_texts() == [TEXT]
    assert s.surfaced() == [("chat", "credential_invalid", "2023-11-14", 1, 1)]
    assert s.alerts() == [(2, "ops", "sent", None, "warn", "post-fail")]
    ops = [d for d in doc["destinations"] if d["name"] == "ops"][0]
    assert (ops["state"], ops["sent"]) == ("done", 1)
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]


def test_the_alert_is_logged_without_text(rig: Rig) -> None:
    s = Setup(rig)
    s.fail("boom")
    s.flush()
    lines = [x for x in checked_log(rig) if x["source"] == "notice-route"]
    assert [(x["hook"], x["rule"], x["destination"], x["level"]) for x in lines] == [
        ("post-fail", "alerts", "ops", "warn"),  # decided
        (None, "alerts", "ops", "warn"),  # delivered (a flush line carries no hook)
    ]
    assert [x["outcome"] for x in lines] == ["deferred", "sent"]
    assert "Channel chat failed" not in (rig.state / "log.jsonl").read_text()
    assert "credential_invalid, failed" not in rig.state_text()  # message text is never stored


def test_a_surfaced_row_is_taken_once(rig: Rig) -> None:
    s = Setup(rig)
    s.fail("boom")
    s.flush()
    s.flush()
    s.flush()
    assert len(s.ops.events("invoke")) == 1 and len(s.alerts()) == 1


def test_an_alert_about_a_failed_alert_routes_nothing_further(rig: Rig) -> None:
    s = Setup(rig)
    s.ops.set(
        exit=12,
        stdout=json.dumps(
            {"error": {"code": "credential_invalid", "retryable": False, "unknown_outcome": False}}
        ),
    )
    s.fail("boom")
    code, _, _ = s.flush()
    assert code == 12  # the alert's delivery failed
    assert s.surfaced() == [
        ("chat", "credential_invalid", "2023-11-14", 1, 1),
        ("ops", "credential_invalid", "2023-11-14", 2, 0),
    ]
    s.flush()
    s.flush()
    assert len(s.alerts()) == 1 and len(s.ops.events("invoke")) == 1
    assert [r[4] for r in s.surfaced()] == [1, 1]  # both taken, nothing new


def test_an_unrouted_alert_is_taken_and_logged_only(rig: Rig) -> None:
    s = Setup(rig, rules=[SEND_RULE])
    s.fail("boom")
    code, _, err = s.flush()
    assert code == 22 and err == ""
    assert s.alerts() == [] and s.ops.events("invoke") == []
    assert s.surfaced()[0][4] == 1
    (line,) = [x for x in checked_log(rig) if x["source"] == "notice-route"]
    assert (line["outcome"], line["reason"], line["destination"]) == ("dropped", "no_rule", None)
    _, out, _ = rig.run("validate", "--json")
    assert "alert_unrouted" in [w["code"] for w in json.loads(out)["warnings"]]


def test_a_drop_rule_for_alerts_routes_nothing(rig: Rig) -> None:
    drop = {"name": "quiet-alerts", "match": {"source": "notice-route"}, "drop": True}
    s = Setup(rig, rules=[drop, ALERT_RULE, SEND_RULE])
    s.fail("boom")
    s.flush()
    assert s.alerts() == []
    (line,) = [x for x in checked_log(rig) if x["source"] == "notice-route"]
    assert line["reason"] == "rule_drop"


@pytest.mark.parametrize(
    ("dest", "level", "outcome", "reason"),
    [
        ({"quiet": "night"}, "warn", "deferred", "quiet"),
        ({"quiet": "night"}, "info", "dropped", "quiet"),
        ({"floor": "error"}, "warn", "dropped", "below_floor"),
        ({"enabled": False}, "warn", "disabled", None),
    ],
)
def test_the_alert_meets_quiet_hours_floor_and_disabled_like_any_notice(
    rig: Rig, dest: dict[str, Any], level: str, outcome: str, reason: str | None
) -> None:
    rule = dict(ALERT_RULE, level=level)
    s = Setup(rig, rules=[rule, SEND_RULE], destinations={"ops": dest})
    s.fail("boom")
    s.flush()
    assert s.ops.events("invoke") == []
    (row,) = s.alerts()
    assert (row[2], row[3]) == (outcome, reason)


def test_an_error_alert_is_delivered_in_quiet_hours(rig: Rig) -> None:
    rule = dict(ALERT_RULE, level="error")
    s = Setup(rig, rules=[rule, SEND_RULE], destinations={"ops": {"quiet": "night"}})
    s.fail("boom")
    s.flush()
    assert [t["level"] for t in s.delivered_texts()] == ["error"]


def test_an_alert_over_the_hourly_cap_waits_for_the_next_hour(rig: Rig) -> None:
    s = Setup(rig, destinations={"ops": {"max_per_hour": 1}})
    s.fail("one")
    s.behave("other_fault")
    s.fail("two")  # a second code: a second surfaced row
    s.flush()
    assert len(s.ops.events("invoke")) == 1  # the cap: one call this hour
    assert rig.query("SELECT COUNT(*) FROM queue") == [(1,)]
    rig.clock.jump_wall(HOUR)
    s.flush()
    assert len(s.ops.events("invoke")) == 2 and rig.query("SELECT COUNT(*) FROM queue") == [(0,)]


def test_an_alert_for_an_unset_credential_waits_until_it_is_present(rig: Rig) -> None:
    s = Setup(rig, destinations={"ops": {"env": ["OPS_CRED"]}})
    s.fail("boom")
    code, doc, _ = flush(rig, env={})
    ops = [d for d in doc["destinations"] if d["name"] == "ops"][0]
    assert ops["state"] == "env_missing" and s.ops.events("invoke") == []
    assert rig.query("SELECT COUNT(*) FROM queue") == [(1,)]
    flush(rig, env={"OPS_CRED": "present"})
    assert len(s.ops.events("invoke")) == 1 and rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
    # the wait itself was surfaced (env_missing, once per hour) and is handled like the rest
    assert ("ops", "env_missing") in [(r[0], r[1]) for r in s.surfaced()]


def test_the_alert_never_carries_notifier_output(rig: Rig) -> None:
    s = Setup(rig)
    body = {
        "error": {
            "code": "credential_invalid", "retryable": False, "unknown_outcome": False,
            "message": CANARY, "details": {"detail": CANARY},
        },
        "message": CANARY,
    }  # fmt: skip
    s.bad.set(exit=12, stdout=json.dumps(body), stderr=CANARY)
    _, _, err = s.fail("boom")
    _, _, err2 = s.flush()
    blob = json.dumps(s.delivered_texts()) + rig.state_text() + err + err2
    assert CANARY not in blob


def test_a_hostile_label_or_source_never_reaches_the_alert_text(rig: Rig) -> None:
    s = Setup(rig)
    rig.send(
        "boom",
        source="s",
        extra=(
            "--label",
            "team=a-b",
        ),
    )
    s.flush()
    assert s.delivered_texts() == [TEXT]
    assert rig.query("SELECT labels FROM deliveries WHERE id = 1") == [('["team=a-b"]',)]


# --- the surfaced row ------------------------------------------------------------------------


def test_a_row_whose_delivery_was_deleted_is_processed_and_skipped(rig: Rig) -> None:
    s = Setup(rig)
    s.fail("boom")
    import sqlite3

    con = sqlite3.connect(rig.state / "state.db")
    con.execute("DELETE FROM deliveries")
    con.commit()
    con.close()
    code, _, err = s.flush()
    assert code in (0, 22) and err == ""
    assert s.surfaced()[0][4] == 1 and s.ops.events("invoke") == []


def test_a_row_of_a_removed_destination_is_processed_and_skipped(rig: Rig) -> None:
    s = Setup(rig)
    s.fail("boom")
    rig.configure({"ops": {"notifier": s.ops}}, [ALERT_RULE])
    code, _, err = s.flush()
    assert err == "" and s.surfaced()[0][4] == 1 and s.ops.events("invoke") == []
    assert s.alerts() == []


def test_a_row_of_a_disabled_destination_is_processed_and_skipped(rig: Rig) -> None:
    s = Setup(rig)
    s.fail("boom")
    rig.configure(
        {"chat": {"notifier": s.bad, "enabled": False}, "ops": {"notifier": s.ops}},
        [ALERT_RULE, SEND_RULE],
    )
    s.flush()
    assert s.surfaced()[0][4] == 1 and s.ops.events("invoke") == []


def test_a_dry_run_and_a_disabled_router_take_nothing(rig: Rig) -> None:
    s = Setup(rig)
    s.fail("boom")
    s.flush("--dry-run")
    assert s.surfaced()[0][4] == 0
    rig.configure({"ops": {"notifier": s.ops}}, [ALERT_RULE], enabled=False)
    rig.run("flush", "--json")
    assert s.surfaced()[0][4] == 0


def test_a_flush_of_one_destination_still_takes_the_surfaced_rows(rig: Rig) -> None:
    s = Setup(rig)
    s.fail("boom")
    s.flush("--destination", "ops")
    assert s.surfaced()[0][4] == 1 and len(s.ops.events("invoke")) == 1


def test_a_pending_row_pins_its_delivery_and_a_processed_one_releases_it(rig: Rig) -> None:
    s = Setup(rig, retention={"history": "1d", "event_ids": "1d"})
    s.fail("boom")
    rig.clock.jump_wall(3 * DAY)
    code, _, _ = rig.run("prune", "--json")
    assert code == 0
    assert rig.query("SELECT COUNT(*) FROM deliveries") == [(1,)]  # pinned
    s.flush()
    rig.run("prune", "--json")
    assert rig.query("SELECT id FROM deliveries WHERE source = 's'") == []  # released
    # the surfaced row is processed and 3 days old: deleted with it
    assert rig.query("SELECT COUNT(*) FROM surfaced") == [(0,)]


def test_a_clock_step_back_inside_the_hour_does_not_duplicate_a_row(rig: Rig) -> None:
    s = Setup(rig, destinations={"chat": {"notifier": rig.notifier("chat"), "max_per_hour": 1}})
    s.behave("credential_invalid")
    s.fail("one")
    s.fail("two")
    s.fail("three")
    rig.clock.jump_wall(-600)
    s.fail("four")
    assert [r[2] for r in s.surfaced() if r[1] == "rate_capped"] == ["2023-11-14T22"]
    _ = seed


def test_send_refuses_the_source_notice_route(rig: Rig) -> None:
    Setup(rig)
    code, out, _ = rig.run(
        "send", "--stdin-json", "--json", "--source", "notice-route",
        stdin=b'{"message": "forged"}',
    )  # fmt: skip
    assert code == 20 and json.loads(out)["error"]["reason"] == "source_mismatch"
    hook = {"tool": "notice-route", "hook": "post-fail", "object_id": "chat"}
    code, out, _ = rig.run("send", "--stdin-json", "--json", stdin=json.dumps(hook).encode())
    assert code == 20 and json.loads(out)["error"]["reason"] == "source_mismatch"
    assert not (rig.state / "state.db").exists()  # nothing was written


def test_an_expired_row_becomes_an_alert_on_the_next_flush(rig: Rig) -> None:
    s = Setup(rig)
    old = NOW - DAY - 5
    seed(rig, "chat", created_at=old, now=old)
    flush(rig, "--destination", "chat")
    assert s.ops.events("invoke") == []
    flush(rig)
    (text,) = s.delivered_texts()
    assert text["message"] == "slack-notice: expired, expired after 0 attempts (s)"


def test_a_failed_digest_is_one_alert(rig: Rig) -> None:
    s = Setup(rig)
    for i in range(3):
        seed(rig, "chat", kind="held", level="warn", message=f"m{i}")
    flush(rig, "--destination", "chat")
    flush(rig)
    assert len(s.delivered_texts()) == 1 and len(s.alerts()) == 1
