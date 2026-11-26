"""``destination list|enable|disable`` end to end (#34): the override table, its effect on
``send``, ``test``, ``flush`` and ``prune``, the no-op rules, migration 2 and the refusals."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from typing import Any

import pytest

from notice_route.schema import SCHEMA_VERSION
from tests.flush_support import checked_log, dest, flush, seed
from tests.manage_support import golden, make_v1_database, run_json, write
from tests.send_support import NOW, Rig, log_errors
from tests.test_cli_rules_validate import schema_errors

pytestmark = pytest.mark.usefixtures("send_environment")

DAY = 86400


def trio(rig: Rig, **top: Any) -> dict[str, Any]:
    """chat and other are enabled; off has ``enabled: false`` in the config."""
    made = {name: rig.notifier(name) for name in ("chat", "other", "off")}
    rig.configure(
        {
            "chat": {"notifier": made["chat"]},
            "other": {"notifier": made["other"]},
            "off": {"notifier": made["off"], "enabled": False},
        },
        **top,
    )
    return made


def lst(rig: Rig, *argv: str, env: dict[str, str] | None = None) -> tuple[int, Any, str]:
    return run_json(rig, "destination", "list", *argv, schema="destination-list.json", env=env)


def toggle(
    rig: Rig, action: str, name: str, *extra: str, env: dict[str, str] | None = None
) -> tuple[int, Any, str]:
    return run_json(
        rig, "destination", action, name, *extra, schema=f"destination-{action}.json", env=env
    )


def overrides(rig: Rig) -> list[tuple[Any, ...]]:
    return rig.query("SELECT * FROM destination_overrides ORDER BY destination")


# --- list ------------------------------------------------------------------------------------


def test_list_shows_config_and_effective_state_and_creates_nothing(rig: Rig) -> None:
    trio(rig)
    code, doc, err = lst(rig)
    assert (code, err) == (0, "")
    assert [
        (d["name"], d["config_enabled"], d["override"], d["enabled"]) for d in doc["destinations"]
    ] == [
        ("chat", True, None, True),
        ("other", True, None, True),
        ("off", False, None, False),
    ]
    golden("destination_list", "plain", doc)
    assert not rig.state.exists()
    code, out, _ = rig.run("destination", "list")
    assert (
        code == 0
        and out.splitlines()[0]
        == "chat slack-notice config_enabled=true override=none enabled=true"
    )


def test_list_with_no_config_is_empty_and_exit_22(rig: Rig) -> None:
    code, doc, err = lst(rig)
    assert code == 22 and doc == {"destinations": []} and "no config" in err
    assert lst(rig, "--quiet")[2] == ""


def test_list_is_not_refused_inside_a_hook_and_refuses_config(rig: Rig) -> None:
    trio(rig)
    assert lst(rig, env={"NOTICE_ROUTE_HOOK": "1"})[0] == 0
    code, doc, _ = lst(rig, "--config", "/etc/x")
    assert code == 2 and doc["error"]["code"] == "usage"


# --- disable and enable ----------------------------------------------------------------------


def test_disable_then_enable_round_trip(rig: Rig) -> None:
    trio(rig)
    code, doc, err = toggle(
        rig, "disable", "chat", "--actor", "human:tester", "--decision-id", "d-9"
    )
    assert (code, err) == (0, "")
    assert doc == {"outcome": "ok", "dry_run": False, "destination": "chat", "action": "disabled",
                   "reason": None, "config_enabled": True, "override": "disabled",
                   "enabled": False}  # fmt: skip
    golden("destination_disable", "ok", doc)
    assert overrides(rig) == [("chat", "disabled", "human:tester", "d-9", NOW)]
    code, doc, _ = lst(rig)
    chat = dest_of(doc, "chat")
    assert (chat["override"], chat["enabled"], chat["config_enabled"]) == ("disabled", False, True)
    assert (chat["override_actor"], chat["override_set_at"]) == ("human:tester", NOW)
    golden("destination_list", "overridden", doc)
    assert dest_of(doc, "other")["override"] is None

    code, doc, _ = toggle(rig, "disable", "chat")  # again: nothing to do
    assert code == 22 and doc["outcome"] == "no_op" and doc["reason"] == "already_disabled"
    assert doc["override"] == "disabled" and doc["enabled"] is False
    golden("destination_disable", "already_disabled", doc)
    assert overrides(rig)[0][2] == "human:tester"  # the first actor stays

    code, doc, _ = toggle(rig, "enable", "chat", "--actor", "human:tester")
    assert code == 0 and doc["action"] == "enabled" and doc["override"] is None
    assert doc["enabled"] is True
    golden("destination_enable", "ok", doc)
    assert overrides(rig) == []
    code, doc, _ = toggle(rig, "enable", "chat")
    assert code == 22 and doc["reason"] == "not_disabled" and doc["enabled"] is True
    golden("destination_enable", "not_disabled", doc)

    lines = checked_log(rig)
    assert [(r["record"], r["destination"], r["override"]) for r in lines] == [
        ("override", "chat", "disabled"),
        ("override", "chat", "cleared"),
    ]
    assert lines[0]["actor"] == "human:tester" and lines[0]["decision_id"] == "d-9"
    assert all(r["source"] is None and r["outcome"] is None for r in lines)


def dest_of(doc: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [d for d in doc["destinations"] if d["name"] == name]
    return found


def test_text_forms(rig: Rig) -> None:
    trio(rig)
    assert rig.run("destination", "disable", "chat", "--dry-run")[:2] == (
        0,
        "disabled chat (dry run)\n",
    )
    assert rig.run("destination", "disable", "chat")[:2] == (0, "disabled chat\n")
    assert rig.run("destination", "disable", "chat")[:2] == (22, "no_op chat: already_disabled\n")
    out = rig.run("destination", "list")[1]
    assert "chat slack-notice config_enabled=true override=disabled enabled=false by=" in out
    assert rig.run("destination", "enable", "chat", "--quiet")[:2] == (0, "")
    assert rig.run("destination", "enable", "chat")[:2] == (22, "no_op chat: not_disabled\n")


def test_the_config_wins_enable_and_disable_of_a_config_disabled_destination_are_no_ops(
    rig: Rig,
) -> None:
    trio(rig)
    for action in ("enable", "disable"):
        code, doc, _ = toggle(rig, action, "off")
        assert code == 22 and doc["outcome"] == "no_op" and doc["reason"] == "config_disabled"
        assert (doc["config_enabled"], doc["override"], doc["enabled"]) == (False, None, False)
        golden(f"destination_{action}", "config_disabled", doc)
    assert not (rig.state / "state.db").exists()  # nothing was created for a no-op
    # an override that is already there is left as it is, and enable cannot lift the config
    assert toggle(rig, "disable", "chat")[0] == 0
    write(rig, "INSERT INTO destination_overrides VALUES ('off', 'disabled', 'human:x', NULL, 5)")
    code, doc, _ = toggle(rig, "enable", "off")
    assert code == 22 and doc["reason"] == "config_disabled" and doc["override"] == "disabled"
    assert doc["enabled"] is False
    assert [r[0] for r in overrides(rig)] == ["chat", "off"]
    assert dest_of(lst(rig)[1], "off")["enabled"] is False


def test_dry_run_changes_nothing_and_reports_what_a_run_would(rig: Rig) -> None:
    trio(rig)
    code, doc, _ = toggle(rig, "disable", "chat", "--dry-run")
    assert code == 0 and doc["dry_run"] is True and doc["action"] == "disabled"
    assert doc["override"] == "disabled" and doc["enabled"] is False
    assert not rig.state.exists()
    assert toggle(rig, "disable", "chat")[0] == 0
    before = rig.query("SELECT * FROM destination_overrides")
    code, doc, _ = toggle(rig, "enable", "chat", "--dry-run")
    assert code == 0 and doc["action"] == "enabled" and doc["override"] is None
    assert toggle(rig, "disable", "chat", "--dry-run")[1]["reason"] == "already_disabled"
    assert rig.query("SELECT * FROM destination_overrides") == before
    assert len(rig.log_lines()) == 1  # only the real disable was logged


def test_enable_creates_nothing_and_disable_creates_the_database(rig: Rig) -> None:
    trio(rig)
    assert toggle(rig, "enable", "chat")[0] == 22
    assert not rig.state.exists()
    assert toggle(rig, "disable", "chat")[0] == 0
    assert (rig.state / "state.db").exists()


def test_no_config_and_a_disabled_router_are_exit_22(rig: Rig) -> None:
    code, doc, err = toggle(rig, "disable", "chat")
    assert code == 22 and doc["reason"] == "unconfigured" and "no config" in err
    assert (doc["config_enabled"], doc["override"], doc["enabled"]) == (None, None, None)
    golden("destination_disable", "unconfigured", doc)
    assert not rig.state.exists()
    trio(rig, enabled=False)
    for name in ("chat", "nobody"):  # whatever the name says
        code, doc, _ = toggle(rig, "enable", name)
        assert code == 22 and doc["reason"] == "disabled"
    assert not rig.state.exists()


def test_a_disabled_router_without_destinations_is_disabled_not_unconfigured(rig: Rig) -> None:
    rig.configure({}, rules=[], enabled=False)
    for action in ("enable", "disable"):
        code, doc, _ = toggle(rig, action, "chat")
        assert code == 22 and doc["reason"] == "disabled"
    assert not rig.state.exists()


@pytest.mark.parametrize(
    "name",
    ["nobody", "Chat", "../chat", "chat;x", "a" * 33, "-chat", "chat\n", "ch at", "é", "CHAT-x"],
)
def test_a_bad_or_unknown_name_is_usage_and_never_echoed(rig: Rig, name: str) -> None:
    trio(rig)
    for action in ("enable", "disable"):
        code, doc, err = toggle(rig, action, name)
        assert code == 2 and doc["error"]["code"] == "usage"
        assert name.strip() not in json.dumps(doc) + err
    assert not rig.state.exists()


def test_a_missing_or_extra_name_is_usage(rig: Rig) -> None:
    trio(rig)
    for argv in (
        ["destination", "disable"],
        ["destination", "disable", "chat", "other"],
        ["destination"],
    ):
        assert rig.run(*argv, "--json")[0] == 2


def test_refusals_hook_config_and_audit_flags(rig: Rig) -> None:
    trio(rig)
    for action in ("enable", "disable"):
        code, doc, _ = toggle(rig, action, "chat", env={"NOTICE_ROUTE_HOOK": "1"})
        assert code == 20 and doc["error"]["reason"] == "hook_recursion"
        code, doc, err = toggle(rig, action, "chat", "--config", "/etc/hostile")
        assert code == 2 and "hostile" not in err
        for bad in (
            ["--actor", "root"],
            ["--decision-id", "a b"],
            ["--decision-id", "a", "--decision-id", "b"],
        ):
            assert toggle(rig, action, "chat", *bad)[0] == 2
    assert not rig.state.exists()


def test_log_false_writes_no_line(rig: Rig) -> None:
    trio(rig, log=False)
    assert toggle(rig, "disable", "chat")[0] == 0 and rig.log_lines() == []


def test_a_hand_edited_override_row_fails_closed_and_shows_only_what_fits(rig: Rig) -> None:
    trio(rig)
    assert toggle(rig, "disable", "chat")[0] == 0
    write(rig, "UPDATE destination_overrides SET actor = 'ro\x1b[0mot', set_at = 'soon'")
    code, doc, _ = lst(rig)
    chat = dest_of(doc, "chat")
    assert code == 0 and chat["override"] == "disabled" and chat["enabled"] is False
    assert chat["override_actor"] is None and chat["override_set_at"] is None


def test_an_override_of_a_destination_that_left_the_config_is_not_listed(rig: Rig) -> None:
    trio(rig)
    write_after = toggle(rig, "disable", "chat")
    assert write_after[0] == 0
    write(rig, "INSERT INTO destination_overrides VALUES ('gone', 'disabled', 'human:x', NULL, 5)")
    assert [d["name"] for d in lst(rig)[1]["destinations"]] == ["chat", "other", "off"]


# --- the override is honored ---------------------------------------------------------------------


def test_send_treats_an_overridden_destination_as_disabled(rig: Rig) -> None:
    n = trio(rig)
    assert toggle(rig, "disable", "chat")[0] == 0
    code, doc, _ = rig.send("hello")
    assert code == 0
    by = {d["name"]: d for d in doc["destinations"]}
    assert by["chat"]["outcome"] == "disabled" and by["chat"]["reason"] is None
    assert by["other"]["outcome"] == "sent" and by["off"]["outcome"] == "disabled"
    assert n["chat"].events("invoke") == [] and len(n["other"].events("invoke")) == 1
    assert rig.query("SELECT outcome, reason FROM deliveries WHERE destination = 'chat'") == [
        ("disabled", None)
    ]
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]


def test_send_with_every_destination_disabled_is_all_disabled(rig: Rig) -> None:
    n = trio(rig)
    assert toggle(rig, "disable", "chat")[0] == 0 and toggle(rig, "disable", "other")[0] == 0
    code, doc, _ = rig.send("hello")
    assert code == 22 and doc["outcome"] == "no_op" and doc["reason"] == "all_disabled"
    assert all(len(x.events("invoke")) == 0 for x in n.values())


def test_test_and_dry_run_show_the_overridden_destination_as_disabled(rig: Rig) -> None:
    trio(rig)
    assert toggle(rig, "disable", "chat")[0] == 0
    body = json.dumps({"message": "hello"}).encode()
    for argv in (["test"], ["send", "--dry-run"]):
        code, out, _ = rig.run(*argv, "--stdin-json", "--source", "s", "--json", stdin=body)
        doc = json.loads(out)
        assert code == 0 and schema_errors(doc, "test.json") == [], doc
        by = {d["name"]: d["decision"] for d in doc["destinations"]}
        assert by == {"chat": "disabled", "other": "send", "off": "disabled"}
    assert toggle(rig, "enable", "chat")[0] == 0
    code, out, _ = rig.run("test", "--stdin-json", "--source", "s", "--json", stdin=body)
    assert {d["name"]: d["decision"] for d in json.loads(out)["destinations"]}["chat"] == "send"


def test_flush_leaves_the_rows_of_an_overridden_destination_alone(rig: Rig) -> None:
    n = trio(rig)
    seed(rig, "chat", message="one")
    seed(rig, "other", message="two")
    assert toggle(rig, "disable", "chat")[0] == 0
    code, doc, _ = flush(rig)
    assert dest(doc, "chat")["state"] == "disabled" and dest(doc, "chat")["sent"] == 0
    assert dest(doc, "other")["sent"] == 1
    assert n["chat"].events("invoke") == []
    assert rig.query(
        "SELECT q.id, q.claimed_by, d.outcome FROM queue q"
        " JOIN deliveries d ON d.id = q.delivery_id"
    ) == [(1, None, "deferred")]
    code, doc, _ = flush(rig, "--dry-run")
    assert dest(doc, "chat")["state"] == "disabled" and dest(doc, "chat")["due_single"] == 0
    assert toggle(rig, "enable", "chat")[0] == 0
    code, doc, _ = flush(rig)
    assert dest(doc, "chat")["sent"] == 1 and len(n["chat"].events("invoke")) == 1


def test_a_disable_during_a_flush_stops_its_next_claim(rig: Rig) -> None:
    n = trio(rig)
    n["chat"].set(sleep=1.5)
    now = int(time.time())
    for i in range(3):
        seed(rig, "chat", message=f"m{i}", now=now, deliver_after=now)
    proc = rig.spawn("flush", "--json")
    n["chat"].wait_for("invoke", timeout=30)
    assert toggle(rig, "disable", "chat")[0] == 0  # commits while the first call is running
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert len(n["chat"].events("invoke")) == 1  # the call in flight finished, no further claim
    assert rig.query("SELECT COUNT(*), COUNT(claimed_by) FROM queue") == [(2, 0)]
    assert json.loads(out)["destinations"][0]["sent"] == 1


def test_prune_surfaces_an_expired_row_only_for_an_enabled_destination(rig: Rig) -> None:
    trio(rig)
    old = NOW - 3 * DAY
    seed(rig, "chat", created_at=old, now=old)
    seed(rig, "other", created_at=old, now=old)
    assert toggle(rig, "disable", "chat")[0] == 0
    code, out, _ = rig.run("prune", "--json")
    assert code == 0 and json.loads(out)["deleted"]["queue"] == 2
    assert rig.query("SELECT destination FROM surfaced") == [("other",)]


def test_validate_and_rules_keep_working_with_overrides(rig: Rig) -> None:
    trio(rig)
    assert toggle(rig, "disable", "chat")[0] == 0
    code, doc, _ = run_json(rig, "validate", schema="validate.json")
    assert code == 0
    assert schema_errors(run_json(rig, "rules", schema="rules.json")[1], "rules.json") == []
    dests = run_json(rig, "rules", schema="rules.json")[1]["destinations"]
    assert [d["enabled"] for d in dests] == [True, True, False]  # config only


# --- migration 2 ----------------------------------------------------------------------------


def test_disable_migrates_an_older_database_and_keeps_its_rows(rig: Rig) -> None:
    trio(rig)
    seed(rig, "chat")
    make_v1_database(rig)
    assert rig.query("PRAGMA user_version") == [(1,)]
    rows_before = rig.query("SELECT * FROM queue") + rig.query("SELECT * FROM deliveries")
    code, doc, _ = toggle(rig, "disable", "chat")
    assert code == 0
    assert rig.query("PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert rig.query("SELECT * FROM queue") + rig.query("SELECT * FROM deliveries") == rows_before
    assert rig.query("PRAGMA integrity_check") == [("ok",)]


def test_enable_migrates_an_older_database_that_exists(rig: Rig) -> None:
    trio(rig)
    make_v1_database(rig)
    code, doc, _ = toggle(rig, "enable", "chat")
    assert code == 22 and doc["reason"] == "not_disabled"
    assert rig.query("PRAGMA user_version") == [(SCHEMA_VERSION,)]


@pytest.mark.parametrize("start", ["v1", "fresh"])
def test_concurrent_first_use_by_real_processes_migrates_once(rig: Rig, start: str) -> None:
    trio(rig)
    if start == "v1":
        make_v1_database(rig)
    now = int(time.time())
    seed_rows = start == "v1"
    if seed_rows:
        conn = sqlite3.connect(rig.state / "state.db")
        conn.execute(
            "INSERT INTO deliveries (ts, source, level, outcome, destination)"
            " VALUES (?, 's', 'info', 'sent', 'chat')",
            (now,),
        )
        conn.commit()
        conn.close()
    procs: list[subprocess.Popen[bytes]] = [
        rig.spawn("destination", "disable", "chat", "--json") for _ in range(4)
    ]
    results = [(p.communicate(timeout=60), p.returncode) for p in procs]
    codes = sorted(code for _, code in results)
    assert codes == [0, 22, 22, 22], results  # exactly one wrote the override
    assert rig.query("PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert rig.query("PRAGMA integrity_check") == [("ok",)]
    assert len(overrides(rig)) == 1
    if seed_rows:
        assert rig.query("SELECT COUNT(*) FROM deliveries") == [(1,)]
    for (out, _), _code in results:
        assert schema_errors(json.loads(out), "destination-disable.json") == []


def test_the_log_schema_accepts_the_override_record_and_rejects_a_bad_one() -> None:
    ok = {
        "ts": "2026-01-01T00:00:00.000Z", "tool": "notice-route", "version": "0",
        "record": "override",
        "source": None, "hook": None, "rule": None, "destination": "chat", "level": None,
        "outcome": None, "reason": None, "code": None, "attempts": None, "title_sha256": None,
        "message_sha256": None, "event_id": None, "dedupe_key_sha256": None, "actor": "human:a",
        "decision_id": None, "due": None, "sent": None, "queued_retry": None, "failed": None,
        "unknown": None, "dropped": None, "expired": None, "duration_ms": 0, "mode": None,
        "result": None, "override": "cleared",
    }  # fmt: skip
    assert log_errors(ok) == []
    assert log_errors({**ok, "override": None}) != []
    assert log_errors({**ok, "destination": None}) != []
    assert log_errors({**ok, "override": "enabled"}) != []
    assert log_errors({**ok, "record": "delivery"}) != []
