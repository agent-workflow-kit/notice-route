"""``flush`` end to end (#23): the real entry point, fake notifiers, a real state directory."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from notice_route import calling, cli
from notice_route import flush as flush_mod
from notice_route.gateway import SYSTEM_TZ_DIRS, Gateway, Interrupted
from notice_route.notify import call_notifier
from notice_route.queue import Lease, claim_row, enqueue, insert_delivery
from notice_route.send import Seams
from notice_route.store import open_store
from tests.flush_support import checked_log, dest, flush, seed
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
    lines = checked_log(rig)
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
    a, b = rig.notifier("a", sleep=2.0), rig.notifier("b", sleep=2.0)
    rig.configure({"a": {"notifier": a}, "b": {"notifier": b}})
    t = real_now()
    seed(rig, "a", now=t, deliver_after=t)
    seed(rig, "b", now=t, deliver_after=t)
    proc = rig.spawn("flush", "--json")
    a.wait_for("invoke", timeout=30)
    b.wait_for("invoke", timeout=30)
    # both calls are in flight at once: neither notifier has finished yet
    assert a.events("exit") == [] and b.events("exit") == []
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    doc = json.loads(out)
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
    (line,) = [x for x in checked_log(rig) if x["outcome"] == "expired"]
    assert (line["source"], line["attempts"], line["code"]) == ("s", 0, "expired")


def test_a_claimed_row_is_not_expired_until_its_claim_lapses(rig: Rig) -> None:
    n = one(rig)
    seed(rig, created_at=NOW - 2 * DAY, claim="t", claim_expires=NOW + 50)
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
    assert sorted(x["outcome"] for x in checked_log(rig) if x["record"] == "delivery") == [
        "dropped", "sent",
    ]  # fmt: skip
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
    assert [x["outcome"] or x["record"] for x in checked_log(rig)] == ["failed", "flush"]


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


# --- locks -------------------------------------------------------------------------------------


def hold(rig: Rig, name: str) -> Any:
    """Take ``locks/<name>`` as another process would; the caller closes the file."""
    gw = rig.gateway()
    with gw.open_state_dir() as state, gw.open_private_subdir(state, "locks") as locks:
        file = gw.open_file(locks, name, mode="a", create=True)
    assert gw.try_lock(file, 0)
    return file


def test_a_busy_send_lock_requeues_the_row_and_stops_the_destination(rig: Rig) -> None:
    other = rig.notifier("other")
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}, "other": {"notifier": other}}, lock_wait="1s")
    seed(rig, message="one")
    seed(rig, message="two")
    seed(rig, "other")
    held = hold(rig, "chat.lock")
    try:
        code, doc, _ = flush(rig)
    finally:
        held.close()
    d = dest(doc)
    assert (code, d["queued_retry"], d["failed"]) == (0, 1, 0)
    assert dest(doc, "other")["sent"] == 1 and n.events("invoke") == []
    assert rig.query(
        "SELECT outcome, reason, attempts, deliver_after FROM deliveries"
        " WHERE destination = 'chat' ORDER BY id"
    ) == [("queued_retry", "lock_busy", 0, NOW + 60), ("deferred", None, 0, NOW)]
    assert rig.query("SELECT count FROM rate WHERE destination = 'chat'") == [(0,)]
    assert rig.query("SELECT deliver_after FROM queue ORDER BY id") == [(NOW + 60,), (NOW,)]


def test_a_second_flusher_on_a_destination_exits_already_running(rig: Rig) -> None:
    n = one(rig)
    seed(rig)
    held = hold(rig, "chat.flush")
    try:
        code, doc, err = flush(rig)
    finally:
        held.close()
    assert (code, dest(doc)["state"], dest(doc)["due_single"], err) == (0, "already_running", 0, "")
    assert n.events("invoke") == [] and rig.query("SELECT COUNT(*) FROM queue") == [(1,)]
    code, doc, _ = flush(rig)  # the lock is free again
    assert dest(doc)["sent"] == 1


def test_an_unusable_flush_lock_is_an_internal_error_and_the_destination_is_left_untouched(
    rig: Rig,
) -> None:
    n = one(rig)
    seed(rig)
    locks = rig.state / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    locks.chmod(0o755)  # not private: unusable
    code, out, err = rig.run("flush", "--json")
    doc = json.loads(out)
    assert code == 1 and doc["error"]["code"] == "internal"
    assert "already_running" not in out
    assert n.events("invoke") == [] and rig.query("SELECT COUNT(claimed_by) FROM queue") == [(0,)]


def test_a_flush_lock_that_cannot_be_opened_is_an_internal_error_too(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = one(rig)
    seed(rig)

    def broken(*_a: Any, **_k: Any) -> bool:
        raise OSError("LOCK-TEXT")

    monkeypatch.setattr(Gateway, "try_lock", broken)
    code, out, err = rig.run("flush", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "internal"
    assert "LOCK-TEXT" not in out + err and n.events("invoke") == []


def test_a_flush_that_finds_the_flush_lock_held_waits_for_nothing(rig: Rig) -> None:
    n = one(rig)
    seed(rig)
    held = hold(rig, "chat.flush")
    try:
        started = time.monotonic()
        code, doc, _ = flush(rig)
        waited = time.monotonic() - started
    finally:
        held.close()
    assert (code, dest(doc)["state"]) == (0, "already_running")
    assert waited < 5 and n.events("invoke") == []


def test_a_stopped_flusher_keeps_its_locks_so_no_second_call_is_made(rig: Rig) -> None:
    n = one(rig, sleep=1.0)
    t = real_now()
    seed(rig, now=t, deliver_after=t)
    proc = rig.spawn("flush", "--json")
    n.wait_for("invoke", timeout=30)
    proc.send_signal(signal.SIGSTOP)  # the exact handle this test started
    try:
        conn = sqlite3.connect(rig.state / "state.db")
        conn.execute("UPDATE queue SET claim_expires = ?", (t - 1,))  # the lease has lapsed
        conn.commit()
        conn.close()
        code, doc, _ = flush(rig)  # the locks, not the lease, protect the row
        assert (code, dest(doc)["state"]) == (0, "already_running")
        assert len(n.events("invoke")) == 1
    finally:
        proc.send_signal(signal.SIGCONT)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert json.loads(out)["destinations"][0]["sent"] == 1
    assert rig.query("SELECT outcome FROM deliveries") == [("sent",)]
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
    assert len(n.events("invoke")) == 1


# --- processes ---------------------------------------------------------------------------------


def real_now() -> int:
    return int(time.time())


def test_two_flushers_on_one_destination_with_40_due_rows_send_each_row_once(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "max_per_hour": 0}})
    t = real_now()
    for i in range(40):
        seed(rig, message=f"row{i:02}", now=t, deliver_after=t)
    procs = [rig.spawn("flush", "--json") for _ in range(2)]
    results = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode in (0, 22) for p in procs), results
    sent = [json.loads(i)["message"] for i in invoked(n)]
    assert sorted(sent) == [f"row{i:02}" for i in range(40)]
    assert len(n.events("invoke")) == 40
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
    assert rig.query("SELECT COUNT(*) FROM deliveries WHERE outcome = 'sent'") == [(40,)]


def test_a_flusher_killed_mid_call_leaves_a_claimed_row_that_lapses_and_is_resent_once(
    rig: Rig,
) -> None:
    n = rig.notifier(hang=True, max_seconds=60)
    rig.configure({"chat": {"notifier": n}})
    t = real_now()
    seed(rig, now=t, deliver_after=t)
    proc = rig.spawn("flush", "--json")
    n.wait_for("invoke", timeout=30)
    proc.kill()  # the exact handle this test started
    proc.communicate(timeout=10)
    n.kill_tracked()
    ((owner, expires),) = rig.query("SELECT claimed_by, claim_expires FROM queue")
    assert owner is not None and expires > t
    n.set()
    later = rig.spawn("flush", "--json")  # the kernel freed the flush lock; the claim is alive
    out, _ = later.communicate(timeout=30)
    assert json.loads(out)["destinations"][0]["sent"] == 0
    conn = sqlite3.connect(rig.state / "state.db")
    conn.execute("UPDATE queue SET claim_expires = ?", (t - 1,))
    conn.commit()
    conn.close()
    again = rig.spawn("flush", "--json")
    out, _ = again.communicate(timeout=30)
    assert json.loads(out)["destinations"][0]["sent"] == 1
    last = rig.spawn("flush", "--json")
    last.communicate(timeout=30)
    assert rig.query("SELECT outcome FROM deliveries") == [("sent",)]
    assert len(n.events("invoke")) == 2  # the killed call and the resend: at least once


def test_a_sigterm_during_a_call_lets_it_finish_makes_no_more_calls_and_exits_with_the_result(
    rig: Rig,
) -> None:
    n = one(rig, sleep=3.0)
    t = real_now()
    for i in range(3):
        seed(rig, message=f"m{i}", now=t, deliver_after=t)
    proc = rig.spawn("flush", "--json")
    n.wait_for("invoke", timeout=30)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert json.loads(out)["destinations"][0]["sent"] == 1
    assert len(n.events("invoke")) == 1
    assert rig.query("SELECT COUNT(*), COUNT(claimed_by) FROM queue") == [(2, 0)]


def test_a_sigterm_before_any_call_releases_the_claim_and_exits_128_plus_n(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}}, lock_wait="2s")
    t = real_now()
    seed(rig, now=t, deliver_after=t)
    held = hold(rig, "chat.lock")
    try:
        proc = rig.spawn("flush", "--json")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not rig.query(
            "SELECT 1 FROM queue WHERE claimed_by IS NOT NULL"
        ):
            time.sleep(0.02)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
    finally:
        held.close()
    after = real_now()
    assert proc.returncode == 128 + signal.SIGTERM and out == b""
    assert n.events("invoke") == []
    ((claimed, expires),) = rig.query("SELECT claimed_by, claim_expires FROM queue")
    assert claimed is not None and expires <= after  # released: due again at once
    assert rig.query("SELECT count FROM rate") == [(0,)]


def test_a_sigterm_during_the_send_lock_wait_ends_the_wait_and_gives_the_row_back(
    rig: Rig,
) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}}, lock_wait="30s")
    t = real_now()
    seed(rig, now=t, deliver_after=t)
    held = hold(rig, "chat.lock")
    try:
        proc = rig.spawn("flush", "--json")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not rig.query(
            "SELECT 1 FROM queue WHERE claimed_by IS NOT NULL"
        ):
            time.sleep(0.02)
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)  # far below lock_wait
        waited = time.monotonic() - started
    finally:
        held.close()
    assert proc.returncode == 128 + signal.SIGTERM and out == b"", err
    assert waited < 10 and n.events("invoke") == []
    assert rig.query("SELECT outcome, reason, attempts FROM deliveries") == [
        ("deferred", None, 0)
    ]  # not requeued as lock_busy
    assert rig.query("SELECT count FROM rate") == [(0,)]
    ((claimed, expires),) = rig.query("SELECT claimed_by, claim_expires FROM queue")
    assert claimed is not None and expires <= real_now()


# --- faults ------------------------------------------------------------------------------------


def use_seams(monkeypatch: pytest.MonkeyPatch, seams: Seams) -> None:
    real = cli.run_flush
    monkeypatch.setattr(cli, "run_flush", lambda *a, **k: real(*a, **{**k, "seams": seams}))


def test_a_result_after_a_takeover_is_fenced_and_goes_to_the_log_only(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = one(rig)
    _, row_id = seed(rig)

    def takeover(*args: Any, **kwargs: Any) -> Any:
        with open_store(rig.gateway()) as store, store.write() as conn:
            conn.execute("UPDATE queue SET claim_expires = ?", (NOW - 1,))
            assert claim_row(conn, row_id, Lease(NOW, 50), "other-claimant") is not None
        return call_notifier(*args, **kwargs)

    use_seams(monkeypatch, Seams(call=takeover))
    code, doc, err = flush(rig)
    assert (code, dest(doc)["sent"]) == (0, 1)
    assert "claim was lost" in err
    assert rig.query("SELECT claimed_by FROM queue") == [("other-claimant",)]
    assert rig.query("SELECT outcome, attempts FROM deliveries") == [("deferred", 0)]
    assert len(n.events("invoke")) == 1 and any(x["outcome"] == "sent" for x in checked_log(rig))


def test_an_exception_from_a_call_is_unknown_leaves_the_row_claimed_and_stops(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig)
    seed(rig, message="one")
    seed(rig, message="two")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("BOOM-TEXT")

    use_seams(monkeypatch, Seams(call=boom))
    code, doc, err = flush(rig)
    assert (code, dest(doc)["unknown"]) == (12, 1)
    assert rig.query("SELECT outcome, code, attempts FROM deliveries ORDER BY id") == [
        ("unknown", "killed", 1), ("deferred", None, 0),
    ]  # fmt: skip
    assert rig.query("SELECT COUNT(claimed_by) FROM queue") == [(1,)]
    assert "BOOM-TEXT" not in err + json.dumps(doc) + rig.state_text()


def test_a_row_that_keeps_escaping_ends_failed_without_another_call(rig: Rig) -> None:
    n = one(rig)
    seed(rig, attempts=5, outcome="unknown", claim="old", claim_expires=NOW - 1)
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["failed"]) == (12, 1) and n.events("invoke") == []
    assert rig.query("SELECT outcome, code FROM deliveries") == [("failed", "retries_exhausted")]
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
    assert rig.query("SELECT count FROM rate") == [(0,)]


def test_a_lapsed_claim_with_attempts_left_is_resent(rig: Rig) -> None:
    n = one(rig)
    seed(rig, attempts=2, outcome="unknown", claim="old", claim_expires=NOW - 1)
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["sent"], len(n.events("invoke"))) == (0, 1, 1)
    assert rig.query("SELECT attempts, outcome FROM deliveries") == [(3, "sent")]


def test_a_failing_surfacing_seam_is_exit_1_and_the_claim_is_left_to_lapse(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig, error={"code": "credential_invalid"})
    seed(rig)

    def broken(*_a: Any, **_k: Any) -> None:
        raise ValueError("SEAM-TEXT")

    use_seams(monkeypatch, Seams(surface=broken))
    code, out, err = rig.run("flush", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "internal"
    assert "SEAM-TEXT" not in out + err
    assert rig.query("SELECT outcome FROM deliveries") == [("deferred",)]
    assert rig.query("SELECT COUNT(claimed_by) FROM queue") == [(1,)]


def test_an_error_from_the_send_lock_before_a_call_releases_the_row_and_is_exit_1(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig)
    seed(rig)

    def broken(*_a: Any, **_k: Any) -> None:
        raise OSError("LOCK-TEXT")

    monkeypatch.setattr(calling, "send_lock", broken)
    code, out, err = rig.run("flush", "--json")
    assert code == 1 and "LOCK-TEXT" not in out + err
    assert rig.query("SELECT outcome, attempts FROM deliveries") == [("deferred", 0)]
    assert rig.query("SELECT count FROM rate") == [(0,)]
    ((_, expires),) = rig.query("SELECT claimed_by, claim_expires FROM queue")
    assert expires <= NOW


# --- review round 1 ----------------------------------------------------------------------------


def test_quiet_omits_the_per_destination_lines_of_a_clean_run(rig: Rig) -> None:
    one(rig)
    seed(rig)
    code, out, err = rig.run("flush", "--quiet")
    assert (code, out, err) == (0, "", "")
    code, out, err = rig.run("flush", "--quiet")  # nothing due: exit 22
    assert (code, out, err) == (22, "", "")


def test_quiet_keeps_the_lines_and_warnings_of_a_run_that_failed(rig: Rig) -> None:
    one(rig, error={"code": "credential_invalid"})
    seed(rig)
    code, _, err = rig.run("flush", "--quiet")
    assert code == 12 and "chat: done (failed=1)" in err


def test_quiet_keeps_the_warnings_of_a_clean_run(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    one(rig)
    seed(rig)

    def takeover(*args: Any, **kwargs: Any) -> Any:
        with open_store(rig.gateway()) as store, store.write() as conn:
            conn.execute("UPDATE queue SET claim_expires = ?", (NOW - 1,))
            claim_row(conn, 1, Lease(NOW, 50), "other-claimant")
        return call_notifier(*args, **kwargs)

    use_seams(monkeypatch, Seams(call=takeover))
    code, _, err = rig.run("flush", "--quiet")
    assert code == 0 and "claim was lost" in err and "chat: done" not in err


def test_an_exhausted_row_makes_no_call_and_does_not_stop_the_destination(rig: Rig) -> None:
    n = one(rig)
    seed(rig, attempts=5, outcome="unknown", claim="old", claim_expires=NOW - 1, message="spent")
    seed(rig, message="next")
    code, doc, _ = flush(rig)
    d = dest(doc)
    assert (code, d["failed"], d["sent"]) == (12, 1, 1)
    assert [json.loads(i)["message"] for i in invoked(n)] == ["next"]
    assert [r[0] for r in rig.query("SELECT outcome FROM deliveries ORDER BY id")] == [
        "failed", "sent",
    ]  # fmt: skip
    checked_log(rig)


def test_the_attempts_of_an_escaped_call_and_of_a_lost_claim_follow_the_row(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig)
    seed(rig, attempts=2, outcome="unknown", claim="old", claim_expires=NOW - 1)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("BOOM-TEXT")

    use_seams(monkeypatch, Seams(call=boom))
    flush(rig)
    assert rig.query("SELECT attempts FROM deliveries") == [(3,)]
    (line,) = [x for x in checked_log(rig) if x["record"] == "delivery"]
    assert (line["outcome"], line["attempts"]) == ("unknown", 3)


def test_the_attempts_of_a_result_whose_claim_was_lost_follow_the_row(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig)
    _, row_id = seed(rig, attempts=2, outcome="unknown", claim="old", claim_expires=NOW - 1)

    def takeover(*args: Any, **kwargs: Any) -> Any:
        with open_store(rig.gateway()) as store, store.write() as conn:
            conn.execute("UPDATE queue SET claim_expires = ?", (NOW - 1,))
            assert claim_row(conn, row_id, Lease(NOW, 50), "other-claimant") is not None
        return call_notifier(*args, **kwargs)

    use_seams(monkeypatch, Seams(call=takeover))
    flush(rig)
    (line,) = [x for x in checked_log(rig) if x["record"] == "delivery"]
    assert (line["outcome"], line["attempts"]) == ("sent", 3)


@pytest.mark.parametrize(
    "scenario",
    ["two_held", "old_row_with_future_deliver_after", "old_due_row", "quiet_warn_only"],
)
def test_dry_run_reports_the_state_a_real_run_ends_in(rig: Rig, scenario: str) -> None:
    if scenario == "quiet_warn_only":
        quiet_config(rig)
    else:
        one(rig)
    if scenario == "two_held":
        seed(rig, level="warn", kind="held")
        seed(rig, level="warn", kind="held")
    elif scenario == "old_row_with_future_deliver_after":
        seed(rig, created_at=NOW - 2 * DAY, deliver_after=NOW + 3600)
    elif scenario == "old_due_row":
        seed(rig, created_at=NOW - 2 * DAY)
    else:
        seed(rig, level="warn")
    dry_code, dry, _ = flush(rig, "--dry-run")
    real_code, real, _ = flush(rig)
    assert (dry_code, dest(dry)["state"]) == (real_code, dest(real)["state"])


def test_an_unknown_destination_with_no_config_is_exit_22_not_2(rig: Rig) -> None:
    code, doc, _ = flush(rig, "--destination", "nope")
    assert (code, doc["outcome"], doc["destinations"]) == (22, "no_op", [])


def test_a_fault_in_one_destination_thread_is_an_internal_error_and_keeps_the_others(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    good, bad = rig.notifier("good"), rig.notifier("bad")
    rig.configure({"bad": {"notifier": bad}, "good": {"notifier": good}})
    seed(rig, "bad")
    seed(rig, "good")
    real = flush_mod._prepare  # pyright: ignore[reportPrivateUsage]

    def prepare(run: Any, store: Any, dst: Any, t: Any) -> None:
        if dst.name == "bad":
            raise RuntimeError("BOOM-TEXT")
        real(run, store, dst, t)

    monkeypatch.setattr(flush_mod, "_prepare", prepare)
    code, out, err = rig.run("flush")
    assert code == 1 and "internal error" in err
    assert "state database" not in err and "BOOM-TEXT" not in out + err
    assert "good: done (sent=1)" in err and len(good.events("invoke")) == 1


def test_a_row_with_an_invalid_level_is_skipped_with_a_warning_and_the_run_goes_on(
    rig: Rig,
) -> None:
    n = one(rig)
    _, junk = seed(rig, message="junk")
    seed(rig, message="good")
    conn = sqlite3.connect(rig.state / "state.db")
    conn.execute("UPDATE queue SET level = 'JUNK-LEVEL' WHERE id = ?", (junk,))
    conn.commit()
    conn.close()
    code, out, err = rig.run("flush", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "internal"
    assert err.count("warning:") == 1 and "JUNK-LEVEL" not in out + err
    assert [json.loads(i)["message"] for i in invoked(n)] == ["good"]
    assert rig.query("SELECT id, claimed_by FROM queue") == [(junk, None)]
    assert rig.query("SELECT count FROM rate") == [(1,)]


def test_many_capped_rows_are_skipped_in_linear_time(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = one(rig)
    rig.configure({"chat": {"notifier": n, "max_per_hour": 3}})
    with open_store(rig.gateway()) as store, store.write() as conn:
        for i in range(300):
            did = insert_delivery(
                conn, ts=NOW, source="s", level="info", outcome="deferred", destination="chat",
                deliver_after=NOW,
            )  # fmt: skip
            enqueue(
                conn, delivery_id=did, kind="single", message=f"m{i}", level="info", source="s",
                deliver_after=NOW, created_at=NOW, lease=Lease(NOW, 100), cap=10_000,
            )  # fmt: skip
    read = 0
    real = flush_mod.due_rows

    def counting(*a: Any, **k: Any) -> Any:
        nonlocal read
        rows = real(*a, **k)
        read += len(rows)
        return rows

    monkeypatch.setattr(flush_mod, "due_rows", counting)
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["sent"], len(n.events("invoke"))) == (0, 3, 3)
    assert read < 2000  # the scan was quadratic: about 45 000 rows


def test_verbose_prints_one_fixed_line_per_decision(rig: Rig) -> None:
    n = one(rig, error={"code": "rate_limited"})
    seed(rig, message=f"{CANARY}-1")
    seed(rig, message=f"{CANARY}-2")
    code, _, err = rig.run("flush", "--verbose")
    assert code == 0 and len(n.events("invoke")) == 1
    lines = [x for x in err.splitlines() if x.startswith("notice-route: chat")]
    assert "notice-route: chat: row 1 queued_retry" in lines
    assert "notice-route: chat: stop after queued_retry" in lines
    assert CANARY not in err
    code, _, quiet = rig.run("flush")  # not verbose: no decision lines
    assert "row " not in quiet and "stop after" not in quiet


def test_a_signal_during_the_output_of_a_run_that_called_does_not_change_its_exit(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = one(rig)
    seed(rig)

    def interrupted(*_a: Any, **_k: Any) -> None:
        raise Interrupted(signal.SIGTERM)

    monkeypatch.setattr(cli, "_send_output", interrupted)
    code, _, _ = rig.run("flush", "--json")
    assert code == 0 and len(n.events("invoke")) == 1


def test_a_signal_during_the_output_of_a_run_that_made_no_call_is_128_plus_n(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig)

    def interrupted(*_a: Any, **_k: Any) -> None:
        raise Interrupted(signal.SIGTERM)

    monkeypatch.setattr(cli, "_send_output", interrupted)
    code, _, _ = rig.run("flush", "--json")
    assert code == 128 + signal.SIGTERM


# --- heartbeat, dry run, usage -----------------------------------------------------------------


def test_the_heartbeat_age_shown_by_validate_follows_flush_last(rig: Rig) -> None:
    one(rig)
    flush(rig)
    code, out, _ = rig.run("validate", "--json")
    assert json.loads(out)["flush_age_s"] == 0
    rig.clock.jump_wall(120)
    flush(rig, "--destination", "chat")  # not a full run: no heartbeat
    flush(rig, "--dry-run")
    assert json.loads(rig.run("validate", "--json")[1])["flush_age_s"] == 120
    flush(rig)
    assert json.loads(rig.run("validate", "--json")[1])["flush_age_s"] == 0


def test_dry_run_lists_what_is_due_and_changes_nothing(rig: Rig) -> None:
    n = one(rig)
    seed(rig)
    seed(rig, kind="held")
    seed(rig, created_at=NOW - 2 * DAY)
    before = (rig.state / "state.db").read_bytes()
    wal = (
        (rig.state / "state.db-wal").read_bytes() if (rig.state / "state.db-wal").exists() else b""
    )
    code, doc, _ = flush(rig, "--dry-run")
    d = dest(doc)
    assert (code, doc["dry_run"], d["state"], d["due_single"], d["due_held"]) == (
        0,
        True,
        "done",
        2,
        1,
    )
    assert (d["sent"], d["expired"], d["dropped"]) == (0, 0, 0)
    assert n.events("invoke") == [] and not (rig.state / "locks").exists()
    assert (rig.state / "state.db").read_bytes() == before
    after = (
        (rig.state / "state.db-wal").read_bytes() if (rig.state / "state.db-wal").exists() else b""
    )
    assert after == wal and rig.query("SELECT COUNT(*) FROM queue") == [(3,)]
    assert checked_log(rig) == []


def test_dry_run_without_a_database_creates_none(rig: Rig) -> None:
    one(rig)
    code, doc, _ = flush(rig, "--dry-run")
    assert (code, dest(doc)["state"]) == (22, "nothing_due")
    assert not rig.state.exists() or not (rig.state / "state.db").exists()


def test_dry_run_of_a_quiet_destination_with_only_postponable_rows_is_quiet(rig: Rig) -> None:
    quiet_config(rig)
    seed(rig, level="warn")
    assert dest(flush(rig, "--dry-run")[1])["state"] == "quiet"
    seed(rig, level="info")
    assert dest(flush(rig, "--dry-run")[1])["state"] == "done"


@pytest.mark.parametrize(
    "argv",
    [
        ["flush", "--config", "x.yaml"],
        ["flush", "--actor", "nobody"],
        ["flush", "--decision-id", "a", "--decision-id", "b"],
        ["flush", "--dest", "chat"],
        ["flush", "--verbose", "--quiet"],
        ["flush", "--bogus"],
    ],
)
def test_flush_usage_errors_are_exit_2_and_touch_nothing(rig: Rig, argv: list[str]) -> None:
    n = one(rig)
    seed(rig)
    code, out, err = rig.run(*argv, "--json")
    assert code == 2 and json.loads(out)["error"]["code"] == "usage"
    assert n.events("invoke") == [] and "bogus" not in out + err


def test_flush_is_refused_inside_a_hook(rig: Rig) -> None:
    n = one(rig)
    seed(rig)
    code, out, _ = rig.run("flush", "--json", env={"NOTICE_ROUTE_HOOK": "1"})
    assert code == 20 and json.loads(out)["error"]["reason"] == "hook_recursion"
    assert n.events("invoke") == [] and rig.query("SELECT COUNT(*) FROM queue") == [(1,)]


# --- wall clock --------------------------------------------------------------------------------


def test_a_wall_clock_that_steps_neither_loses_nor_repeats_a_row(rig: Rig) -> None:
    n = one(rig)
    seed(rig, deliver_after=NOW + 30)
    assert dest(flush(rig)[1])["sent"] == 0
    rig.clock.jump_wall(60)
    assert dest(flush(rig)[1])["sent"] == 1
    rig.clock.jump_wall(-3600)  # backward: the row is gone, nothing is sent again
    assert dest(flush(rig)[1])["sent"] == 0
    rig.clock.jump_wall(2 * 3600)
    assert dest(flush(rig)[1])["sent"] == 0 and len(n.events("invoke")) == 1


# --- hostile text ------------------------------------------------------------------------------


CANARY = "HOSTILE-7f3a91"


def test_hostile_row_text_and_notifier_output_never_reach_stdout_stderr_or_the_log(
    rig: Rig,
) -> None:
    n = one(
        rig,
        stdout=f'{{"outcome":"sent","x":"{CANARY}\\u001b[31m"}}',
        stderr=f"{CANARY}\x1b]0;x\x07",
    )
    seed(rig, message=f'{CANARY}\x1b[31m‮\n{{"a":1}}\u0000', title=f"{CANARY}\x1b[2J")
    code, out, err = rig.run("flush", "--json")
    doc = json.loads(out)
    assert code == 0 and dest(doc)["sent"] == 1
    assert CANARY not in out + err and "\x1b" not in out + err
    assert CANARY not in json.dumps(checked_log(rig))
    assert all(log_errors(x) == [] for x in checked_log(rig))
    assert len(n.events("invoke")) == 1


def test_hostile_error_output_from_a_notifier_is_not_echoed(rig: Rig) -> None:
    one(rig, error={"code": "rate_limited", "message": CANARY, "details": {"x": CANARY}})
    seed(rig)
    code, out, err = rig.run("flush")
    assert code == 0 and CANARY not in out + err + json.dumps(checked_log(rig))
