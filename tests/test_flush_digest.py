"""Digests end to end (#24): due ``held`` rows of one destination folded into one notification,
through the real ``flush`` entry point, fake notifiers and a real state directory."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

import pytest

from notice_route.decide import hour_start
from notice_route.notify import CallResult, call_notifier
from notice_route.queue import Lease, claim_row
from notice_route.send import Seams, SurfaceEvent
from notice_route.store import open_store
from tests import sibling_schemas as ss
from tests.flush_support import checked_log, dest, flush, seed
from tests.send_support import NOW, Rig
from tests.test_flush import _HAVE_ZONES, hold, invoked, one, quiet_config, states, use_seams

pytestmark = [
    pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database"),
    pytest.mark.usefixtures("send_environment"),
]

HOUR = hour_start(NOW)


def held(rig: Rig, count: int = 2, **kw: Any) -> list[int]:
    """``count`` due held rows (ids returned); ``message`` and ``title`` may use ``{i}``."""
    ids: list[int] = []
    for i in range(count):
        extra: dict[str, Any] = {
            k: v.replace("{i}", str(i)) if isinstance(v, str) else v for k, v in kw.items()
        }
        extra.setdefault("level", "warn")
        extra.setdefault("message", f"m{i}")
        extra.setdefault("title", f"t{i}")
        ids.append(seed(rig, kind="held", **extra)[1])
    return ids


def payloads(n: Any) -> list[dict[str, Any]]:
    return [json.loads(x) for x in invoked(n)]


def rate(rig: Rig) -> list[tuple[Any, ...]]:
    return rig.query("SELECT class, count FROM rate WHERE destination = 'chat' ORDER BY class")


def set_rate(rig: Rig, klass: str, count: int) -> None:
    conn = sqlite3.connect(rig.state / "state.db")
    conn.execute(
        "INSERT INTO rate (destination, hour, class, count) VALUES ('chat', ?, ?, ?)",
        (HOUR, klass, count),
    )
    conn.commit()
    conn.close()


# --- formation ---------------------------------------------------------------------------------


def test_one_due_held_row_is_sent_as_itself_not_as_a_digest(rig: Rig) -> None:
    n = one(rig)
    held(rig, 1, title="Solo", message="alone", link="https://h.example/x")
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["sent"], dest(doc)["digests"]) == (0, 1, 0)
    assert payloads(n) == [
        {"title": "Solo", "message": "alone", "level": "warn", "link": "https://h.example/x"}
    ]
    assert rig.query("SELECT reason FROM deliveries") == [(None,)]


def test_two_due_held_rows_become_one_digest_with_the_exact_text(rig: Rig) -> None:
    n = one(rig)
    held(rig, 2, source="build", rule="r")
    code, doc, _ = flush(rig)
    d = dest(doc)
    assert (code, d["state"], d["sent"], d["digests"]) == (0, "done", 2, 1)
    assert payloads(n) == [
        {
            "title": "2 held notifications",
            "message": "warn build: t0 - m0\nwarn build: t1 - m1",
            "level": "warn",
        }
    ]
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]


@pytest.mark.parametrize(
    ("count", "lines", "tail"), [(10, 10, None), (11, 10, "and 1 more"), (25, 10, "and 15 more")]
)
def test_the_digest_for_10_11_and_25_rows(
    rig: Rig, count: int, lines: int, tail: str | None
) -> None:
    n = one(rig)
    held(rig, count)
    code, doc, _ = flush(rig)
    (body,) = payloads(n)
    got = body["message"].splitlines()
    assert body["title"] == f"{count} held notifications"
    assert len(got) == lines + (1 if tail else 0) and (tail is None or got[-1] == tail)
    assert (code, dest(doc)["sent"], dest(doc)["digests"]) == (0, count, 1)
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]


def test_the_digest_is_ordered_by_level_then_age_and_takes_the_highest_level(rig: Rig) -> None:
    n = one(rig)
    for level in ("info", "warn", "error", "action", "error", "warn"):
        held(rig, 1, level=level, title=f"{level}-{{i}}", message="x")
    flush(rig)
    (body,) = payloads(n)
    assert body["level"] == "error"
    assert body["message"].splitlines() == [
        "error s: error-0 - x",
        "error s: error-0 - x",
        "action s: action-0 - x",
        "warn s: warn-0 - x",
        "warn s: warn-0 - x",
        "info s: info-0 - x",
    ]


def test_a_digest_has_no_link_and_no_dedupe_key(rig: Rig) -> None:
    n = one(rig)
    held(rig, 3, link="https://h.example/{i}")
    flush(rig)
    (body,) = payloads(n)
    assert "link" not in body
    (call,) = n.events("invoke")
    assert not [a for a in call["argv"] if a.startswith("--dedupe-key")]


@pytest.mark.parametrize("tool", ["slack-notice", "ha-notice"])
def test_the_digest_payload_validates_against_the_pinned_notification_schemas(
    rig: Rig, tool: str
) -> None:
    n = rig.notifier(tool, tool=tool)
    rig.configure({"chat": {"notifier": n, "kind": tool}})
    held(rig, 12)
    flush(rig)
    (body,) = payloads(n)
    ss.validate(body, tool, "notification.json")


def test_single_rows_are_sent_as_themselves_first_and_never_folded(rig: Rig) -> None:
    n = one(rig)
    seed(rig, message="one")
    seed(rig, message="two")
    held(rig, 2)
    code, doc, _ = flush(rig)
    assert [p.get("title") or p["message"] for p in payloads(n)] == [
        "one", "two", "2 held notifications",
    ]  # fmt: skip
    assert (dest(doc)["sent"], dest(doc)["digests"]) == (4, 1)


def test_two_single_rows_make_no_digest(rig: Rig) -> None:
    n = one(rig)
    seed(rig)
    seed(rig)
    _, doc, _ = flush(rig)
    assert (dest(doc)["digests"], len(n.events("invoke"))) == (0, 2)


def test_a_failed_single_row_stops_the_destination_before_the_digest(rig: Rig) -> None:
    n = one(rig, error={"code": "credential_invalid"})
    seed(rig)
    held(rig, 2)
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["failed"], dest(doc)["digests"]) == (12, 1, 0)
    assert len(n.events("invoke")) == 1
    assert rig.query("SELECT COUNT(*) FROM queue WHERE kind = 'held'") == [(2,)]


def test_a_second_flush_has_nothing_left_to_digest(rig: Rig) -> None:
    n = one(rig)
    held(rig, 3)
    flush(rig)
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["digests"], len(n.events("invoke"))) == (22, 0, 1)


def test_a_quiet_destination_folds_only_the_rows_that_reach_step_7(rig: Rig) -> None:
    n = quiet_config(rig)
    held(rig, 2, level="error")
    held(rig, 2, level="warn")  # postponed to the window's end
    held(rig, 1, level="info")  # dropped
    code, doc, _ = flush(rig)
    (body,) = payloads(n)
    assert body["title"] == "2 held notifications" and body["level"] == "error"
    assert (dest(doc)["sent"], dest(doc)["dropped"], dest(doc)["digests"]) == (2, 1, 1)
    assert rig.query("SELECT COUNT(*) FROM queue WHERE deliver_after > ?", (NOW,)) == [(2,)]
    assert code == 0


def test_dry_run_counts_a_pair_of_held_rows_as_work(rig: Rig) -> None:
    n = one(rig)
    held(rig, 2)
    code, doc, _ = flush(rig, "--dry-run")
    assert (code, dest(doc)["state"], dest(doc)["due_held"]) == (0, "done", 2)
    assert n.events("invoke") == [] and rig.query("SELECT COUNT(*) FROM queue") == [(2,)]


# --- members' end ------------------------------------------------------------------------------


def test_each_member_ends_sent_with_reason_digest_and_one_attempt(rig: Rig) -> None:
    one(rig)
    held(rig, 3, outcome="deferred", reason="quiet")
    flush(rig)
    assert states(rig) == [(i, "sent", "digest", None, 1) for i in (1, 2, 3)]


def test_the_log_has_one_line_per_member_with_its_own_text_hash_and_one_flush_line(
    rig: Rig,
) -> None:
    one(rig)
    held(rig, 3)
    flush(rig)
    lines = checked_log(rig)
    rows = [x for x in lines if x["record"] == "delivery"]
    assert [(x["outcome"], x["reason"], x["attempts"]) for x in rows] == [("sent", "digest", 1)] * 3
    assert len({x["message_sha256"] for x in rows}) == 3
    assert [x["record"] for x in lines].count("flush") == 1


def test_a_failed_digest_fails_every_member_and_surfaces_once_for_the_first_row(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig, error={"code": "credential_invalid"})
    held(rig, 1, level="warn", source="late")
    seed(rig, kind="held", level="error", source="first", title="x", message="y")
    held(rig, 1, level="error", source="second")
    events: list[SurfaceEvent] = []
    use_seams(monkeypatch, Seams(surface=lambda _c, e: events.append(e)))
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["failed"], dest(doc)["digests"]) == (12, 3, 1)
    assert states(rig) == [(i, "failed", "digest", "credential_invalid", 1) for i in (1, 2, 3)]
    (event,) = events
    assert (event.delivery_id, event.source, event.level) == (2, "first", "error")
    assert (event.outcome, event.code, event.attempts) == ("failed", "credential_invalid", 1)
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]


def test_an_unknown_digest_leaves_every_member_unknown_and_surfaces_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig, error={"code": "outcome_unknown"})
    held(rig, 3)
    events: list[SurfaceEvent] = []
    use_seams(monkeypatch, Seams(surface=lambda _c, e: events.append(e)))
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["unknown"], doc["outcome"]) == (12, 3, "partial")
    assert [e.outcome for e in events] == ["unknown"] and events[0].delivery_id == 1
    assert states(rig)[0][1:3] == ("unknown", "digest")


def test_a_disabled_notifier_ends_every_member_disabled(rig: Rig) -> None:
    one(rig, disabled=True)
    held(rig, 2)
    code, _, _ = flush(rig)
    assert code == 22 or code == 0
    assert [s[1:3] for s in states(rig)] == [("disabled", "digest")] * 2
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]


def test_a_retryable_digest_keeps_the_rows_held_and_adds_one_attempt_to_each(rig: Rig) -> None:
    one(rig, error={"code": "rate_limited"})
    for attempts in (0, 2, 2):
        seed(
            rig, kind="held", attempts=attempts, outcome="queued_retry" if attempts else "deferred"
        )
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["queued_retry"], dest(doc)["digests"]) == (0, 3, 1)
    # the schedule of the highest attempt count (3): 30 minutes
    assert rig.query(
        "SELECT d.outcome, d.reason, d.attempts, d.deliver_after, q.kind, q.deliver_after,"
        " q.claimed_by FROM deliveries d JOIN queue q ON q.delivery_id = d.id ORDER BY d.id"
    ) == [("queued_retry", "digest", a, NOW + 1800, "held", NOW + 1800, None) for a in (1, 3, 3)]


def test_a_retried_digest_is_sent_again_when_due_and_the_rows_keep_folding(rig: Rig) -> None:
    n = one(rig, error={"code": "rate_limited"})
    held(rig, 2)
    flush(rig)
    n.set()
    flush(rig)  # not due yet
    assert len(n.events("invoke")) == 1
    rig.clock.jump_wall(61)
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["sent"], dest(doc)["digests"]) == (0, 2, 1)
    assert len(payloads(n)) == 2 and payloads(n)[0] == payloads(n)[1]
    assert states(rig) == [(1, "sent", "digest", None, 2), (2, "sent", "digest", None, 2)]


def test_a_member_whose_attempts_are_used_up_fails_and_the_others_retry(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig, error={"code": "rate_limited"})
    seed(rig, kind="held", attempts=4, outcome="queued_retry")
    seed(rig, kind="held", attempts=1, outcome="queued_retry")
    events: list[SurfaceEvent] = []
    use_seams(monkeypatch, Seams(surface=lambda _c, e: events.append(e)))
    code, doc, _ = flush(rig)
    d = dest(doc)
    assert (code, d["failed"], d["queued_retry"]) == (12, 1, 1)
    assert states(rig) == [
        (1, "failed", "digest", "retries_exhausted", 5),
        (2, "queued_retry", "digest", "rate_limited", 2),
    ]
    assert rig.query("SELECT kind, deliver_after FROM queue") == [("held", NOW + 300)]
    assert [(e.delivery_id, e.code) for e in events] == [(1, "retries_exhausted")]


def test_rows_already_spent_by_escaped_calls_end_failed_without_joining_the_digest(
    rig: Rig,
) -> None:
    n = one(rig)
    seed(rig, kind="held", attempts=5, outcome="unknown", claim="old", claim_expires=NOW - 1)
    held(rig, 2)
    code, doc, _ = flush(rig)
    d = dest(doc)
    assert (code, d["failed"], d["sent"], d["digests"]) == (12, 1, 2, 1)
    assert payloads(n)[0]["title"] == "2 held notifications"
    assert states(rig)[0] == (1, "failed", None, "retries_exhausted", 5)


def test_a_digest_that_shrinks_to_one_row_sends_that_row_as_itself(rig: Rig) -> None:
    n = one(rig)
    seed(rig, kind="held", attempts=5, outcome="unknown", claim="old", claim_expires=NOW - 1)
    held(rig, 1, title="left", message="over")
    _, doc, _ = flush(rig)
    assert payloads(n) == [{"title": "left", "message": "over", "level": "warn"}]
    assert (dest(doc)["failed"], dest(doc)["sent"], dest(doc)["digests"]) == (1, 1, 0)


def test_a_busy_send_lock_returns_the_members_held_and_gives_back_one_count(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}}, lock_wait="1s")
    held(rig, 3)
    set_rate(rig, "other", 5)
    lock = hold(rig, "chat.lock")
    try:
        code, doc, _ = flush(rig)
    finally:
        lock.close()
    assert (code, dest(doc)["queued_retry"], n.events("invoke")) == (0, 3, [])
    assert rate(rig) == [("other", 5)]
    assert (
        rig.query(
            "SELECT d.outcome, d.reason, d.attempts, q.kind, q.deliver_after, q.claimed_by"
            " FROM deliveries d JOIN queue q ON q.delivery_id = d.id ORDER BY d.id"
        )
        == [("queued_retry", "lock_busy", 0, "held", NOW + 60, None)] * 3
    )


def test_an_unset_variable_found_at_call_time_defers_the_members_single_and_gives_back_one_count(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig)
    held(rig, 3)
    set_rate(rig, "other", 5)
    use_seams(
        monkeypatch,
        Seams(call=lambda *_a, **_k: CallResult("env_missing", missing=("X",))),
    )
    code, _, _ = flush(rig)
    assert code == 0 and rate(rig) == [("other", 5)]
    assert (
        rig.query(
            "SELECT d.outcome, d.reason, d.attempts, q.kind, q.deliver_after FROM deliveries d"
            " JOIN queue q ON q.delivery_id = d.id ORDER BY d.id"
        )
        == [("deferred", "env_missing", 0, "single", NOW)] * 3
    )


@pytest.mark.parametrize("stored", ["lock_busy", "link_refused", "keyword_refused"])
def test_a_plain_retry_of_members_stored_with_another_reason_sets_digest(
    rig: Rig, stored: str
) -> None:
    one(rig, error={"code": "rate_limited"})
    for _ in range(2):
        seed(rig, kind="held", outcome="queued_retry", reason=stored)
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["queued_retry"]) == (0, 2)
    assert states(rig) == [(i, "queued_retry", "digest", "rate_limited", 1) for i in (1, 2)]


def test_a_lock_busy_result_of_this_call_keeps_its_reason_over_a_stored_one(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}}, lock_wait="1s")
    for _ in range(2):
        seed(rig, kind="held", outcome="queued_retry", reason="rate_limited")
    lock = hold(rig, "chat.lock")
    try:
        flush(rig)
    finally:
        lock.close()
    assert [s[1:3] for s in states(rig)] == [("queued_retry", "lock_busy")] * 2


# --- the hourly counters -----------------------------------------------------------------------


def test_a_digest_counts_one_call_in_the_class_of_its_highest_level(rig: Rig) -> None:
    one(rig)
    held(rig, 4, level="warn")
    held(rig, 1, level="error")
    flush(rig)
    assert rate(rig) == [("error", 1)]
    held(rig, 3, level="action")
    flush(rig)
    assert rate(rig) == [("error", 1), ("other", 1)]


def test_the_cap_never_holds_a_digest_but_the_digest_still_counts(rig: Rig) -> None:
    n = one(rig)
    rig.configure({"chat": {"notifier": n, "max_per_hour": 2}})
    held(rig, 3)
    set_rate(rig, "other", 2)
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["sent"], dest(doc)["digests"]) == (0, 3, 1)
    assert rate(rig) == [("other", 3)]


def test_the_cap_still_holds_a_lone_held_row(rig: Rig) -> None:
    n = one(rig)
    rig.configure({"chat": {"notifier": n, "max_per_hour": 2}})
    held(rig, 1)
    set_rate(rig, "other", 2)
    _, doc, _ = flush(rig)
    assert (dest(doc)["sent"], n.events("invoke")) == (0, [])


# --- expiry ------------------------------------------------------------------------------------


def test_a_row_24_hours_old_expires_and_one_second_younger_joins_the_digest(rig: Rig) -> None:
    n = one(rig)
    seed(rig, kind="held", created_at=NOW - 86400, message="old")
    seed(rig, kind="held", created_at=NOW - 86399, message="edge")
    seed(rig, kind="held", message="new")
    _, doc, _ = flush(rig)
    d = dest(doc)
    assert (d["expired"], d["sent"], d["digests"], d["due_held"]) == (1, 2, 1, 3)
    (body,) = payloads(n)
    assert body["title"] == "2 held notifications" and "old" not in body["message"]
    assert states(rig)[0][1] == "expired"


def test_when_expiry_leaves_one_row_it_is_sent_as_itself(rig: Rig) -> None:
    n = one(rig)
    seed(rig, kind="held", created_at=NOW - 86400, message="old")
    seed(rig, kind="held", message="new", title="T")
    flush(rig)
    assert payloads(n) == [{"title": "T", "message": "new", "level": "info"}]


# --- hostile text ------------------------------------------------------------------------------

CANARY = "HOSTILE-7f3a91"


def test_hostile_member_text_is_never_raw_and_never_expanded(rig: Rig) -> None:
    n = one(rig)
    bad = f"{{{{source}}}}{CANARY}\x1b[31mred\x1b]0;x\x07‮\r\nSECOND\x00\ttab"
    held(rig, 2, title=bad, message=bad)
    held(rig, 1, title="x" * 5000, message="y" * 5000)
    held(rig, 1, title="\U0001f600" * 300, message="z")
    code, out, err = rig.run("flush", "--json")
    assert code == 0 and CANARY not in out + err
    (body,) = payloads(n)
    ss.validate(body, "slack-notice", "notification.json")
    text = body["message"]
    for ch in ("\x1b", "\x00", "\x07", "‮", "\r", "\t"):
        assert ch not in text
    assert "SECOND" not in text and "{{source}}" in text
    assert all(len(line.split(": ", 1)[1]) <= 120 for line in text.splitlines()[:4])
    text.encode("utf-8")
    assert CANARY not in json.dumps(checked_log(rig))


# --- failures at each point --------------------------------------------------------------------


def test_a_member_claimed_by_someone_else_during_the_call_is_logged_only_the_rest_recorded(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = one(rig)
    first, *_ = held(rig, 3)

    def takeover(*args: Any, **kwargs: Any) -> Any:
        with open_store(rig.gateway()) as store, store.write() as conn:
            conn.execute("UPDATE queue SET claim_expires = ? WHERE id = ?", (NOW - 1, first))
            assert claim_row(conn, first, Lease(NOW, 50), "other-claimant") is not None
        return call_notifier(*args, **kwargs)

    use_seams(monkeypatch, Seams(call=takeover))
    code, doc, err = flush(rig)
    assert code == 0 and "claim was lost" in err and len(n.events("invoke")) == 1
    assert dest(doc)["sent"] == 3 and dest(doc)["digests"] == 1
    assert rig.query("SELECT id, claimed_by FROM queue") == [(first, "other-claimant")]
    assert [s[1] for s in states(rig)] == ["deferred", "sent", "sent"]
    assert sorted(x["outcome"] for x in checked_log(rig) if x["record"] == "delivery") == [
        "sent", "sent", "sent",
    ]  # fmt: skip


def test_an_exception_from_the_call_leaves_every_member_unknown_and_claimed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig)
    held(rig, 3)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("BOOM-TEXT")

    use_seams(monkeypatch, Seams(call=boom))
    code, doc, err = flush(rig)
    assert (code, dest(doc)["unknown"], dest(doc)["digests"]) == (12, 3, 1)
    assert states(rig) == [(i, "unknown", "digest", "killed", 1) for i in (1, 2, 3)]
    assert rig.query("SELECT COUNT(claimed_by) FROM queue") == [(3,)]
    assert "BOOM-TEXT" not in err + json.dumps(doc) + rig.state_text()
    lines = [x for x in checked_log(rig) if x["record"] == "delivery"]
    assert [(x["outcome"], x["reason"], x["attempts"]) for x in lines] == [
        ("unknown", "digest", 1)
    ] * 3


def test_the_attempts_of_an_escaped_digest_follow_each_member(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig)
    for attempts in (0, 2, 3):
        seed(rig, kind="held", attempts=attempts, outcome="unknown" if attempts else "deferred")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("BOOM-TEXT")

    use_seams(monkeypatch, Seams(call=boom))
    flush(rig)
    assert [s[4] for s in states(rig)] == [1, 3, 4]
    lines = [x for x in checked_log(rig) if x["record"] == "delivery"]
    assert [x["attempts"] for x in lines] == [1, 3, 4]


def test_a_digest_whose_recording_fails_leaves_all_members_claimed_then_resends_them(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = one(rig, error={"code": "credential_invalid"})
    held(rig, 3)

    def broken(*_a: Any, **_k: Any) -> None:
        raise ValueError("SEAM-TEXT")

    use_seams(monkeypatch, Seams(surface=broken))
    code, out, err = rig.run("flush", "--json")
    assert code == 1 and "SEAM-TEXT" not in out + err
    assert [s[1] for s in states(rig)] == ["deferred"] * 3  # all or nothing
    assert rig.query("SELECT COUNT(claimed_by) FROM queue") == [(3,)]
    conn = sqlite3.connect(rig.state / "state.db")
    conn.execute("UPDATE queue SET claim_expires = ?", (NOW - 1,))
    conn.commit()
    conn.close()
    n.set()  # the notifier works now
    code, doc, _ = flush(rig)
    assert (code, dest(doc)["sent"], dest(doc)["digests"]) == (0, 3, 1)
    assert len(n.events("invoke")) == 2  # the documented duplicate


def test_a_flusher_killed_mid_digest_leaves_the_members_claimed_and_they_are_resent_once(
    rig: Rig,
) -> None:
    n = rig.notifier(hang=True, max_seconds=60)
    rig.configure({"chat": {"notifier": n}})
    t = int(time.time())
    for i in range(3):
        seed(rig, kind="held", now=t, deliver_after=t, message=f"m{i}")
    proc = rig.spawn("flush", "--json")
    n.wait_for("invoke", timeout=30)
    proc.kill()  # the exact handle this test started
    proc.communicate(timeout=10)
    n.kill_tracked()
    rows = rig.query("SELECT claimed_by FROM queue")
    assert len(rows) == 3 and len({r[0] for r in rows}) == 1 and rows[0][0] is not None
    n.set()
    conn = sqlite3.connect(rig.state / "state.db")
    conn.execute("UPDATE queue SET claim_expires = ?", (t - 1,))
    conn.commit()
    conn.close()
    out, _ = rig.spawn("flush", "--json").communicate(timeout=30)
    d = json.loads(out)["destinations"][0]
    assert (d["sent"], d["digests"]) == (3, 1)
    rig.spawn("flush", "--json").communicate(timeout=30)
    assert rig.query("SELECT outcome FROM deliveries") == [("sent",)] * 3
    assert len(n.events("invoke")) == 2  # the killed call and the resend: at least once


def test_a_signal_before_the_call_releases_the_members_and_the_count(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig)
    held(rig, 3)
    from notice_route import calling

    monkeypatch.setattr(calling.Gate, "begin", lambda self: False)
    flush(rig)
    assert rate(rig) == [("other", 0)] or rate(rig) == []
    assert rig.query("SELECT COUNT(*) FROM queue WHERE claim_expires > ?", (NOW,)) == [(0,)]
    assert [s[1] for s in states(rig)] == ["deferred"] * 3


def test_a_wall_clock_that_steps_neither_loses_nor_repeats_a_digest(rig: Rig) -> None:
    n = one(rig)
    held(rig, 3, deliver_after=NOW + 30)
    assert dest(flush(rig)[1])["digests"] == 0
    rig.clock.jump_wall(60)
    assert dest(flush(rig)[1])["digests"] == 1
    rig.clock.jump_wall(-3600)
    assert dest(flush(rig)[1])["digests"] == 0
    rig.clock.jump_wall(2 * 3600)
    assert dest(flush(rig)[1])["digests"] == 0 and len(n.events("invoke")) == 1


def test_a_stored_row_with_an_invalid_level_is_left_out_of_the_digest(rig: Rig) -> None:
    n = one(rig)
    ids = held(rig, 3)
    conn = sqlite3.connect(rig.state / "state.db")
    conn.execute("UPDATE queue SET level = 'JUNK-LEVEL' WHERE id = ?", (ids[0],))
    conn.commit()
    conn.close()
    code, out, err = rig.run("flush", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "internal"
    assert err.count("warning:") == 1 and "JUNK-LEVEL" not in out + err
    (body,) = payloads(n)
    assert body["title"] == "2 held notifications"
    assert rig.query("SELECT id FROM queue") == [(ids[0],)]


def test_the_attempts_of_a_lost_member_follow_its_row(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig)
    first = seed(rig, kind="held", attempts=2, outcome="queued_retry")[1]
    held(rig, 2)

    def takeover(*args: Any, **kwargs: Any) -> Any:
        with open_store(rig.gateway()) as store, store.write() as conn:
            conn.execute("UPDATE queue SET claim_expires = ? WHERE id = ?", (NOW - 1, first))
            assert claim_row(conn, first, Lease(NOW, 50), "other-claimant") is not None
        return call_notifier(*args, **kwargs)

    use_seams(monkeypatch, Seams(call=takeover))
    flush(rig)
    lines = [x for x in checked_log(rig) if x["record"] == "delivery"]
    assert sorted(x["attempts"] for x in lines) == [1, 1, 2]


def test_a_member_whose_rows_vanished_during_the_call_does_not_stop_the_others_recording(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = one(rig, error={"code": "rate_limited"})
    first, *_ = held(rig, 3)

    def vanish(*args: Any, **kwargs: Any) -> Any:
        conn = sqlite3.connect(rig.state / "state.db")
        conn.execute("DELETE FROM queue WHERE id = ?", (first,))
        conn.execute("DELETE FROM deliveries WHERE id = ?", (first,))
        conn.commit()
        conn.close()
        return call_notifier(*args, **kwargs)

    use_seams(monkeypatch, Seams(call=vanish))
    code, doc, _ = flush(rig)
    assert len(n.events("invoke")) == 1 and code == 0
    assert states(rig) == [(i, "queued_retry", "digest", "rate_limited", 1) for i in (2, 3)]
    assert rig.query("SELECT kind, claimed_by FROM queue ORDER BY id") == [("held", None)] * 2


def test_the_give_back_of_a_digest_lands_in_the_digests_class_not_the_first_claimed_members(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    one(rig)
    first = held(rig, 1, level="error")[0]
    held(rig, 2, level="warn")
    set_rate(rig, "error", 5)
    set_rate(rig, "other", 5)

    def takeover_then_env_missing(*_a: Any, **_k: Any) -> Any:
        with open_store(rig.gateway()) as store, store.write() as conn:
            conn.execute("UPDATE queue SET claim_expires = ? WHERE id = ?", (NOW - 1, first))
            assert claim_row(conn, first, Lease(NOW, 50), "other-claimant") is not None
        return CallResult("env_missing", missing=("X",))

    use_seams(monkeypatch, Seams(call=takeover_then_env_missing))
    flush(rig)
    assert rate(rig) == [("error", 5), ("other", 5)]


# --- the 100-row cap ---------------------------------------------------------------------------


@pytest.mark.parametrize(("seeded", "dropped"), [(100, 3), (98, 1), (97, 0)])
def test_a_digest_requeue_meets_the_100_row_cap(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, seeded: int, dropped: int
) -> None:
    one(rig, error={"code": "rate_limited"})
    held(rig, 3)

    def fill_then_call(*args: Any, **kwargs: Any) -> Any:
        for _ in range(seeded):
            seed(rig, level="action", deliver_after=NOW + 9999)
        return call_notifier(*args, **kwargs)

    use_seams(monkeypatch, Seams(call=fill_then_call))
    code, doc, _ = flush(rig)
    d = dest(doc)
    assert (d["dropped"], d["queued_retry"]) == (dropped, 3 - dropped)
    got = states(rig)[:3]
    for i, (_id, outcome, reason, _code, attempts) in enumerate(got):
        if i < dropped:
            assert (outcome, reason, attempts) == ("dropped", "overflow", 1)
        else:
            assert (outcome, reason, attempts) == ("queued_retry", "digest", 1)
    assert rig.query("SELECT COUNT(*) FROM queue") == [(min(100, seeded + 3 - dropped),)]
    lines = [x["outcome"] for x in checked_log(rig) if x["record"] == "delivery"]
    assert (lines.count("dropped"), lines.count("queued_retry")) == (dropped, 3 - dropped)
