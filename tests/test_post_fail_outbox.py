"""The ``post-fail`` outbox (#37, design C-62): a synchronous hook that fails is run again by a
later ``flush`` until it succeeds or its retry limits end it; one that succeeds runs once."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from notice_route.gateway import SYSTEM_TZ_DIRS
from notice_route.schema import MIGRATIONS, SCHEMA_VERSION, TABLES
from tests.alerts_support import MakeHook, Setup, hooks_config
from tests.flush_support import checked_log
from tests.manage_support import write
from tests.send_support import NOW, Rig

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = [
    pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database"),
    pytest.mark.usefixtures("send_environment"),
]

DELAYS = (60, 300, 1800, 7200, 21600)
DAY = 86400


def with_hooks(rig: Rig, *hooks: Any, **kw: Any) -> Setup:
    return Setup(rig, **hooks_config(*hooks, **kw))


def outbox(rig: Rig) -> list[tuple[Any, ...]]:
    return rig.query(
        "SELECT destination, attempts, next_attempt, claim_expires FROM hook_outbox ORDER BY id"
    )


def hook_log(rig: Rig) -> list[tuple[str, str]]:
    return [(x["mode"], x["result"]) for x in checked_log(rig) if x["record"] == "hook"]


# --- schema ------------------------------------------------------------------------------------


def test_the_outbox_is_migration_three_and_a_required_table() -> None:
    assert SCHEMA_VERSION == len(MIGRATIONS) == 3
    assert "hook_outbox" in TABLES


def test_a_version_two_database_is_migrated_by_a_flush_and_keeps_its_rows(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    write(rig, "DROP TABLE hook_outbox")
    write(rig, "PRAGMA user_version=2")
    code, doc, err = s.flush()
    assert code in (0, 22), (doc, err)
    assert rig.query("PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert rig.query("SELECT COUNT(*) FROM hook_outbox") == [(0,)]
    assert rig.query("SELECT COUNT(*) FROM deliveries") != [(0,)]


# --- the 1.0 behavior that must not change ---------------------------------------------------


def test_a_hook_that_succeeds_runs_once_and_leaves_no_row(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook()
    s = with_hooks(rig, h)
    s.fail("boom")
    for _ in range(3):
        rig.clock.jump_wall(DAY // 8)
        s.flush()
    assert len(h.runs()) == 1
    assert outbox(rig) == []
    assert hook_log(rig) == [("sync", "ok")]


def test_the_first_attempt_runs_in_the_flush_that_takes_the_row(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    code, doc, err = s.flush()
    assert (code, doc["outcome"]) == (0, "ok")
    assert err.count("a post-fail hook failed (nonzero_exit)") == 1
    assert len(h.runs()) == 1
    assert outbox(rig) == [("chat", 1, NOW + DELAYS[0], None)]


def test_a_flush_with_no_hook_configured_writes_no_row(rig: Rig) -> None:
    s = Setup(rig)
    s.fail("boom")
    s.flush()
    assert outbox(rig) == []


# --- retry ----------------------------------------------------------------------------------------


def test_a_hook_that_fails_is_run_again_by_a_later_flush_with_the_same_payload(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    s.flush()  # the same second: the delay has not passed
    assert len(h.runs()) == 1
    rig.clock.jump_wall(DELAYS[0] - 1)
    s.flush()
    assert len(h.runs()) == 1
    rig.clock.jump_wall(1)
    h.set(exit=0)
    code, _, err = s.flush()
    assert code in (0, 22) and err == ""
    first, second = h.runs()
    assert first["stdin"] == second["stdin"]
    assert outbox(rig) == []  # it succeeded: the row is gone
    rig.clock.jump_wall(DAY // 4)
    s.flush()
    assert len(h.runs()) == 2  # and it is not run a third time
    assert hook_log(rig) == [("sync", "nonzero_exit"), ("sync", "ok")]


def test_every_failure_warns_and_never_changes_the_exit_code(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    rig.clock.jump_wall(DELAYS[0])
    code, doc, err = s.flush()
    assert code in (0, 22) and doc["outcome"] in ("ok", "no_op")
    assert err.count("a post-fail hook failed (nonzero_exit)") == 1


def test_the_retry_delays_and_the_attempt_limit(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    t = NOW
    s.flush()
    for n, delay in enumerate(DELAYS, start=1):
        assert outbox(rig) == [("chat", n, t + delay, None)]
        rig.clock.jump_wall(delay - 1)
        s.flush()
        assert len(h.runs()) == n  # not before the delay
        rig.clock.jump_wall(1)
        t += delay
        code, _, err = s.flush()
        assert code in (0, 22)
        assert len(h.runs()) == n + 1
    assert len(h.runs()) == 6
    assert outbox(rig) == []  # the sixth failure ended it
    assert "a post-fail hook gave up (nonzero_exit)" in err
    assert "a post-fail hook failed" not in err
    assert hook_log(rig) == [("sync", "nonzero_exit")] * 6 + [("sync", "gave_up")]
    rig.clock.jump_wall(DAY)
    s.flush()
    assert len(h.runs()) == 6


def test_a_row_a_day_old_is_given_up_without_a_run(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    rig.clock.jump_wall(DAY)
    code, _, err = s.flush()
    assert code in (0, 22)
    assert len(h.runs()) == 1
    assert outbox(rig) == []
    assert err.count("a post-fail hook gave up (expired)") == 1
    assert hook_log(rig) == [("sync", "nonzero_exit"), ("sync", "gave_up")]


def test_a_row_just_under_a_day_old_still_runs(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    rig.clock.jump_wall(DAY - 1)
    s.flush()
    assert len(h.runs()) == 2


def test_only_the_failing_entry_is_retried(rig: Rig, make_hook: MakeHook) -> None:
    ok, bad = make_hook("ok"), make_hook("bad", exit=3)
    s = with_hooks(rig, ok, bad)
    s.fail("boom")
    s.flush()
    rig.clock.jump_wall(DELAYS[0])
    s.flush()
    assert (len(ok.runs()), len(bad.runs())) == (1, 2)
    assert len(outbox(rig)) == 1


def test_two_entries_with_the_same_command_are_two_rows(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h, h)
    s.fail("boom")
    s.flush()
    assert len(h.runs()) == 2 and len(outbox(rig)) == 2
    rig.clock.jump_wall(DELAYS[0])
    h.set(exit=0)
    s.flush()
    assert len(h.runs()) == 4 and outbox(rig) == []


def test_a_hook_that_cannot_be_started_is_retried(rig: Rig) -> None:
    s = Setup(rig, hooks={"post-fail": [{"cmd": "/nonexistent/hook-command"}]})
    s.fail("boom")
    s.flush()
    assert [r[1] for r in outbox(rig)] == [1]
    rig.clock.jump_wall(DELAYS[0])
    s.flush()
    assert [r[1] for r in outbox(rig)] == [2]


def test_the_retry_of_a_hook_that_times_out(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook(sleep=30)
    s = with_hooks(rig, h, timeout="1s")
    s.fail("boom")
    _, _, err = s.flush()
    assert "a post-fail hook failed (timeout)" in err
    assert [r[1] for r in outbox(rig)] == [1]
    h.kill_tracked()


# --- not outboxed --------------------------------------------------------------------------------


def test_an_async_hook_is_never_written_to_the_outbox_or_retried(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h, is_async=True)
    s.fail("boom")
    s.flush()
    h.wait_for("run")
    assert outbox(rig) == []
    rig.clock.jump_wall(DAY // 2)
    s.flush()
    assert len(h.runs()) == 1


def test_a_dry_run_runs_and_claims_nothing(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    rig.clock.jump_wall(DELAYS[0])
    before = outbox(rig)
    s.flush("--dry-run")
    assert len(h.runs()) == 1 and outbox(rig) == before


def test_a_flush_limited_to_one_destination_still_runs_due_rows(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    rig.clock.jump_wall(DELAYS[0])
    s.flush("--destination", "ops")
    assert len(h.runs()) == 2


# --- rows that are dropped unrun --------------------------------------------------------------


def test_rows_of_an_entry_that_left_the_config_are_deleted_unrun(
    rig: Rig, make_hook: MakeHook
) -> None:
    gone, kept = make_hook("gone", exit=3), make_hook("kept", exit=3)
    s = with_hooks(rig, gone, kept)
    s.fail("boom")
    s.flush()
    assert len(outbox(rig)) == 2
    rig.configure(
        {"chat": {"notifier": s.bad}, "ops": {"notifier": s.ops}},
        [s_rule for s_rule in _rules()],
        **hooks_config(kept),
    )
    rig.clock.jump_wall(DELAYS[0])
    s.flush()
    assert len(gone.runs()) == 1 and len(kept.runs()) == 2
    assert len(outbox(rig)) == 1


def test_rows_of_an_entry_that_became_async_are_deleted_unrun(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    rig.configure(
        {"chat": {"notifier": s.bad}, "ops": {"notifier": s.ops}},
        _rules(),
        **hooks_config(h, is_async=True),
    )
    rig.clock.jump_wall(DELAYS[0])
    s.flush()
    assert len(h.runs()) == 1 and outbox(rig) == []


def test_rows_of_a_destination_that_left_the_config_are_deleted_unrun(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    rig.configure({"ops": {"notifier": s.ops}}, [_rules()[0]], **hooks_config(h))
    rig.clock.jump_wall(DELAYS[0])
    s.flush()
    assert len(h.runs()) == 1 and outbox(rig) == []


def _rules() -> list[dict[str, Any]]:
    from tests.alerts_support import ALERT_RULE, SEND_RULE

    return [ALERT_RULE, SEND_RULE]


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        "null",
        '{"tool":"notice-route"}',
        "x" * 9000,
        json.dumps({"tool": "other"}),
    ],
)
def test_a_stored_payload_that_does_not_fit_is_deleted_unrun(
    rig: Rig, make_hook: MakeHook, payload: str
) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    write(rig, "UPDATE hook_outbox SET payload = ?", (payload,))
    rig.clock.jump_wall(DELAYS[0])
    code, _, err = s.flush()
    assert code in (0, 22) and len(h.runs()) == 1 and outbox(rig) == []
    assert payload[:20] not in err


def test_a_payload_with_a_field_changed_is_deleted_unrun(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    (stored,) = rig.query("SELECT payload FROM hook_outbox")
    forged = json.loads(stored[0])
    forged["notification_source"] = "Not A Source!"
    write(rig, "UPDATE hook_outbox SET payload = ?", (json.dumps(forged),))
    rig.clock.jump_wall(DELAYS[0])
    s.flush()
    assert len(h.runs()) == 1 and outbox(rig) == []


def test_a_payload_for_another_destination_than_its_row_is_deleted_unrun(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    write(rig, "UPDATE hook_outbox SET destination = 'ops'")
    rig.clock.jump_wall(DELAYS[0])
    s.flush()
    assert len(h.runs()) == 1 and outbox(rig) == []


# --- claims ------------------------------------------------------------------------------------


def test_a_live_claim_keeps_a_row_from_running_and_a_lapsed_one_does_not(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    rig.clock.jump_wall(DELAYS[0])
    write(rig, "UPDATE hook_outbox SET claimed_by = 'other', claim_expires = ?", (NOW + 10_000,))
    s.flush()
    assert len(h.runs()) == 1
    write(rig, "UPDATE hook_outbox SET claim_expires = ?", (NOW + DELAYS[0] - 1,))
    s.flush()
    assert len(h.runs()) == 2  # a lapsed claim is a crashed run: tried again


def test_an_attempt_that_crashed_still_counts(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    write(
        rig,
        "UPDATE hook_outbox SET attempts = 5, claimed_by = 'dead', claim_expires = ?",
        (NOW + 1,),
    )
    rig.clock.jump_wall(DELAYS[0])
    _, _, err = s.flush()
    assert len(h.runs()) == 2 and outbox(rig) == []
    assert "gave up" in err


def test_a_row_at_the_attempt_limit_is_given_up_without_a_run(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    write(rig, "UPDATE hook_outbox SET attempts = 6")
    rig.clock.jump_wall(DELAYS[0])
    _, _, err = s.flush()
    assert len(h.runs()) == 1 and outbox(rig) == []
    assert "a post-fail hook gave up (nonzero_exit)" not in err
    assert err.count("a post-fail hook gave up") == 1


def test_two_flushers_run_a_due_row_once(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook(exit=3, sleep=0.3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    assert len(h.runs()) == 1
    # the spawned processes read the real clock: make the row due and young by it
    write(rig, "UPDATE hook_outbox SET created_at = ?, next_attempt = 0", (int(time.time()),))
    first = rig.spawn("flush", "--json")
    second = rig.spawn("flush", "--json")
    for p in (first, second):
        p.communicate(timeout=60)
    assert len(h.runs()) == 2


def test_a_hook_that_succeeds_while_another_flusher_waits_runs_once(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(sleep=0.3)
    s = with_hooks(rig, h)
    s.fail("boom")
    first = rig.spawn("flush", "--json")
    second = rig.spawn("flush", "--json")
    for p in (first, second):
        p.communicate(timeout=60)
    assert len(h.runs()) == 1 and outbox(rig) == []


# --- take transaction -------------------------------------------------------------------------


def test_a_flush_killed_after_taking_the_row_loses_no_sync_hook(
    rig: Rig, make_hook: MakeHook, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notice_route import flush as flush_mod

    h = make_hook()
    s = with_hooks(rig, h)
    s.fail("boom")

    def die(*_a: Any, **_k: Any) -> Any:
        raise KeyboardInterrupt  # the process ends right after the row was taken

    monkeypatch.setattr(flush_mod, "_run_outbox", die)
    with pytest.raises(KeyboardInterrupt):
        s.flush()
    monkeypatch.undo()
    assert s.surfaced()[0][4] == 1 and h.runs() == []
    assert len(outbox(rig)) == 1  # taken and durable
    code, _, _ = s.flush()
    assert code in (0, 22) and len(h.runs()) == 1 and outbox(rig) == []


def test_a_fault_of_the_alert_routing_keeps_the_hook_in_the_outbox(
    rig: Rig, make_hook: MakeHook, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notice_route import alerts

    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")

    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("routing fault")

    monkeypatch.setattr(alerts, "_route", broken)
    code, _, err = s.flush()
    assert code in (0, 22) and len(h.runs()) == 1
    assert len(outbox(rig)) == 1 and s.surfaced()[0][4] == 1
    assert err.count("a post-fail hook failed") == 1


def test_an_alerts_own_failure_runs_its_hook_through_the_outbox(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(exit=3)
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
    assert sources == ["s", "notice-route"]
    assert len(outbox(rig)) == 2


# --- overrides and limits ---------------------------------------------------------------------


def test_a_destination_disabled_by_an_override_still_has_its_hook_retried(
    rig: Rig, make_hook: MakeHook
) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    code, _, _ = rig.run("destination", "disable", "chat", "--json")
    assert code in (0, 22)
    rig.clock.jump_wall(DELAYS[0])
    s.flush()
    assert len(h.runs()) == 2


def test_a_run_takes_at_most_one_hundred_rows(rig: Rig, make_hook: MakeHook) -> None:
    h = make_hook(exit=3)
    s = with_hooks(rig, h)
    s.fail("boom")
    s.flush()
    (row,) = rig.query(
        "SELECT destination, hook_key, event_id, payload, created_at, next_attempt"
        " FROM hook_outbox"
    )
    for i in range(1, 106):
        write(
            rig,
            "INSERT INTO hook_outbox (surfaced_id, destination, hook_key, event_id, payload,"
            " created_at, attempts, next_attempt) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (1000 + i, *row),
        )
    rig.clock.jump_wall(DELAYS[0])
    s.flush()
    assert len(h.runs()) == 1 + 100
    s.flush()  # the same second: the 100 are not due again, the other 6 are
    assert len(h.runs()) == 1 + 100 + 6
