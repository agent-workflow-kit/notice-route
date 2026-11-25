"""Recording an outcome: deliveries, the queue, dedup records, counters and fencing (#20)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from notice_route import decide, dedup
from notice_route import queue as q
from notice_route.gateway import FakeClock, Gateway
from notice_route.outcome import (
    Applied,
    Outcome,
    apply_outcome,
    record_escaped,
    record_outcome,
)
from notice_route.queue import Lease, QueueRow
from notice_route.store import Store, open_store

NOW = 1_700_000_000
HOUR = decide.hour_start(NOW)
CLAIM = "claim-a"
DIGEST = "b" * 64
EVENT_DIGEST = "c" * 64


def retry_at(attempts: int) -> int | None:
    """A stand-in for the schedule of #25: 60 s, then 300 s, and none after 3 attempts."""
    return {1: NOW + 60, 2: NOW + 300}.get(attempts)


@pytest.fixture
def gw(home: Path) -> Gateway:
    return Gateway(passwd_home=lambda: str(home), clock=FakeClock(wall=NOW))


@pytest.fixture
def store(gw: Gateway) -> Iterator[Store]:
    with open_store(gw) as s:
        yield s


def make(
    store: Store,
    *,
    link: str | None = "https://x.example/a",
    message: str = "hello",
    attempts: int = 0,
    reason: str | None = None,
    level: str = "warn",
    token: str = CLAIM,
    destination: str = "d",
    kind: str = "single",
) -> QueueRow:
    with store.write() as conn:
        did = q.insert_delivery(
            conn,
            ts=NOW,
            source="src",
            level=level,
            outcome="sending",
            destination=destination,
            attempts=attempts,
            reason=reason,
            dedupe=DIGEST,
            message_sha256=decide._sha16(message),
        )
        assert dedup.check_repeat(
            conn, destination=destination, digest=DIGEST, now=NOW, window_length=600
        ).opened
        dedup.bind_window(conn, destination=destination, digest=DIGEST, delivery_id=did)
        dedup.record_event_id(
            conn,
            destination=destination,
            source="src",
            hook="h",
            event_id=7,
            digest=EVENT_DIGEST,
            now=NOW,
            retention=1000,
            delivery_id=did,
        )
        conn.execute(
            "INSERT INTO rate (destination, hour, class, count) VALUES (?, ?, 'other', 1)"
            " ON CONFLICT DO UPDATE SET count = count + 1",
            (destination, HOUR),
        )
        res = q.enqueue(
            conn,
            delivery_id=did,
            kind=kind,  # type: ignore[arg-type]
            message=message,
            level=level,
            source="src",
            deliver_after=NOW,
            created_at=NOW,
            lease=Lease(NOW, 20),
            link=link,
            dedupe=DIGEST,
            claim_token=token,
        )
        row = q.get_row(conn, res.row_id)
    assert row is not None
    return row


def apply(store: Store, row: QueueRow, outcome: Outcome, **kw: object) -> Applied:
    with store.write() as conn:
        return apply_outcome(
            conn,
            row=row,
            token=kw.pop("token", CLAIM),  # type: ignore[arg-type]
            outcome=outcome,
            lease=Lease(NOW, 20),
            retry_at=retry_at,
            **kw,  # type: ignore[arg-type]
        )


def kind_of(store: Store) -> object:
    return store.read("SELECT kind FROM queue")[0][0]


def delivery(store: Store, did: int) -> tuple[object, ...]:
    return store.read(
        "SELECT outcome, reason, code, attempts, deliver_after, message_sha256"
        " FROM deliveries WHERE id = ?",
        (did,),
    )[0]


def n(store: Store, table: str) -> int:
    return cast(int, store.read(f"SELECT COUNT(*) FROM {table}")[0][0])


def reclaim(store: Store, row: QueueRow) -> QueueRow:
    """The row again, claimed by CLAIM as a later flush would hold it."""
    with store.write() as conn:
        conn.execute("UPDATE queue SET claimed_by = ? WHERE id = ?", (CLAIM, row.id))
        again = q.get_row(conn, row.id)
    assert again is not None
    return again


def rate(store: Store) -> int:
    return cast(int, store.read("SELECT count FROM rate")[0][0])


# --- terminal outcomes ------------------------------------------------------------------------


def test_sent_deletes_the_row_counts_the_attempt_and_keeps_dedup(store: Store) -> None:
    row = make(store)
    res = apply(store, row, Outcome("sent", notifier_outcome="sent"))
    assert (res.applied, res.outcome, res.attempts, res.notifier_outcome) == (
        True,
        "sent",
        1,
        "sent",
    )
    assert delivery(store, row.delivery_id)[:5] == ("sent", None, None, 1, None)
    assert n(store, "queue") == 0
    assert n(store, "dedup") == 1 and n(store, "event_ids") == 1
    assert not res.released
    assert rate(store) == 1


def test_unknown_keeps_dedup(store: Store) -> None:
    row = make(store)
    apply(store, row, Outcome("unknown", "killed"))
    assert delivery(store, row.delivery_id)[:4] == ("unknown", None, "killed", 1)
    assert n(store, "queue") == 0 and n(store, "dedup") == 1 and n(store, "event_ids") == 1


@pytest.mark.parametrize("outcome", [Outcome("failed", "auth_required"), Outcome("disabled", None)])
def test_failed_and_disabled_release_dedup(store: Store, outcome: Outcome) -> None:
    row = make(store)
    res = apply(store, row, outcome)
    assert res.released
    assert delivery(store, row.delivery_id)[0] == outcome.outcome
    assert n(store, "queue") == 0 and n(store, "dedup") == 0 and n(store, "event_ids") == 0


def test_release_touches_only_its_own_delivery(store: Store) -> None:
    other = make(store, destination="e", token="claim-b")
    row = make(store)
    apply(store, row, Outcome("failed", "x"))
    assert n(store, "dedup") == 1
    assert delivery(store, other.delivery_id)[0] == "sending"


def test_failed_sets_a_prior_reason_untouched(store: Store) -> None:
    row = make(store, reason="quiet", attempts=2)
    res = apply(store, row, Outcome("sent"))
    assert delivery(store, row.delivery_id)[:4] == ("sent", "quiet", None, 3)
    assert res.reason == "quiet"


def test_hourly_counter_is_not_given_back_for_a_call_that_was_made(store: Store) -> None:
    row = make(store)
    apply(store, row, Outcome("failed", "exec_failed"), count_hour=HOUR)
    assert rate(store) == 1


# --- retries ----------------------------------------------------------------------------------


def test_queued_retry_requeues_with_the_schedule(store: Store) -> None:
    row = make(store)
    res = apply(store, row, Outcome("queued_retry", "network_error"))
    assert (res.outcome, res.attempts, res.deliver_after) == ("queued_retry", 1, NOW + 60)
    assert delivery(store, row.delivery_id)[:5] == (
        "queued_retry",
        None,
        "network_error",
        1,
        NOW + 60,
    )
    back = store.read("SELECT claimed_by, claim_expires, deliver_after FROM queue")[0]
    assert back == (None, None, NOW + 60)
    assert n(store, "dedup") == 1 and n(store, "event_ids") == 1  # kept while pending


def test_retries_exhausted_ends_failed_and_releases(store: Store) -> None:
    row = make(store, attempts=2)
    res = apply(store, row, Outcome("queued_retry", "network_error"))
    assert (res.outcome, res.code, res.released) == ("failed", "retries_exhausted", True)
    assert delivery(store, row.delivery_id)[:4] == ("failed", None, "retries_exhausted", 3)
    assert n(store, "queue") == 0 and n(store, "dedup") == 0


def test_link_refused_rewrites_the_row(store: Store) -> None:
    row = make(store)
    res = apply(store, row, Outcome("queued_retry", "link_scheme", refusal="link_refused"))
    assert (res.outcome, res.reason, res.deliver_after) == (
        "queued_retry",
        "link_refused",
        NOW + 60,
    )
    msg, link, due = store.read("SELECT message, link, deliver_after FROM queue")[0]
    assert (msg, link, due) == ("hello\nhttps://x.example/a", None, NOW + 60)
    d = delivery(store, row.delivery_id)
    assert d[:5] == ("queued_retry", "link_refused", "link_scheme", 1, NOW + 60)
    assert d[5] == decide._sha16("hello\nhttps://x.example/a")


def test_keyword_refused_prefixes_the_source_and_keeps_the_link(store: Store) -> None:
    row = make(store)
    res = apply(store, row, Outcome("queued_retry", "command_keyword", refusal="keyword_refused"))
    assert res.reason == "keyword_refused"
    msg, link = store.read("SELECT message, link FROM queue")[0]
    assert (msg, link) == ("src: hello", "https://x.example/a")


def test_refusal_rewrite_is_cut_to_4000(store: Store) -> None:
    row = make(store, message="m" * 3990, link="https://x.example/" + "a" * 100)
    apply(store, row, Outcome("queued_retry", "link_scheme", refusal="link_refused"))
    msg = str(store.read("SELECT message FROM queue")[0][0])
    assert len(msg) == 4000 and msg.startswith("m" * 3990 + "\n")


def test_refusal_cut_leaves_no_trailing_space(store: Store) -> None:
    row = make(store, message="m" * 3998 + " ", link="https://x.example/a")
    apply(store, row, Outcome("queued_retry", "link_scheme", refusal="link_refused"))
    msg = str(store.read("SELECT message FROM queue")[0][0])
    assert not msg.endswith(" ") and len(msg) <= 4000


@pytest.mark.parametrize("prior", ["link_refused", "keyword_refused"])
@pytest.mark.parametrize("refusal", ["link_refused", "keyword_refused"])
def test_a_second_refusal_ends_failed(store: Store, prior: str, refusal: str) -> None:
    row = make(store, reason=prior, attempts=1)
    code = "link_invalid" if refusal == "link_refused" else "command_keyword"
    res = apply(store, row, Outcome("queued_retry", code, refusal=refusal))  # type: ignore[arg-type]
    assert (res.outcome, res.code, res.released) == ("failed", code, True)
    assert delivery(store, row.delivery_id)[:4] == ("failed", prior, code, 2)
    assert n(store, "queue") == 0 and n(store, "dedup") == 0


def test_link_refusal_of_a_row_without_a_link_ends_failed(store: Store) -> None:
    row = make(store, link=None)
    res = apply(store, row, Outcome("queued_retry", "link_invalid", refusal="link_refused"))
    assert (res.outcome, res.code) == ("failed", "link_invalid")


def test_refusal_retry_is_bounded_by_the_attempt_cap(store: Store) -> None:
    row = make(store, attempts=2)
    res = apply(store, row, Outcome("queued_retry", "link_invalid", refusal="link_refused"))
    assert (res.outcome, res.code) == ("failed", "retries_exhausted")


def test_a_second_refusal_at_the_attempt_cap_keeps_the_refusal_code(store: Store) -> None:
    row = make(store, reason="link_refused", attempts=2)
    res = apply(store, row, Outcome("queued_retry", "link_invalid", refusal="link_refused"))
    assert (res.outcome, res.code) == ("failed", "link_invalid")
    assert delivery(store, row.delivery_id)[:4] == ("failed", "link_refused", "link_invalid", 3)


def test_a_link_refusal_without_a_link_at_the_attempt_cap_keeps_the_refusal_code(
    store: Store,
) -> None:
    row = make(store, link=None, attempts=2)
    res = apply(store, row, Outcome("queued_retry", "link_invalid", refusal="link_refused"))
    assert (res.outcome, res.code) == ("failed", "link_invalid")


def test_the_second_refusal_attempt_follows_the_schedule_like_any_retry(store: Store) -> None:
    row = make(store, attempts=1)
    res = apply(store, row, Outcome("queued_retry", "command_keyword", refusal="keyword_refused"))
    assert (res.outcome, res.deliver_after) == ("queued_retry", NOW + 300)


def test_a_non_refusal_retry_keeps_the_refused_reason_so_a_later_refusal_still_fails(
    store: Store,
) -> None:
    row = make(store, reason="link_refused", attempts=1)
    apply(store, row, Outcome("queued_retry", "network_error"))
    assert delivery(store, row.delivery_id)[1] == "link_refused"


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome("queued_retry", "network_error"),
        Outcome("queued_retry", "link_invalid", refusal="link_refused"),
        Outcome("queued_retry", "command_keyword", refusal="keyword_refused"),
        Outcome("deferred", None, "env_missing", counted=False),
    ],
)
def test_a_requeued_held_row_becomes_a_single_row(store: Store, outcome: Outcome) -> None:
    row = make(store, kind="held")
    assert kind_of(store) == "held"
    assert apply(store, row, outcome).applied
    assert kind_of(store) == "single"


def test_row12_and_row18_retries_keep_their_codes(store: Store) -> None:
    row = make(store)
    res = apply(store, row, Outcome("queued_retry", "signal"))
    assert (res.outcome, res.code) == ("queued_retry", "signal")
    assert delivery(store, row.delivery_id)[2] == "signal"
    row = make(store, destination="e", token="claim-b")
    res = apply(store, row, Outcome("queued_retry", "upstream_error"), token="claim-b")
    assert (res.outcome, res.code) == ("queued_retry", "upstream_error")


def test_exec_failed_counts_an_attempt(store: Store) -> None:
    row = make(store, attempts=1)
    res = apply(store, row, Outcome("failed", "exec_failed"))
    assert (res.outcome, res.attempts) == ("failed", 2)


def test_a_64_character_code_and_reason_are_accepted(store: Store) -> None:
    code = "c" * 64
    row = make(store)
    res = apply(store, row, Outcome("failed", code))
    assert delivery(store, row.delivery_id)[2] == code and res.code == code
    other = make(store, destination="e", token="claim-b")
    res = apply(store, other, Outcome("deferred", None, "r" * 64, counted=False), token="claim-b")
    assert delivery(store, other.delivery_id)[1] == "r" * 64


# --- internal faults and env_missing ----------------------------------------------------------


def test_internal_fault_records_nothing(store: Store) -> None:
    row = make(store)
    res = apply(store, row, Outcome(None, counted=False), count_hour=HOUR)
    assert res.internal and not res.applied
    assert delivery(store, row.delivery_id)[:4] == ("sending", None, None, 0)
    assert store.read("SELECT claimed_by FROM queue")[0][0] == CLAIM
    assert rate(store) == 1


def test_env_missing_requeues_due_now_without_an_attempt_and_gives_the_count_back(
    store: Store,
) -> None:
    row = make(store)
    res = apply(
        store, row, Outcome("deferred", None, "env_missing", counted=False), count_hour=HOUR
    )
    assert (res.outcome, res.reason, res.attempts) == ("deferred", "env_missing", 0)
    assert delivery(store, row.delivery_id)[:5] == ("deferred", "env_missing", None, 0, NOW)
    assert store.read("SELECT claimed_by, deliver_after FROM queue")[0] == (None, NOW)
    assert rate(store) == 0
    assert n(store, "dedup") == 1


def test_a_retry_after_a_deferral_clears_the_stale_code(store: Store) -> None:
    row = make(store)
    apply(store, row, Outcome("queued_retry", "network_error"))
    again = reclaim(store, row)
    apply(store, again, Outcome("deferred", None, "env_missing", counted=False))
    assert delivery(store, row.delivery_id)[:3] == ("deferred", "env_missing", None)


def test_env_missing_never_overwrites_a_stored_refusal_reason(store: Store) -> None:
    row = make(store, reason="link_refused", attempts=1)
    res = apply(store, row, Outcome("deferred", None, "env_missing", counted=False))
    assert res.reason == "link_refused"
    assert delivery(store, row.delivery_id)[:2] == ("deferred", "link_refused")
    again = reclaim(store, row)
    res = apply(store, again, Outcome("queued_retry", "link_invalid", refusal="link_refused"))
    assert (res.outcome, res.code) == ("failed", "link_invalid")


def test_env_missing_without_a_count_hour_gives_nothing_back(store: Store) -> None:
    row = make(store)
    apply(store, row, Outcome("deferred", None, "env_missing", counted=False))
    assert rate(store) == 1


def test_give_back_never_goes_below_zero(store: Store) -> None:
    row = make(store)
    with store.write() as conn:
        conn.execute("UPDATE rate SET count = 0")
    apply(store, row, Outcome("deferred", None, "env_missing", counted=False), count_hour=HOUR)
    assert rate(store) == 0


# --- fencing ----------------------------------------------------------------------------------


def test_a_stale_claimant_whose_delivery_was_trimmed_is_not_applied(store: Store) -> None:
    row = make(store)
    with store.write() as conn:
        conn.execute("DELETE FROM queue")  # evicted, and its delivery trimmed since
        conn.execute("DELETE FROM dedup")
        conn.execute("DELETE FROM event_ids")
        conn.execute("DELETE FROM deliveries")
    assert not apply(store, row, Outcome("sent")).applied


def test_a_stale_claimant_changes_nothing(store: Store) -> None:
    row = make(store)
    with store.write() as conn:  # taken over
        conn.execute("UPDATE queue SET claimed_by = 'other'")
    before = store.read("SELECT * FROM deliveries")
    res = apply(store, row, Outcome("failed", "x"), count_hour=HOUR)
    assert not res.applied and not res.internal
    assert store.read("SELECT * FROM deliveries") == before
    assert n(store, "queue") == 1 and n(store, "dedup") == 1 and rate(store) == 1


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome("sent"),
        Outcome("queued_retry", "e"),
        Outcome("queued_retry", "link_invalid", refusal="link_refused"),
        Outcome("deferred", None, "env_missing", counted=False),
    ],
)
def test_a_stale_claimant_changes_nothing_for_every_kind_of_result(
    store: Store, outcome: Outcome
) -> None:
    row = make(store)
    with store.write() as conn:
        conn.execute("UPDATE queue SET claimed_by = 'other'")
    snap = (store.read("SELECT * FROM deliveries"), store.read("SELECT * FROM queue"))
    assert not apply(store, row, outcome, count_hour=HOUR).applied
    assert (store.read("SELECT * FROM deliveries"), store.read("SELECT * FROM queue")) == snap


def test_an_evicted_row_records_nothing_and_keeps_the_overflow_end(store: Store) -> None:
    row = make(store)
    with store.write() as conn:  # the cap evicted it while the call ran
        conn.execute("DELETE FROM queue")
        conn.execute("UPDATE deliveries SET outcome='dropped', reason='overflow', code='overflow'")
        dedup.release(conn, row.delivery_id)
    res = apply(store, row, Outcome("sent"))
    assert not res.applied
    assert delivery(store, row.delivery_id)[:3] == ("dropped", "overflow", "overflow")


def test_a_lapsed_claim_nobody_took_still_accepts_its_result(store: Store) -> None:
    row = make(store)
    with store.write() as conn:
        q.release_claims(conn, CLAIM, Lease(NOW + 1000, 20))
    assert apply(store, row, Outcome("sent")).applied


def test_a_second_result_for_the_same_row_is_not_applied(store: Store) -> None:
    row = make(store)
    assert apply(store, row, Outcome("sent")).applied
    assert not apply(store, row, Outcome("failed", "x")).applied
    assert delivery(store, row.delivery_id)[0] == "sent"


# --- the cap on a requeue ---------------------------------------------------------------------


def test_a_requeued_row_dropped_by_the_cap_ends_dropped_and_releases(store: Store) -> None:
    row = make(store, level="info")
    with store.write() as conn:
        for _ in range(100):
            did = q.insert_delivery(
                conn, ts=NOW, source="s", level="error", outcome="deferred", destination="d"
            )
            q.enqueue(
                conn,
                delivery_id=did,
                kind="single",
                message="m",
                level="error",
                source="s",
                deliver_after=NOW,
                created_at=NOW,
                lease=Lease(NOW, 20),
            )
    res = apply(store, row, Outcome("queued_retry", "e"))
    assert (res.outcome, res.code, res.reason) == ("dropped", "overflow", "overflow")
    assert res.released and len(res.evicted) == 1
    assert delivery(store, row.delivery_id)[:4] == ("dropped", "overflow", "overflow", 1)
    assert n(store, "dedup") == 0 and n(store, "event_ids") == 0


def test_env_missing_give_back_survives_the_cap_eviction(store: Store) -> None:
    row = make(store, level="info")
    with store.write() as conn:
        for _ in range(100):
            did = q.insert_delivery(
                conn, ts=NOW, source="s", level="error", outcome="deferred", destination="d"
            )
            q.enqueue(
                conn,
                delivery_id=did,
                kind="single",
                message="m",
                level="error",
                source="s",
                deliver_after=NOW,
                created_at=NOW,
                lease=Lease(NOW, 20),
            )
    res = apply(
        store, row, Outcome("deferred", None, "env_missing", counted=False), count_hour=HOUR
    )
    assert (res.outcome, res.code) == ("dropped", "overflow")
    assert rate(store) == 0


# --- atomicity --------------------------------------------------------------------------------


def test_a_failure_while_recording_rolls_everything_back(
    store: Store, gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = make(store)

    def boom(*_a: object, **_k: object) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(dedup, "release", boom)
    with pytest.raises(RuntimeError):
        record_outcome(
            store,
            gw,
            send_worst_case_s=10,
            row=row,
            token=CLAIM,
            outcome=Outcome("failed", "x"),
            retry_at=retry_at,
        )
    assert delivery(store, row.delivery_id)[:4] == ("sending", None, None, 0)
    assert store.read("SELECT claimed_by FROM queue")[0][0] == CLAIM
    assert n(store, "dedup") == 1


def test_a_failing_schedule_rolls_back(store: Store, gw: Gateway) -> None:
    row = make(store)

    def bad(_attempts: int) -> int | None:
        raise ValueError("no")

    with pytest.raises(ValueError):
        record_outcome(
            store,
            gw,
            send_worst_case_s=10,
            row=row,
            token=CLAIM,
            outcome=Outcome("queued_retry", "e"),
            retry_at=bad,
        )
    assert delivery(store, row.delivery_id)[3] == 0 and n(store, "queue") == 1


def test_record_outcome_reads_the_clock_inside_the_transaction(store: Store, gw: Gateway) -> None:
    row = make(store)
    res = record_outcome(
        store,
        gw,
        send_worst_case_s=10,
        row=row,
        token=CLAIM,
        outcome=Outcome("deferred", None, "env_missing", counted=False),
        retry_at=retry_at,
    )
    assert res.deliver_after == NOW


# --- an exception escaping the call -----------------------------------------------------------


def test_record_escaped_marks_unknown_and_keeps_the_claim(store: Store) -> None:
    row = make(store)
    assert record_escaped(store, row=row, token=CLAIM)
    assert delivery(store, row.delivery_id)[:4] == ("unknown", None, "killed", 1)
    assert store.read("SELECT claimed_by FROM queue")[0][0] == CLAIM
    assert n(store, "dedup") == 1 and n(store, "event_ids") == 1


def test_record_escaped_is_fenced(store: Store) -> None:
    row = make(store)
    with store.write() as conn:
        conn.execute("UPDATE queue SET claimed_by = 'other'")
    assert not record_escaped(store, row=row, token=CLAIM)
    assert delivery(store, row.delivery_id)[0] == "sending"
