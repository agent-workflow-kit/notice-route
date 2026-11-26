"""``queue list``, ``queue show`` and ``queue drop`` end to end (#34): the real entry point, a real
state directory, fake notifiers; every document is validated against its schema."""

from __future__ import annotations

import json
from typing import Any

import pytest

from notice_route.schema import SCHEMA_VERSION
from tests.flush_support import checked_log, seed
from tests.manage_support import golden, make_v1_database, run_json, write
from tests.send_support import NOW, Rig
from tests.test_cli_rules_validate import schema_errors

pytestmark = pytest.mark.usefixtures("send_environment")

LEASE = 58  # twice send_worst_case_s (29: lock_wait 2 + timeout 10 + 17)
LIVE = NOW + 20  # inside the lease
HOSTILE_TITLE = '\x1b[31mred\x1b[0m ‮evil⁦ \u0085 a\nb\rc ​ 😀 "q" \\'


def one(rig: Rig, **top: Any) -> Any:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}, "other": {"notifier": rig.notifier("other")}}, **top)
    return n


def listing(rig: Rig, *argv: str) -> tuple[int, Any, str]:
    code, doc, err = run_json(rig, "queue", "list", *argv, schema="queue-list.json")
    return code, doc, err


def dataset(rig: Rig) -> None:
    """Rows 1 to 6: unclaimed, live, lapsed (expired), lapsed (beyond the lease), other
    destination, held."""
    seed(rig, title="plain", rule="r1", reason="rate_capped", attempts=2)
    seed(rig, claim="tok-live", claim_expires=LIVE)
    seed(rig, claim="tok-gone", claim_expires=NOW - 5)
    seed(rig, claim="tok-far", claim_expires=NOW + 10_000)
    seed(rig, "other", level="error", title=None, source="beta")
    seed(rig, kind="held", level="warn", deliver_after=NOW + 3600, title="held one")


# --- list ------------------------------------------------------------------------------------


def test_list_on_a_fresh_home_is_empty_and_creates_nothing(rig: Rig) -> None:
    code, doc, err = listing(rig)
    assert (code, doc, err) == (0, [], "")
    assert not rig.state.exists()
    one(rig)
    assert listing(rig)[:2] == (0, [])
    assert not (rig.state / "state.db").exists()


def test_list_with_no_config_uses_the_longest_lease(rig: Rig) -> None:
    seed(rig, claim="tok", claim_expires=NOW + 500_000)  # live under the longest lease only
    code, doc, _ = listing(rig)
    assert code == 0 and doc[0]["claim"] == "live"


def test_list_orders_by_id_and_reports_every_claim_state(rig: Rig) -> None:
    one(rig)
    dataset(rig)
    code, doc, err = listing(rig)
    assert code == 0 and err == ""
    assert [r["id"] for r in doc] == [1, 2, 3, 4, 5, 6]
    assert [r["claim"] for r in doc] == ["none", "live", "lapsed", "lapsed", "none", "none"]
    assert [r["claim_expires"] for r in doc] == [None, LIVE, NOW - 5, NOW + 10_000, None, None]
    assert doc[0]["reason"] == "rate_capped" and doc[0]["attempts"] == 2
    assert doc[0]["rule"] == "r1" and doc[5]["kind"] == "held"
    golden("queue_list", "all", doc)


def test_list_filters_and_limit(rig: Rig) -> None:
    one(rig)
    dataset(rig)
    ids = lambda *argv: [r["id"] for r in listing(rig, *argv)[1]]  # noqa: E731
    assert ids("--destination", "other") == [5]
    assert ids("--kind", "held") == [6]
    assert ids("--kind", "single", "--destination", "chat") == [1, 2, 3, 4]
    assert ids("--limit", "2") == [1, 2]
    assert ids("--destination", "nobody") == []
    code, doc, _ = listing(rig, "--limit", "1000")
    assert code == 0 and len(doc) == 6


@pytest.mark.parametrize(
    "argv",
    [
        ["--limit", "0"],
        ["--limit", "1001"],
        ["--limit", "-1"],
        ["--limit", "abc"],
        ["--limit", "1", "--limit", "2"],
        ["--limit", "01"],
        ["--destination", "Chat"],
        ["--destination", "../x"],
        ["--destination", "a" * 33],
        ["--destination", "chat", "--destination", "other"],
        ["--kind", "digest"],
        ["--kind", "held", "--kind", "single"],
        ["--bogus"],
        ["--config", "/nonexistent"],
    ],
)
def test_list_usage_errors_are_exit_2_and_never_echoed(rig: Rig, argv: list[str]) -> None:
    one(rig)
    seed(rig)
    code, doc, err = listing(rig, *argv)
    assert code == 2 and doc["error"]["code"] == "usage"
    for word in ("Chat", "../x", "bogus", "nonexistent", "digest", "abc"):
        assert word not in json.dumps(doc) + err


def test_list_text_form_never_prints_a_title_raw(rig: Rig) -> None:
    one(rig)
    seed(
        rig, title=HOSTILE_TITLE, message="BODYTEXT-canary", link="https://example.test/LINK-canary"
    )
    code, out, err = rig.run("queue", "list")
    assert code == 0 and err == ""
    assert out.count("\n") == 1 and out.isascii() and out.isprintable() is False  # one newline
    for raw in ("\x1b", "‮", "⁦", "\u0085", "\r", "​"):
        assert raw not in out
    assert "\\x1b" in out and "\\u202e" in out and "\\ud83d\\ude00" in out
    assert "BODYTEXT" not in out and "LINK-canary" not in out
    assert out.startswith("1 chat single info source=s rule=- deliver_after=")


def test_list_json_never_holds_message_link_or_claimant(rig: Rig) -> None:
    one(rig)
    seed(rig, claim="tok-claimant-xyz", claim_expires=LIVE, message="BODYTEXT-canary",
         link="https://example.test/LINK-canary", title=HOSTILE_TITLE)  # fmt: skip
    code, doc, _ = listing(rig)
    text = json.dumps(doc)
    assert code == 0 and "BODYTEXT" not in text and "LINK" not in text and "claimant" not in text
    assert doc[0]["title"] == HOSTILE_TITLE  # as stored (cut to 100): the JSON escapes it


def test_list_cuts_a_long_title_to_100_characters(rig: Rig) -> None:
    one(rig)
    seed(rig, title="t" * 5000)
    assert listing(rig)[1][0]["title"] == "t" * 100


def test_list_leaves_out_a_row_outside_its_grammar_and_warns_once(rig: Rig) -> None:
    one(rig)
    for _ in range(4):
        seed(rig)
    write(rig, "UPDATE queue SET level = 'loud' WHERE id = 1")
    write(rig, "UPDATE queue SET source = 'Bad Source' WHERE id = 2")
    write(rig, "UPDATE queue SET deliver_after = 'soon' WHERE id = 3")
    code, doc, err = listing(rig)
    assert code == 0 and [r["id"] for r in doc] == [4]
    assert err.count("warning") == 1 and "3 unreadable queue rows left out" in err
    _, _, quiet = listing(rig, "--quiet")
    assert quiet == ""


def test_list_decodes_a_title_that_is_not_utf8_with_replacement(rig: Rig) -> None:
    one(rig)
    seed(rig)
    write(rig, "UPDATE queue SET title = CAST(X'66ff6f' AS TEXT) WHERE id = 1")
    code, doc, _ = listing(rig)
    assert code == 0 and doc[0]["title"] == "f�o"


def test_list_applies_the_limit_before_the_grammar_check(rig: Rig) -> None:
    one(rig)
    seed(rig)
    seed(rig)
    write(rig, "UPDATE queue SET kind = 'held' WHERE id = 1")
    write(rig, "UPDATE queue SET level = 'loud' WHERE id = 1")
    code, doc, err = listing(rig, "--limit", "1")
    assert code == 0 and doc == [] and "1 unreadable" in err


def test_list_reads_the_clock_for_the_claim_state(rig: Rig) -> None:
    one(rig)
    seed(rig, claim="tok", claim_expires=NOW + LEASE)  # a full lease
    assert listing(rig)[1][0]["claim"] == "live"
    rig.clock.jump_wall(10)  # forward: still live
    assert listing(rig)[1][0]["claim"] == "live"
    rig.clock.jump_wall(-30)  # a step back: the expiry is beyond now plus the lease: lapsed
    assert listing(rig)[1][0]["claim"] == "lapsed"
    rig.clock.jump_wall(100)  # past the expiry
    assert listing(rig)[1][0]["claim"] == "lapsed"


def test_list_is_not_refused_inside_a_hook(rig: Rig) -> None:
    one(rig)
    seed(rig)
    code, doc, _ = listing(rig)
    code_hook, doc_hook, _ = run_json(
        rig, "queue", "list", schema="queue-list.json", env={"NOTICE_ROUTE_HOOK": "1"}
    )
    assert (code, code_hook) == (0, 0) and doc == doc_hook


# --- show ------------------------------------------------------------------------------------


def shown(rig: Rig, row_id: str) -> tuple[int, dict[str, Any], str]:
    return run_json(rig, "queue", "show", row_id, schema="queue-show.json")


def test_show_one_row_json_and_text(rig: Rig) -> None:
    one(rig)
    dataset(rig)
    code, doc, err = shown(rig, "2")
    assert (code, err) == (0, "") and doc["outcome"] == "ok" and doc["reason"] is None
    assert doc["row"]["claim"] == "live" and doc["row"]["claim_expires"] == LIVE
    golden("queue_show", "live", doc)
    code, out, err = rig.run("queue", "show", "1")
    assert code == 0 and err == ""
    assert out.splitlines()[0] == "id: 1" and "claim: none" in out and 'title: "plain"' in out
    assert "reason: rate_capped" in out and "rule: r1" in out


def test_show_never_prints_a_title_raw(rig: Rig) -> None:
    one(rig)
    seed(rig, title=HOSTILE_TITLE, message="BODYTEXT-canary")
    code, out, _ = rig.run("queue", "show", "1")
    assert code == 0 and out.isascii()
    for raw in ("\x1b", "‮", "\u0085", "\r", "​"):
        assert raw not in out
    assert "BODYTEXT" not in out
    assert shown(rig, "1")[0] == 0


def test_show_an_id_that_is_not_queued_is_exit_22(rig: Rig) -> None:
    one(rig)
    seed(rig)
    code, doc, err = shown(rig, "99")
    assert (code, err) == (22, "") and doc == {
        "outcome": "no_op",
        "reason": "not_found",
        "row": None,
    }
    golden("queue_show", "not_found", doc)
    assert rig.run("queue", "show", "99")[:2] == (22, "no_op: not_found\n")


def test_show_on_a_fresh_home_is_exit_22_and_creates_nothing(rig: Rig) -> None:
    assert shown(rig, "1")[0] == 22
    assert not rig.state.exists()


@pytest.mark.parametrize(
    "bad",
    [
        "0",
        "-1",
        "abc",
        "01",
        "1.5",
        "9223372036854775808",
        "99999999999999999999",
        "1;DROP",
        "",
        "٣",
    ],
)
def test_show_and_drop_refuse_a_bad_id_as_usage_without_echo(rig: Rig, bad: str) -> None:
    one(rig)
    seed(rig)
    for verb in ("show", "drop"):
        code, doc, err = run_json(rig, "queue", verb, bad, schema="queue-show.json")
        assert code == 2 and doc["error"]["code"] == "usage"
        assert "DROP" not in json.dumps(doc) + err and "abc" not in json.dumps(doc) + err
    assert rig.query("SELECT COUNT(*) FROM queue") == [(1,)]


def test_show_the_largest_id_is_valid_and_not_found(rig: Rig) -> None:
    one(rig)
    seed(rig)
    assert shown(rig, "9223372036854775807")[0] == 22


def test_show_of_a_row_outside_its_grammar_is_exit_1(rig: Rig) -> None:
    one(rig)
    seed(rig)
    write(rig, "UPDATE queue SET level = 'loud' WHERE id = 1")
    code, doc, err = shown(rig, "1")
    assert code == 1 and doc["error"]["code"] == "internal"
    assert "loud" not in json.dumps(doc) + err


def test_show_and_list_refuse_config_and_take_no_audit_flags(rig: Rig) -> None:
    one(rig)
    seed(rig)
    for argv in (
        ["queue", "show", "1", "--config", "/x"],
        ["queue", "show", "1", "--actor", "human:a"],
        ["queue", "show", "1", "--dry-run"],
        ["queue", "list", "--decision-id", "d"],
        ["queue", "--config", "/x", "list"],
        ["--config", "/x", "queue", "list"],
        ["queue", "show"],
        ["queue"],
        ["queue", "show", "1", "2"],
        ["queue", "list", "--json", "--json", "extra"],
    ):
        code, _, err = rig.run(*argv, "--json")
        assert code == 2 and "/x" not in err, argv


# --- drop ------------------------------------------------------------------------------------


def dropped(rig: Rig, row_id: str, *extra: str, env: dict[str, str] | None = None) -> Any:
    return run_json(rig, "queue", "drop", row_id, *extra, schema="queue-drop.json", env=env)


def deliveries(rig: Rig) -> list[tuple[Any, ...]]:
    return rig.query(
        "SELECT id, outcome, reason, code, deliver_after, attempts FROM deliveries ORDER BY id"
    )


def test_drop_an_unclaimed_row_ends_it_dropped_manual(rig: Rig) -> None:
    one(rig)
    did, qid = seed(rig, title="t", message="BODYTEXT-canary", deliver_after=NOW + 60, attempts=1)
    write(rig, "INSERT INTO dedup VALUES (1, 'chat', ?, ?, 600, ?)", ("a" * 64, NOW, did))
    write(
        rig,
        "INSERT INTO event_ids (destination, source, hook, event_id, repeat_digest, seen_at,"
        " delivery_id) VALUES ('chat', 's', '', '1', ?, ?, ?)",
        ("b" * 64, NOW, did),
    )
    write(
        rig,
        "INSERT INTO rate (destination, hour, class, count) VALUES ('chat', ?, 'other', 3)",
        (NOW - NOW % 3600,),
    )
    write(rig, "UPDATE deliveries SET dedupe = ? WHERE id = ?", ("cd" * 32, did))
    before_rate = rig.query("SELECT * FROM rate")
    code, doc, err = dropped(rig, str(qid), "--actor", "human:tester", "--decision-id", "d-1")
    assert (code, err) == (0, "")
    assert doc == {"outcome": "ok", "dry_run": False, "id": qid, "reason": None,
                   "destination": "chat", "delivery_id": did}  # fmt: skip
    golden("queue_drop", "ok", doc)
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
    assert deliveries(rig) == [(did, "dropped", "manual", None, None, 1)]
    assert rig.query("SELECT COUNT(*) FROM dedup") == [(0,)]  # released: judged afresh
    assert rig.query("SELECT COUNT(*) FROM event_ids") == [(0,)]
    assert rig.query("SELECT * FROM rate") == before_rate  # the count is not given back
    assert rig.query("SELECT COUNT(*) FROM surfaced") == [(0,)]  # nothing is surfaced
    assert "BODYTEXT" not in rig.state_text()  # the text went with the row
    (line,) = checked_log(rig)
    assert line["record"] == "delivery" and line["outcome"] == "dropped"
    assert line["reason"] == "manual" and line["destination"] == "chat" and line["attempts"] == 1
    assert line["actor"] == "human:tester" and line["decision_id"] == "d-1"
    assert line["dedupe_key_sha256"] == "cd" * 8


def test_a_dropped_row_shows_in_history_with_reason_manual(rig: Rig) -> None:
    one(rig)
    seed(rig)
    assert dropped(rig, "1")[0] == 0
    code, doc, _ = run_json(rig, "history", schema="history.json")
    assert code == 0 and doc[0]["outcome"] == "dropped" and doc[0]["reason"] == "manual"
    code, doc, _ = run_json(rig, "history", "--outcome", "dropped", schema="history.json")
    assert code == 0 and len(doc) == 1


def test_drop_text_form(rig: Rig) -> None:
    one(rig)
    seed(rig)
    seed(rig)
    assert rig.run("queue", "drop", "1", "--dry-run")[:2] == (0, "would drop 1 (chat)\n")
    assert rig.run("queue", "drop", "1")[:2] == (0, "dropped 1 (chat)\n")
    assert rig.run("queue", "drop", "1")[:2] == (22, "no_op: not_found\n")
    assert rig.run("queue", "drop", "2", "--quiet")[:2] == (0, "")


def test_drop_refuses_a_live_claim_and_changes_nothing(rig: Rig) -> None:
    one(rig)
    seed(rig, claim="tok-live", claim_expires=LIVE)
    before = (rig.query("SELECT * FROM queue"), deliveries(rig))
    code, doc, err = dropped(rig, "1")
    assert code == 20 and doc["error"]["reason"] == "claimed" and doc["error"]["code"] == "refused"
    assert "live claim" in err and "tok-live" not in json.dumps(doc) + err
    assert (rig.query("SELECT * FROM queue"), deliveries(rig)) == before
    assert rig.log_lines() == []
    assert rig.run("queue", "drop", "1")[0] == 20  # the text form refuses too


def test_drop_dry_run_also_refuses_a_live_claim(rig: Rig) -> None:
    one(rig)
    seed(rig, claim="tok-live", claim_expires=LIVE)
    assert dropped(rig, "1", "--dry-run")[0] == 20


@pytest.mark.parametrize("expires", [NOW - 5, NOW, NOW + 10_000])
def test_drop_ends_a_row_whose_claim_has_lapsed(rig: Rig, expires: int) -> None:
    one(rig)
    seed(rig, claim="tok-old", claim_expires=expires)
    code, doc, _ = dropped(rig, "1")
    assert code == 0 and doc["outcome"] == "ok"
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]


def test_drop_decides_the_claim_with_the_lease_predicate_when_the_clock_steps(rig: Rig) -> None:
    """A step back makes a claim that was a full lease from now lapsed, exactly as ``flush``
    reads it; a step forward within the lease leaves it live."""
    one(rig)
    seed(rig, claim="tok", claim_expires=NOW + LEASE)
    rig.clock.jump_wall(10)
    assert dropped(rig, "1")[0] == 20  # still live
    rig.clock.jump_wall(-30)
    assert dropped(rig, "1")[0] == 0  # lapsed by the predicate (the flusher would resend it too)


def test_drop_an_id_that_is_not_queued_is_exit_22_and_creates_nothing(rig: Rig) -> None:
    code, doc, _ = dropped(rig, "7")
    assert code == 22
    assert doc == {"outcome": "no_op", "dry_run": False, "id": 7, "reason": "not_found",
                   "destination": None, "delivery_id": None}  # fmt: skip
    assert not rig.state.exists()
    one(rig)
    seed(rig)
    assert dropped(rig, "7")[0] == 22
    assert rig.query("SELECT COUNT(*) FROM queue") == [(1,)]


def test_drop_dry_run_writes_nothing_and_creates_nothing(rig: Rig) -> None:
    code, doc, _ = dropped(rig, "1", "--dry-run")
    assert code == 22 and doc["dry_run"] is True and not rig.state.exists()
    one(rig)
    seed(rig)
    before = (rig.query("SELECT * FROM queue"), deliveries(rig))
    code, doc, _ = dropped(rig, "1", "--dry-run")
    assert code == 0 and doc["outcome"] == "ok" and doc["dry_run"] is True
    assert (rig.query("SELECT * FROM queue"), deliveries(rig)) == before
    assert rig.log_lines() == []


def test_drop_is_refused_inside_a_hook_before_anything_else(rig: Rig) -> None:
    one(rig)
    seed(rig)
    code, doc, _ = dropped(rig, "1", env={"NOTICE_ROUTE_HOOK": "1"})
    assert code == 20 and doc["error"]["reason"] == "hook_recursion"
    assert rig.query("SELECT COUNT(*) FROM queue") == [(1,)]
    # a bad flag is still usage, as for flush
    assert dropped(rig, "1", "--actor", "nobody", env={"NOTICE_ROUTE_HOOK": "1"})[0] == 2


def test_drop_refuses_config_as_usage(rig: Rig) -> None:
    one(rig)
    seed(rig)
    code, doc, err = dropped(rig, "1", "--config", "/etc/hostile")
    assert code == 2 and doc["error"]["code"] == "usage" and "hostile" not in err
    assert rig.query("SELECT COUNT(*) FROM queue") == [(1,)]


@pytest.mark.parametrize(
    "extra",
    [
        ["--actor", "root"],
        ["--actor", "human:"],
        ["--actor", "human:a b"],
        ["--decision-id", "a b"],
        ["--decision-id", "a", "--decision-id", "b"],
    ],
)
def test_drop_checks_the_audit_flags(rig: Rig, extra: list[str]) -> None:
    one(rig)
    seed(rig)
    code, doc, err = dropped(rig, "1", *extra)
    assert code == 2 and doc["error"]["code"] == "usage" and "root" not in err
    assert rig.query("SELECT COUNT(*) FROM queue") == [(1,)]


def test_drop_writes_no_log_line_when_the_config_has_log_false_or_there_is_none(rig: Rig) -> None:
    one(rig, log=False)
    seed(rig)
    assert dropped(rig, "1")[0] == 0 and rig.log_lines() == []


def test_drop_with_no_config_uses_the_longest_lease_and_writes_no_log(rig: Rig) -> None:
    seed(rig, claim="tok", claim_expires=NOW + 500_000)
    assert dropped(rig, "1")[0] == 20
    seed(rig)
    assert dropped(rig, "2")[0] == 0 and rig.log_lines() == []


def test_drop_logs_only_what_fits_the_log_grammar(rig: Rig) -> None:
    one(rig)
    seed(rig)
    write(rig, "UPDATE queue SET source = 'Not A Source' WHERE id = 1")
    assert dropped(rig, "1")[0] == 0
    assert rig.log_lines() == []  # the row is dropped; no line could be valid


def test_drop_names_no_destination_that_is_outside_the_grammar(rig: Rig) -> None:
    one(rig)
    seed(rig)
    write(rig, "UPDATE deliveries SET destination = 'Bad Name!' WHERE id = 1")
    code, doc, _ = dropped(rig, "1")
    assert code == 0 and doc["destination"] is None and "Bad" not in json.dumps(doc)


def test_drop_a_held_row_of_a_disabled_destination_works(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "enabled": False}})
    seed(rig, kind="held", level="warn")
    assert dropped(rig, "1")[0] == 0


# --- older schema ------------------------------------------------------------------------------


def test_read_only_commands_refuse_an_older_schema_and_leave_it_alone(rig: Rig) -> None:
    one(rig)
    make_v1_database(rig)
    seed_v1 = rig.state / "state.db"
    before = seed_v1.read_bytes()
    for argv in (
        ["queue", "list"],
        ["queue", "show", "1"],
        ["queue", "drop", "1", "--dry-run"],
        ["destination", "list"],
        ["destination", "disable", "chat", "--dry-run"],
        ["destination", "enable", "chat", "--dry-run"],
    ):
        code, out, err = rig.run(*argv, "--json")
        error = json.loads(out)["error"]
        assert code == 1 and error["code"] == "internal", argv
        assert "needs a migration" in error["message"], argv
    assert seed_v1.read_bytes() == before


def test_a_writing_command_migrates_an_older_schema_and_keeps_its_rows(rig: Rig) -> None:
    one(rig)
    make_v1_database(rig)
    did, qid = seed_in_v1(rig)
    code, doc, _ = dropped(rig, str(qid))
    assert code == 0
    assert rig.query("PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert rig.query("SELECT COUNT(*) FROM destination_overrides") == [(0,)]
    assert deliveries(rig)[0][:3] == (did, "dropped", "manual")


def seed_in_v1(rig: Rig) -> tuple[int, int]:
    """``seed`` opens the store for writing, which migrates: insert by hand instead."""
    import sqlite3

    conn = sqlite3.connect(rig.state / "state.db")
    try:
        cur = conn.execute(
            "INSERT INTO deliveries (ts, source, level, outcome, destination, attempts)"
            " VALUES (?, 's', 'info', 'deferred', 'chat', 0)",
            (NOW,),
        )
        did = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO queue (delivery_id, kind, message, level, source, deliver_after,"
            " created_at) VALUES (?, 'single', 'm', 'info', 's', ?, ?)",
            (did, NOW, NOW),
        )
        conn.commit()
        assert did is not None and cur.lastrowid is not None
        return did, cur.lastrowid
    finally:
        conn.close()


def test_the_row_schema_rejects_what_the_commands_would_never_print() -> None:
    row = {"id": 1, "delivery_id": 1, "destination": "chat", "kind": "single", "level": "info",
           "source": "s", "rule": None, "title": None, "deliver_after": 0, "created_at": 0,
           "attempts": 0, "reason": None, "claim": "none", "claim_expires": None}  # fmt: skip
    assert schema_errors([row], "queue-list.json") == []
    for key, bad in (("claim", "held"), ("reason", "bogus"), ("kind", "digest"), ("id", 0)):
        assert schema_errors([{**row, key: bad}], "queue-list.json") != []
    assert schema_errors([{**row, "message": "x"}], "queue-list.json") != []
