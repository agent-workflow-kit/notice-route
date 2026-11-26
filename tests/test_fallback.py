"""The fallback chain end to end (#35, docs/design.md C-63): the real entry points, fake
notifiers, a real state directory. A delivery that ends ``failed`` is handed once to the
destination named by ``fallback``; nothing else triggers it; no chain loops."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

import pytest

from notice_route.queue import Lease, enqueue, insert_delivery
from notice_route.store import open_store
from tests.flush_support import checked_log, dest, flush, seed
from tests.manage_support import make_v3_database
from tests.send_support import MORNING, NOW, Rig
from tests.test_cli_rules_validate import schema_errors

pytestmark = pytest.mark.usefixtures("send_environment")

BAD = {"error": {"code": "credential_invalid"}}  # exit 12, not retryable: ``failed``
RULE_A = [{"name": "r", "match": {"level_min": "info"}, "notify": ["a"]}]
DAY = 86400


def pair(rig: Rig, *, a: dict[str, Any] | None = None, b: dict[str, Any] | None = None) -> Any:
    """``a`` always fails and falls back to ``b``, which works; the rule notifies ``a`` only."""
    bad, good = rig.notifier("bad", **BAD), rig.notifier("good")
    rig.configure(
        {
            "a": {"notifier": bad, "fallback": "b", **(a or {})},
            "b": {"notifier": good, **(b or {})},
        },
        RULE_A,
    )
    return bad, good


def rows(rig: Rig) -> list[tuple[Any, ...]]:
    """The deliveries to a destination (the alert a failure routes has none)."""
    return rig.query(
        "SELECT id, destination, outcome, reason, code, fallback_of FROM deliveries"
        " WHERE destination IS NOT NULL ORDER BY id"
    )


def distinct_digests(rig: Rig) -> None:
    """Seeded rows share one digest; a real notification has its own."""
    conn = sqlite3.connect(rig.state / "state.db")
    conn.execute("UPDATE queue SET dedupe = printf('%064x', id)")
    conn.commit()
    conn.close()


def fallback_rows(rig: Rig) -> list[tuple[Any, ...]]:
    return rig.query(
        "SELECT id, destination, outcome, reason, fallback_of FROM deliveries"
        " WHERE fallback_of IS NOT NULL ORDER BY id"
    )


# --- the trigger ------------------------------------------------------------------------------


def test_a_failed_send_queues_one_row_for_the_fallback_and_does_not_call_it(rig: Rig) -> None:
    bad, good = pair(rig)
    code, doc, _ = rig.send("hello")
    assert code == 12 and [d["name"] for d in doc["destinations"]] == ["a"]
    assert doc["destinations"][0]["outcome"] == "failed"
    assert len(bad.events("invoke")) == 1 and good.events("invoke") == []
    assert rows(rig) == [
        (1, "a", "failed", None, "credential_invalid", None),
        (2, "b", "deferred", None, None, 1),
    ]
    assert rig.query(
        "SELECT d.destination, q.kind, q.message, q.deliver_after, q.claimed_by"
        " FROM queue q JOIN deliveries d ON d.id = q.delivery_id"
    ) == [("b", "single", "hello", NOW, None)]


def test_flush_delivers_the_fallback_once_and_never_loops(rig: Rig) -> None:
    bad, good = pair(rig)
    rig.send("hello")
    code, doc, _ = flush(rig)
    assert code == 0 and dest(doc, "b")["sent"] == 1
    for _ in range(2):  # nothing is left to do
        again, doc, _ = flush(rig)
        assert again in (0, 22)
    assert len(bad.events("invoke")) == 1 and len(good.events("invoke")) == 1
    assert [json.loads(r["text"])["message"] for r in good.events("stdin")] == ["hello"]
    assert rows(rig)[1][1:3] == ("b", "sent") and rows(rig)[1][5] == 1
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]


def test_the_fallback_delivery_is_counted_in_the_hourly_counters_of_the_sending_destination(
    rig: Rig,
) -> None:
    pair(rig)
    rig.send("hello")
    flush(rig)
    assert rig.query("SELECT destination, count FROM rate ORDER BY destination") == [
        ("a", 1),
        ("b", 1),
    ]


def test_the_fallback_gets_the_rendered_text_of_the_failed_delivery(rig: Rig) -> None:
    _, good = pair(rig)
    rig.send("hello")
    flush(rig)
    (call,) = good.events("stdin")
    sent = json.loads(call["text"])
    assert sent["message"] == "hello" and sent["level"] == "info"


def test_a_config_without_a_fallback_writes_nothing_extra(rig: Rig) -> None:
    bad = rig.notifier("bad", **BAD)
    rig.configure({"a": {"notifier": bad}}, RULE_A)
    assert rig.send("hello")[0] == 12
    assert rows(rig) == [(1, "a", "failed", None, "credential_invalid", None)]
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]


def test_an_unknown_result_does_not_fall_back(rig: Rig) -> None:
    unsure = rig.notifier("unsure", error={"code": "outcome_unknown"})
    good = rig.notifier("good")
    rig.configure({"a": {"notifier": unsure, "fallback": "b"}, "b": {"notifier": good}}, RULE_A)
    code, doc, _ = rig.send("hello")
    assert code == 12 and doc["destinations"][0]["outcome"] == "unknown"
    assert fallback_rows(rig) == [] and good.events("invoke") == []


def test_a_row_that_is_only_retried_does_not_fall_back_yet(rig: Rig) -> None:
    slow = rig.notifier("slow", error={"code": "rate_limited"})
    good = rig.notifier("good")
    rig.configure({"a": {"notifier": slow, "fallback": "b"}, "b": {"notifier": good}}, RULE_A)
    rig.send("hello")
    assert rows(rig)[0][2] == "queued_retry" and len(rows(rig)) == 1


def test_a_row_that_ends_retries_exhausted_without_a_call_falls_back(rig: Rig) -> None:
    pair(rig)
    bad, good = pair(rig)
    seed(rig, "a", attempts=5, outcome="unknown", claim="old", claim_expires=NOW - 1)
    code, doc, _ = flush(rig)
    assert code == 12 and dest(doc, "a")["failed"] == 1 and bad.events("invoke") == []
    assert [r[1] for r in fallback_rows(rig)] == ["b"]
    flush(rig)
    assert len(good.events("invoke")) == 1


def test_a_call_that_ends_retries_exhausted_falls_back(rig: Rig) -> None:
    slow = rig.notifier("slow", error={"code": "rate_limited"})
    good = rig.notifier("good")
    rig.configure({"a": {"notifier": slow, "fallback": "b"}, "b": {"notifier": good}}, RULE_A)
    seed(rig, "a", attempts=4, outcome="queued_retry")
    flush(rig)
    assert rows(rig)[0][2:5] == ("failed", None, "retries_exhausted")
    assert [r[1] for r in fallback_rows(rig)] == ["b"]


def test_a_failed_digest_member_falls_back_as_held_and_is_folded_at_the_fallback(
    rig: Rig,
) -> None:
    bad, good = pair(rig)
    for text in ("one", "two", "three"):
        seed(rig, "a", kind="held", message=text, level="warn")
    distinct_digests(rig)
    flush(rig)
    assert len(bad.events("invoke")) == 1  # one digest call
    assert [r[1] for r in fallback_rows(rig)] == ["b"] * 3
    flush(rig)
    (call,) = good.events("stdin")
    assert json.loads(call["text"])["title"] == "3 held notifications"


# --- chains -----------------------------------------------------------------------------------


def chain(rig: Rig, fails: tuple[bool, ...]) -> list[Any]:
    names = "abcd"[: len(fails)]
    made = [rig.notifier(n, **(BAD if f else {})) for n, f in zip(names, fails, strict=True)]
    dests: dict[str, Any] = {}
    for i, name in enumerate(names):
        body: dict[str, Any] = {"notifier": made[i]}
        if i + 1 < len(names):
            body["fallback"] = names[i + 1]
        dests[name] = body
    rig.configure(dests, RULE_A)
    return made


def test_a_chain_goes_on_while_each_destination_fails_and_ends_at_the_first_that_works(
    rig: Rig,
) -> None:
    a, b, c = chain(rig, (True, True, False))
    rig.send("hello")
    for _ in range(3):
        flush(rig)
    assert [len(n.events("invoke")) for n in (a, b, c)] == [1, 1, 1]
    assert [(r[1], r[2], r[5]) for r in rows(rig)] == [
        ("a", "failed", None),
        ("b", "failed", 1),
        ("c", "sent", 2),
    ]


def test_the_last_destination_of_a_chain_that_fails_ends_it(rig: Rig) -> None:
    a, b, c, d = chain(rig, (True, True, True, True))
    rig.send("hello")
    for _ in range(6):
        flush(rig)
    assert [len(n.events("invoke")) for n in (a, b, c, d)] == [1, 1, 1, 1]
    assert [r[1] for r in rows(rig)] == ["a", "b", "c", "d"]
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]


def test_an_edit_of_the_config_between_two_hops_cannot_make_a_loop(rig: Rig) -> None:
    a, b = chain(rig, (True, True))
    rig.send("hello")  # a fails, b is queued
    bad_a, bad_b = a, b
    rig.configure(  # now b falls back to a and a to nothing: valid on its own
        {"a": {"notifier": bad_a}, "b": {"notifier": bad_b, "fallback": "a"}}, RULE_A
    )
    flush(rig)  # b fails: a is on its chain, so a gets nothing
    flush(rig)
    assert len(a.events("invoke")) == 1 and len(b.events("invoke")) == 1
    assert [r[1] for r in rows(rig)] == ["a", "b"]


def make_chain(rig: Rig, destinations: list[str], *, drop_parent: bool = False) -> int:
    """Failed deliveries ``destinations[0]`` <- ... <- last, each ``fallback_of`` the one before;
    returns the id of the last, with a claimed queue row and attempts that end it on a call."""
    prior: int | None = None
    last = 0
    with open_store(rig.gateway()) as store, store.write() as conn:
        for i, name in enumerate(destinations):
            last = insert_delivery(
                conn,
                ts=NOW,
                source="s",
                level="info",
                outcome="failed" if i + 1 < len(destinations) else "deferred",
                destination=name,
                fallback_of=prior,
                dedupe="ab" * 32,
            )
            if i == 0 and drop_parent:
                conn.execute("DELETE FROM deliveries WHERE id = ?", (last,))
            prior = last
        enqueue(
            conn,
            delivery_id=last,
            kind="single",
            message="hello",
            level="info",
            source="s",
            deliver_after=NOW,
            created_at=NOW,
            lease=Lease(NOW, 100),
            dedupe="ab" * 32,
            cap=10_000,
        )
    return last


def test_a_chain_that_already_has_three_hops_gets_no_further_delivery(rig: Rig) -> None:
    notifiers = [rig.notifier(n, **(BAD if n == "d" else {})) for n in "abcde"]
    rig.configure(
        {n: {"notifier": notifiers[i]} for i, n in enumerate("abcde")}
        | {"d": {"notifier": notifiers[3], "fallback": "e"}},
        RULE_A,
    )
    make_chain(rig, ["a", "b", "c", "d"])
    code, doc, _ = flush(rig)
    assert code == 12 and dest(doc, "d")["failed"] == 1
    assert rig.query("SELECT COUNT(*) FROM deliveries WHERE destination = 'e'") == [(0,)]
    assert notifiers[4].events("invoke") == []


def test_a_chain_with_a_missing_row_gets_no_further_delivery(rig: Rig) -> None:
    bad, good = rig.notifier("bad", **BAD), rig.notifier("good")
    rig.configure(
        {"x": {"notifier": rig.notifier("x")}, "a": {"notifier": bad, "fallback": "b"}}
        | {"b": {"notifier": good}},
        RULE_A,
    )
    make_chain(rig, ["x", "a"], drop_parent=True)
    flush(rig)
    assert rig.query("SELECT COUNT(*) FROM deliveries WHERE destination = 'b'") == [(0,)]


# --- the fallback destination's own rules ------------------------------------------------------


def test_a_fallback_disabled_in_the_config_gets_a_disabled_row_and_ends_the_chain(
    rig: Rig,
) -> None:
    bad = rig.notifier("bad", **BAD)
    off, last = rig.notifier("off"), rig.notifier("last")
    rig.configure(
        {
            "a": {"notifier": bad, "fallback": "b"},
            "b": {"notifier": off, "enabled": False, "fallback": "c"},
            "c": {"notifier": last},
        },
        RULE_A,
    )
    rig.send("hello")
    flush(rig)
    assert [(r[1], r[2], r[5]) for r in rows(rig)] == [("a", "failed", None), ("b", "disabled", 1)]
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
    assert off.events("invoke") == [] and last.events("invoke") == []


def test_a_fallback_disabled_by_an_override_is_treated_the_same(rig: Rig) -> None:
    _, good = pair(rig)
    code, _, _ = rig.run("destination", "disable", "b", "--actor", "human:t")
    assert code == 0
    rig.send("hello")
    flush(rig)
    assert [(r[1], r[2]) for r in fallback_rows(rig)] == [("b", "disabled")]
    assert good.events("invoke") == []


def test_a_level_below_the_floor_of_the_fallback_is_dropped(rig: Rig) -> None:
    _, good = pair(rig, b={"floor": "error"})
    rig.send("hello")
    flush(rig)
    assert [(r[1], r[2], r[3]) for r in fallback_rows(rig)] == [("b", "dropped", "below_floor")]
    assert good.events("invoke") == []


def test_an_info_in_the_quiet_window_of_the_fallback_is_dropped(rig: Rig) -> None:
    _, good = pair(rig, b={"quiet": "night"})
    rig.send("hello")  # NOW is inside the window
    assert [(r[1], r[2], r[3]) for r in fallback_rows(rig)] == [("b", "dropped", "quiet")]
    assert good.events("invoke") == []


def test_a_warn_in_the_quiet_window_of_the_fallback_waits_for_the_end_of_it(rig: Rig) -> None:
    pair(rig, b={"quiet": "night"})
    rig.send("hello", level="warn")
    assert [(r[1], r[2], r[3]) for r in fallback_rows(rig)] == [("b", "deferred", "quiet")]
    assert rig.query("SELECT kind, deliver_after FROM queue") == [("held", MORNING)]


def test_a_notification_the_fallback_already_has_is_not_delivered_twice(rig: Rig) -> None:
    bad, good = rig.notifier("bad", **BAD), rig.notifier("good")
    rig.configure(
        {"a": {"notifier": bad, "fallback": "b"}, "b": {"notifier": good}},
        [{"name": "r", "match": {"level_min": "info"}, "notify": ["a", "b"]}],
    )
    rig.send("hello")
    flush(rig)
    assert len(good.events("invoke")) == 1
    assert fallback_rows(rig) == []


def test_the_hourly_cap_of_the_fallback_holds_the_second_delivery_back(rig: Rig) -> None:
    bad, good = pair(rig, b={"max_per_hour": 1})
    rig.send("one")
    rig.send("two")
    flush(rig)
    assert len(good.events("invoke")) == 1
    assert rig.query("SELECT COUNT(*) FROM queue") == [(1,)]
    assert rig.query("SELECT count FROM rate WHERE destination = 'b'") == [(1,)]


def test_a_missing_credential_of_the_fallback_leaves_its_row_for_flush(rig: Rig) -> None:
    _, good = pair(rig, b={"env": ["B_CRED"]})
    rig.send("hello")
    flush(rig)  # the variable is unset: nothing is called
    assert good.events("invoke") == []
    flush(rig, env={"B_CRED": "x"})
    assert len(good.events("invoke")) == 1


# --- logging, history, the other commands ------------------------------------------------------


def test_the_decision_of_the_fallback_is_logged_for_the_fallback_destination(rig: Rig) -> None:
    pair(rig)
    rig.send("hello")
    lines = checked_log(rig)
    got = [(x["destination"], x["outcome"], x["attempts"]) for x in lines]
    assert ("a", "failed", 1) in got and ("b", "deferred", 0) in got
    flush(rig)
    assert [x["outcome"] for x in checked_log(rig) if x["destination"] == "b"] == [
        "deferred",
        "sent",
    ]


def test_history_shows_what_a_fallback_row_replaced(rig: Rig) -> None:
    pair(rig)
    rig.send("hello")
    flush(rig)
    code, out, _ = rig.run("history", "--json")
    assert code == 0
    doc = json.loads(out)
    assert schema_errors(doc, "history.json") == []
    assert {(r["destination"], r["fallback_of"]) for r in doc if r["destination"]} == {
        ("a", None),
        ("b", 1),
    }


def test_test_and_dry_run_show_no_fallback(rig: Rig) -> None:
    pair(rig)
    for argv in (("test",), ("send", "--dry-run")):
        code, out, _ = rig.run(*argv, "--stdin-json", "--json", "--source", "s",
                               stdin=b'{"message": "hello"}')  # fmt: skip
        doc = json.loads(out)
        assert [d["name"] for d in doc["destinations"]] == ["a"], code
    assert not (rig.state / "state.db").exists()


def test_a_flush_dry_run_writes_nothing(rig: Rig) -> None:
    pair(rig)
    seed(rig, "a")
    flush(rig, "--dry-run")
    assert rig.query("SELECT COUNT(*) FROM deliveries") == [(1,)]
    assert rig.query("SELECT COUNT(*) FROM queue") == [(1,)]


# --- one failure, two processes ----------------------------------------------------------------


def test_two_flush_processes_make_one_fallback_row(rig: Rig) -> None:
    bad, _ = pair(rig)
    t = int(time.time())
    seed(rig, "a", now=t, deliver_after=t)
    procs = [rig.spawn("flush", "--json", "--destination", "a") for _ in range(2)]
    for proc in procs:
        proc.communicate(timeout=60)
    assert len(bad.events("invoke")) == 1
    assert [r[1] for r in fallback_rows(rig)] == ["b"]


def test_the_schema_version_is_4_and_a_version_3_database_migrates_with_its_rows(
    rig: Rig,
) -> None:
    pair(rig)
    make_v3_database(rig)
    conn = sqlite3.connect(rig.state / "state.db")
    conn.execute(
        "INSERT INTO deliveries (ts, source, level, outcome, destination)"
        " VALUES (1, 's', 'info', 'sent', 'a')"
    )
    conn.commit()
    conn.close()
    assert rig.run("history")[0] == 1  # read-only: it needs a writing command first
    rig.send("hello")
    assert rig.query("PRAGMA user_version") == [(4,)]
    assert [r[0] for r in rig.query("SELECT fallback_of FROM deliveries ORDER BY id")][:2] == [
        None,
        None,
    ]
    assert [r[1] for r in rig.query("PRAGMA table_info(deliveries)")][-1] == "fallback_of"
    assert [(r[1], r[4]) for r in fallback_rows(rig)] == [("b", 2)]


# --- faults and hostile stored data ------------------------------------------------------------


def boom_after_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    """The decision of the fallback writes a row, then fails."""
    from notice_route import fallback as fallback_mod

    real = fallback_mod.decide_fallback

    def failing(*args: Any, **kwargs: Any) -> Any:
        real(*args, **kwargs)
        raise RuntimeError("BOOM-TEXT")

    monkeypatch.setattr(fallback_mod, "decide_fallback", failing)


def test_a_fault_in_the_fallback_is_rolled_back_and_the_failure_is_still_recorded(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, good = pair(rig)
    boom_after_insert(monkeypatch)
    code, doc, err = rig.send("hello")
    assert code == 12 and doc["destinations"][0]["outcome"] == "failed"
    assert "a fallback could not be routed" in err and "BOOM-TEXT" not in err
    assert rows(rig) == [(1, "a", "failed", None, "credential_invalid", None)]
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
    assert rig.query("SELECT COUNT(*) FROM dedup WHERE destination = 'b'") == [(0,)]
    assert "BOOM-TEXT" not in rig.state_text()
    assert len(bad.events("invoke")) == 1 and good.events("invoke") == []


def test_a_fault_in_the_fallback_of_a_flush_is_one_warning_and_the_run_goes_on(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair(rig)
    seed(rig, "a")
    boom_after_insert(monkeypatch)
    code, _, err = flush(rig)
    assert code == 12 and err.count("a fallback could not be routed") == 1
    assert rows(rig)[0][2] == "failed" and fallback_rows(rig) == []


def test_a_failed_row_without_a_usable_digest_gets_no_fallback(rig: Rig) -> None:
    pair(rig)
    seed(rig, "a")
    conn = sqlite3.connect(rig.state / "state.db")
    conn.execute("UPDATE queue SET dedupe = NULL")
    conn.commit()
    conn.close()
    code, _, _ = flush(rig)
    assert code == 12 and fallback_rows(rig) == []


def test_the_labels_and_the_text_of_a_failed_notification_reach_the_fallback_unchanged(
    rig: Rig,
) -> None:
    _, good = pair(rig)
    text = "héllo 世界 " + "x" * 3000
    rig.send(text, extra=("--label", "env=prod", "--label", "team=core"))
    flush(rig)
    assert [r[0] for r in rig.query("SELECT labels FROM deliveries WHERE destination = 'b'")] == [
        '["env=prod", "team=core"]'
    ]
    (call,) = good.events("stdin")
    assert json.loads(call["text"])["message"] == text


def test_a_fallback_row_never_reaches_a_destination_twice_in_a_chain(rig: Rig) -> None:
    a, b = chain(rig, (True, True))
    rig.configure(
        {"a": {"notifier": a, "fallback": "b"}, "b": {"notifier": b}},
        [{"name": "r", "match": {"level_min": "info"}, "notify": ["a", "b"]}],
    )
    rig.send("hello")  # both fail: b's own failure has no fallback and a is not revisited
    for _ in range(3):
        flush(rig)
    assert len(a.events("invoke")) == 1 and len(b.events("invoke")) == 1
