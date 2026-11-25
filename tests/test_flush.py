"""``flush`` end to end (#23): the real entry point, fake notifiers, a real state directory."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from notice_route.gateway import SYSTEM_TZ_DIRS
from tests.flush_support import dest, flush, seed
from tests.send_support import MORNING, NOW, Rig, log_errors

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = [
    pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database"),
    pytest.mark.usefixtures("send_environment"),
]

DAY = 86400


def one(rig: Rig, **behavior: Any) -> Any:
    n = rig.notifier(**behavior)
    rig.configure({"chat": {"notifier": n}})
    return n


def invoked(n: Any) -> list[str]:
    """The JSON texts the notifier read on stdin, in call order."""
    return [r["text"] for r in n.events("stdin")]


def states(rig: Rig) -> list[tuple[Any, ...]]:
    return rig.query("SELECT id, outcome, reason, code, attempts FROM deliveries ORDER BY id")


# --- steps 6: due single rows ----------------------------------------------------------------


def test_due_single_rows_are_sent_oldest_first_one_call_each(rig: Rig) -> None:
    n = one(rig)
    for text in ("one", "two", "three"):
        seed(rig, message=text)
    code, doc, err = flush(rig)
    assert code == 0 and err == ""
    d = dest(doc)
    assert (d["state"], d["due_single"], d["sent"]) == ("done", 3, 3)
    assert doc["outcome"] == "ok" and doc["dry_run"] is False
    assert [json.loads(i)["message"] for i in invoked(n)] == ["one", "two", "three"]
    assert [s[1] for s in states(rig)] == ["sent"] * 3
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
    assert rig.query("SELECT count FROM rate") == [(3,)]


def test_the_log_has_one_line_per_row_and_one_flush_summary(rig: Rig) -> None:
    one(rig)
    seed(rig, message="one")
    seed(rig, message="two")
    flush(rig, "--actor", "auto:cron", "--decision-id", "d1")
    lines = rig.log_lines()
    assert all(log_errors(x) == [] for x in lines)
    assert [x["record"] for x in lines] == ["delivery", "delivery", "flush"]
    assert {x["actor"] for x in lines} == {"auto:cron"} and {x["decision_id"] for x in lines} == {
        "d1"
    }
    summary = lines[-1]
    assert (summary["due"], summary["sent"], summary["failed"]) == (2, 2, 0)
    assert lines[0]["outcome"] == "sent" and lines[0]["destination"] == "chat"
    assert lines[0]["message_sha256"] and "one" not in json.dumps(lines)


@pytest.mark.parametrize(
    ("delta", "sent"), [(-1, 1), (0, 1), (1, 0)], ids=["past", "now", "future"]
)
def test_a_row_is_due_when_its_deliver_after_has_passed(rig: Rig, delta: int, sent: int) -> None:
    n = one(rig)
    seed(rig, deliver_after=NOW + delta)
    code, doc, _ = flush(rig)
    assert dest(doc)["sent"] == sent and len(n.events("invoke")) == sent
    assert code == (0 if sent else 22)
    assert dest(doc)["state"] == ("done" if sent else "nothing_due")


def test_nothing_due_is_exit_22_no_op_and_a_run_still_writes_the_heartbeat(rig: Rig) -> None:
    one(rig)
    code, doc, _ = flush(rig)
    assert (code, doc["outcome"]) == (22, "no_op")
    assert dest(doc)["state"] == "nothing_due"
    assert rig.query("SELECT key, value FROM meta ORDER BY key") == [
        ("flush_env_missing", None), ("flush_last", NOW),
    ]  # fmt: skip


def test_no_config_and_a_disabled_router_are_no_ops_that_touch_no_state(rig: Rig) -> None:
    code, doc, err = flush(rig)
    assert (code, doc["outcome"], doc["destinations"]) == (22, "no_op", [])
    assert "no config" in err
    one(rig)
    rig.configure({"chat": {"notifier": rig.notifier()}}, enabled=False)
    code, doc, _ = flush(rig)
    assert (code, doc["destinations"]) == (22, [])
    assert not (rig.state / "state.db").exists()


def test_an_unknown_destination_is_a_usage_error(rig: Rig) -> None:
    one(rig)
    code, doc, err = flush(rig, "--destination", "nope")
    assert code == 2 and doc["error"]["code"] == "usage"
    assert "nope" not in err and "nope" not in json.dumps(doc)


def test_destination_limits_the_run_and_writes_no_heartbeat(rig: Rig) -> None:
    a, b = rig.notifier("a"), rig.notifier("b")
    rig.configure({"a": {"notifier": a}, "b": {"notifier": b}})
    seed(rig, "a")
    seed(rig, "b")
    code, doc, _ = flush(rig, "--destination", "b")
    assert [d["name"] for d in doc["destinations"]] == ["b"] and code == 0
    assert len(a.events("invoke")) == 0 and len(b.events("invoke")) == 1
    assert rig.query("SELECT key, value FROM meta ORDER BY key") == [
        ("flush_env_missing", None), ("flush_last", None),
    ]  # fmt: skip


def test_destinations_run_in_parallel(rig: Rig) -> None:
    a, b = rig.notifier("a", sleep=0.7), rig.notifier("b", sleep=0.7)
    rig.configure({"a": {"notifier": a}, "b": {"notifier": b}})
    seed(rig, "a")
    seed(rig, "b")
    code, doc, _ = flush(rig)
    assert code == 0 and doc["duration_ms"] < 1300
    assert (dest(doc, "a")["sent"], dest(doc, "b")["sent"]) == (1, 1)


def test_text_output_goes_to_stderr_and_stdout_stays_empty(rig: Rig) -> None:
    one(rig)
    seed(rig)
    code, out, err = rig.run("flush")
    assert (code, out) == (0, "")
    assert err == "notice-route: chat: done (sent=1)\n"


# --- steps 1 to 3 -----------------------------------------------------------------------------


def test_a_disabled_destination_is_skipped_and_its_rows_wait(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "enabled": False}})
    seed(rig, created_at=NOW - 2 * DAY)
    code, doc, _ = flush(rig)
    assert (code, doc["outcome"], dest(doc)["state"]) == (22, "no_op", "disabled")
    assert dest(doc)["due_single"] == 0 and n.events("invoke") == []
    assert rig.query("SELECT COUNT(*) FROM queue") == [(1,)]  # not even expired


def test_a_destination_with_an_unset_variable_is_left_untouched(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "env": ["ROUTE_KEY"]}})
    seed(rig, created_at=NOW - 2 * DAY)
    seed(rig)
    before = states(rig)
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["state"], dest(doc)["sent"]) == (0, "env_missing", 0)
    assert n.events("invoke") == [] and states(rig) == before
    assert rig.query("SELECT COUNT(*) FROM queue WHERE claimed_by IS NOT NULL") == [(0,)]
    assert rig.query("SELECT value FROM meta WHERE key = 'flush_env_missing'") == [(NOW,)]
    code, doc, _ = flush(rig, env={"ROUTE_KEY": ""})  # an empty value counts as unset
    assert dest(doc)["state"] == "env_missing"


def test_a_set_variable_lets_the_destination_run_and_clears_the_marker(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "env": ["ROUTE_KEY"]}})
    seed(rig)
    flush(rig)
    code, doc, _ = flush(rig, env={"ROUTE_KEY": "v"})
    assert (code, dest(doc)["state"], dest(doc)["sent"]) == (0, "done", 1)
    assert rig.query("SELECT value FROM meta WHERE key = 'flush_env_missing'") == [(None,)]


# --- step 4: expiry ----------------------------------------------------------------------------


def test_rows_24_hours_old_end_expired_and_younger_rows_are_sent(rig: Rig) -> None:
    n = one(rig)
    old, _ = seed(rig, created_at=NOW - DAY, message="old")
    young, _ = seed(rig, created_at=NOW - DAY + 1, message="young")
    code, doc, _ = flush(rig)
    d = dest(doc)
    assert (code, d["state"], d["expired"], d["sent"]) == (0, "done", 1, 1)
    assert (d["due_single"], d["due_held"]) == (2, 0)
    assert [json.loads(i)["message"] for i in invoked(n)] == ["young"]
    got = dict((r[0], r[1:]) for r in rig.query("SELECT id, outcome, code FROM deliveries"))
    assert got[old] == ("expired", "expired") and got[young][0] == "sent"
    assert any(x["outcome"] == "expired" for x in rig.log_lines())


def test_a_claimed_row_is_not_expired_until_its_claim_lapses(rig: Rig) -> None:
    n = one(rig)
    seed(rig, created_at=NOW - 2 * DAY, token="t", claim_expires=NOW + 50)
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["state"]) == (22, "nothing_due")
    assert rig.query("SELECT COUNT(*) FROM queue") == [(1,)] and n.events("invoke") == []


# --- step 5: quiet -----------------------------------------------------------------------------


def quiet_config(rig: Rig, **dest_keys: Any) -> Any:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "quiet": "night", **dest_keys}})
    return n


def test_while_quiet_info_is_dropped_warn_and_action_wait_for_the_window_end(rig: Rig) -> None:
    n = quiet_config(rig)
    info, _ = seed(rig, level="info")
    warn, w = seed(rig, level="warn")
    action, a = seed(rig, level="action", kind="held")
    err_id, _ = seed(rig, level="error")
    code, doc, _ = flush(rig)
    d = dest(doc)
    assert (code, d["state"], d["dropped"], d["sent"]) == (0, "done", 1, 1)
    assert len(n.events("invoke")) == 1
    got = {
        r[0]: r[1:] for r in rig.query("SELECT id, outcome, reason, deliver_after FROM deliveries")
    }
    assert got[info] == ("dropped", "quiet", None)
    assert got[err_id][0] == "sent"
    assert got[warn][2] == MORNING and got[action][2] == MORNING
    assert rig.query("SELECT id, kind, deliver_after FROM queue ORDER BY id") == [
        (w, "single", MORNING), (a, "held", MORNING),
    ]  # fmt: skip


def test_while_quiet_with_only_postponed_rows_the_state_is_quiet(rig: Rig) -> None:
    n = quiet_config(rig)
    seed(rig, level="warn")
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["state"]) == (0, "quiet")
    assert n.events("invoke") == []


def test_a_held_row_whose_window_has_ended_is_sent_as_itself(rig: Rig) -> None:
    n = quiet_config(rig)
    rig.clock.jump_wall(MORNING - NOW + 10)  # 06:00:10
    seed(rig, level="warn", kind="held", deliver_after=MORNING)
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["state"], dest(doc)["sent"]) == (0, "done", 1)
    assert len(n.events("invoke")) == 1


def test_two_due_held_rows_are_left_for_the_digest(rig: Rig) -> None:
    n = one(rig)
    seed(rig, level="warn", kind="held")
    seed(rig, level="warn", kind="held")
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["due_held"], dest(doc)["sent"], dest(doc)["digests"]) == (22, 2, 0, 0)
    assert n.events("invoke") == []  # TODO(#24)


# --- stop after a result that is not sent ------------------------------------------------------


def test_a_failed_result_stops_the_destination_and_the_other_rows_keep_their_deliver_after(
    rig: Rig,
) -> None:
    n = one(rig, error={"code": "credential_invalid"})
    seed(rig, message="one")
    _, second = seed(rig, message="two", deliver_after=NOW - 5)
    seed(rig, message="three", deliver_after=NOW - 3)
    code, doc, _ = flush(rig)
    d = dest(doc)
    assert (code, doc["outcome"], d["failed"], d["sent"]) == (12, "partial", 1, 0)
    assert len(n.events("invoke")) == 1
    assert rig.query("SELECT id, deliver_after, claimed_by FROM queue ORDER BY id")[0][1:] == (
        NOW - 5,
        None,
    )
    assert rig.query("SELECT outcome FROM deliveries ORDER BY id") == [
        ("failed",), ("deferred",), ("deferred",),
    ]  # fmt: skip
    assert second > 0


def test_a_retryable_result_stops_the_destination_and_exits_0(rig: Rig) -> None:
    n = one(rig, error={"code": "rate_limited"})
    seed(rig, message="one")
    seed(rig, message="two")
    code, doc, _ = flush(rig)
    d = dest(doc)
    assert (code, d["queued_retry"], d["failed"]) == (0, 1, 0)
    assert len(n.events("invoke")) == 1
    assert rig.query("SELECT outcome, attempts, deliver_after FROM deliveries ORDER BY id") == [
        ("queued_retry", 1, NOW + 60), ("deferred", 0, NOW),
    ]  # fmt: skip


def test_an_unknown_result_is_exit_12_and_stops(rig: Rig) -> None:
    n = one(rig, error={"code": "outcome_unknown"})
    seed(rig)
    seed(rig)
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["unknown"]) == (12, 1)
    assert len(n.events("invoke")) == 1


def test_a_failure_of_one_destination_does_not_stop_another(rig: Rig) -> None:
    bad, good = rig.notifier("bad", error={"code": "credential_invalid"}), rig.notifier("good")
    rig.configure({"bad": {"notifier": bad}, "good": {"notifier": good}})
    seed(rig, "bad")
    seed(rig, "good")
    seed(rig, "good")
    code, doc, _ = flush(rig)
    assert (code, dest(doc, "bad")["failed"], dest(doc, "good")["sent"]) == (12, 1, 2)


def test_the_hourly_cap_leaves_a_row_with_its_attempts_intact(rig: Rig) -> None:
    n = one(rig)
    rig.configure({"chat": {"notifier": n, "max_per_hour": 2}})
    for _ in range(3):
        seed(rig)
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["sent"], dest(doc)["state"]) == (0, 2, "done")
    assert len(n.events("invoke")) == 2
    assert rig.query("SELECT attempts, outcome FROM deliveries ORDER BY id DESC LIMIT 1") == [
        (0, "deferred")
    ]
    assert rig.query("SELECT COUNT(*) FROM queue WHERE claimed_by IS NULL") == [(1,)]
    code, doc, _ = flush(rig)  # still capped: nothing more, no change
    assert dest(doc)["sent"] == 0 and len(n.events("invoke")) == 2
