"""``send`` with real processes and faults (#22): locks, recording failures, escaped exceptions,
signals, parallel and killed senders, deadlines, and the injectable seams."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from notice_route import calling, cli, send
from notice_route.gateway import SYSTEM_TZ_DIRS, Gateway
from notice_route.notify import CallResult
from notice_route.queue import Lease, claim_due
from notice_route.report import worst_case_s
from notice_route.store import StoreError, open_store
from notice_route.validate import load_valid_config
from tests.send_support import NOW, Rig, RigClock

_F_SETPIPE_SZ = 1031  # Linux: fcntl.F_SETPIPE_SZ is not in every Python
_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = [
    pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database"),
    pytest.mark.usefixtures("send_environment"),
]


def only(doc: dict[str, Any]) -> dict[str, Any]:
    assert len(doc["destinations"]) == 1
    return doc["destinations"][0]


def use_seams(monkeypatch: pytest.MonkeyPatch, seams: send.Seams) -> None:
    real = send.run_send
    monkeypatch.setattr(
        cli,
        "run_send",
        lambda *a, **k: real(*a, **{**k, "seams": seams}),  # type: ignore[misc]
    )


# --- the send lock -----------------------------------------------------------------------------


def test_a_busy_send_lock_queues_a_retry_without_a_call_or_an_attempt(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}}, lock_wait="1s")
    gw = rig.gateway()
    with gw.open_state_dir() as state, gw.open_private_subdir(state, "locks") as locks:
        held = gw.open_file(locks, "chat.lock", mode="a", create=True)
        assert gw.try_lock(held, 0)
        try:
            code, doc, err = rig.send("hi")
        finally:
            held.close()
    d = only(doc)
    assert code == 0 and err == ""
    assert (d["outcome"], d["reason"], d["code"], d["attempts"], d["deliver_after"]) == (
        "queued_retry", "lock_busy", None, 0, NOW + 60,
    )  # fmt: skip
    assert n.events("invoke") == []
    assert rig.query("SELECT count FROM rate") == [(0,)]  # the hourly count was given back
    assert rig.query("SELECT kind, claimed_by FROM queue") == [("single", None)]


def test_the_wsl_kind_shares_one_lock_between_destinations(rig: Rig) -> None:
    n = rig.notifier("wsl-notice", sleep=0.3)
    rig.configure(
        {"a": {"notifier": n, "kind": "wsl-notice"}, "b": {"notifier": n, "kind": "wsl-notice"}},
        lock_wait="10s",
    )
    code, doc, _ = rig.send("hi")
    assert code == 0 and [d["outcome"] for d in doc["destinations"]] == ["sent", "sent"]
    order = [r["event"] for r in n.records() if r["event"] in ("invoke", "exit")]
    assert order == ["invoke", "exit", "invoke", "exit"]  # never two calls at once


def hold_lock(rig: Rig, name: str = "chat") -> Any:
    """Take ``locks/<name>.lock`` from the test; returns the open file (close it to release)."""
    gw = rig.gateway()
    with gw.open_state_dir() as state, gw.open_private_subdir(state, "locks") as locks:
        held = gw.open_file(locks, f"{name}.lock", mode="a", create=True)
    assert gw.try_lock(held, 0)
    return held


def signal_when_claimed(rig: Rig, signum: int, release: Any) -> threading.Thread:
    """A thread that waits (bounded) for a claimed queue row, sends ``signum`` to this process,
    gives the signal time to be handled, then calls ``release``."""

    def hit() -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if rig.query("SELECT COUNT(*) FROM queue WHERE claimed_by IS NOT NULL") == [(1,)]:
                    break
            except sqlite3.Error:
                pass
            time.sleep(0.02)
        os.kill(os.getpid(), signum)
        time.sleep(0.3)
        release()

    thread = threading.Thread(target=hit)
    thread.start()
    return thread


def test_a_signal_during_the_lock_wait_makes_no_call_and_releases_the_claim(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}}, lock_wait="20s")
    held = hold_lock(rig)
    thread = signal_when_claimed(rig, signal.SIGTERM, held.close)
    try:
        code, out, err = rig.run(
            "send", "--stdin-json", "--json", "--source", "s", stdin=b'{"message": "hi"}'
        )
    finally:
        thread.join()
    assert (code, out, err) == (128 + signal.SIGTERM, "", "")
    assert n.events("invoke") == []
    assert rig.query("SELECT outcome, attempts FROM deliveries") == [("sending", 0)]
    ((owner, expires),) = rig.query("SELECT claimed_by, claim_expires FROM queue")
    assert owner is not None and expires <= NOW  # released: the next flush may take it


def test_a_notifier_call_that_exits_the_thread_is_unknown_and_the_run_goes_on(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def leave(*_a: object, **_k: object) -> CallResult:
        raise SystemExit(3)

    use_seams(monkeypatch, send.Seams(call=leave))
    rig.configure({"chat": {"notifier": rig.notifier()}})
    code, doc, _ = rig.send("hi")
    d = only(doc)
    assert code == 12 and (d["outcome"], d["code"], d["attempts"]) == ("unknown", "killed", 1)


def test_a_recording_that_raises_is_one_destinations_problem_not_the_runs(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def surface(_conn: sqlite3.Connection, event: send.SurfaceEvent) -> None:
        raise RuntimeError("SURFACETEXT")

    use_seams(monkeypatch, send.Seams(surface=surface))
    good = rig.notifier("good")
    bad = rig.notifier("bad", error={"code": "credential_invalid"})
    rig.configure({"a": {"notifier": bad}, "b": {"notifier": good}})
    code, out, err = rig.run("send", "--message", "hi", "--source", "s")
    assert code == 1 and out == ""
    assert "a duplicate is possible" in err and "SURFACETEXT" not in err + rig.state_text()
    assert "notice-route: b: sent" in err and "notice-route: a: failed" in err
    assert rig.query("SELECT destination, outcome FROM deliveries ORDER BY id") == [
        ("a", "sending"), ("b", "sent"),
    ]  # fmt: skip
    assert sorted((x["destination"], x["outcome"]) for x in rig.log_lines()) == [
        ("a", "failed"), ("b", "sent"),
    ]  # fmt: skip
    assert len(good.events("invoke")) == 1


def test_a_result_that_is_recorded_on_the_second_try_after_a_busy_database(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = send.apply_outcome
    calls: list[int] = []

    def flaky(*a: Any, **k: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise StoreError("busy")
        return real(*a, **k)

    monkeypatch.setattr(send, "apply_outcome", flaky)
    rig.configure({"chat": {"notifier": rig.notifier()}})
    code, doc, err = rig.send("hi")
    assert code == 0 and only(doc)["outcome"] == "sent" and err == ""
    assert len(calls) == 2
    assert rig.query("SELECT outcome FROM deliveries") == [("sent",)]


def test_a_result_that_cannot_be_recorded_after_the_retry_still_exits_1(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def broken(*_a: Any, **_k: Any) -> Any:
        calls.append(1)
        raise StoreError("busy")

    monkeypatch.setattr(send, "apply_outcome", broken)
    rig.configure({"chat": {"notifier": rig.notifier()}})
    code, out, err = rig.run("send", "--message", "hi", "--source", "s", "--json")
    assert code == 1 and len(calls) == 2  # one retry, no more
    assert rig.query("SELECT COUNT(*) FROM queue WHERE claimed_by IS NOT NULL") == [(1,)]


def test_exit_1_wins_over_12_and_the_other_destinations_are_still_handled(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = send.apply_outcome

    def refuse_b(conn: Any, *, row: Any, **k: Any) -> Any:
        if row.destination == "b":
            raise StoreError("busy")
        return real(conn, row=row, **k)

    monkeypatch.setattr(send, "apply_outcome", refuse_b)
    good = rig.notifier("good")
    bad = rig.notifier("bad", error={"code": "credential_invalid"})
    rig.configure({"a": {"notifier": good}, "b": {"notifier": good}, "c": {"notifier": bad}})
    code, out, err = rig.run("send", "--message", "hi", "--source", "s", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "internal"
    assert rig.query("SELECT destination, outcome FROM deliveries ORDER BY id") == [
        ("a", "sent"), ("b", "sending"), ("c", "failed"),
    ]  # fmt: skip
    assert sorted((x["destination"], x["outcome"]) for x in rig.log_lines()) == [
        ("a", "sent"), ("b", "sent"), ("c", "failed"),
    ]  # fmt: skip


def test_the_hourly_count_is_given_back_for_the_hour_that_was_counted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notice_route.store import Store

    def no_read(self: Store, *_a: object, **_k: object) -> list[Any]:
        raise StoreError("internal")  # the hour must not come from a later read

    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}}, lock_wait="1s")
    held = hold_lock(rig)
    monkeypatch.setattr(Store, "read", no_read)

    def cross_the_hour() -> None:
        time.sleep(0.3)
        rig.clock.jump_wall(3600)

    thread = threading.Thread(target=cross_the_hour)
    thread.start()
    try:
        code, doc, _ = rig.send("hi")
    finally:
        thread.join()
        held.close()
    assert code == 0 and only(doc)["reason"] == "lock_busy"
    assert rig.query("SELECT hour, count FROM rate") == [(NOW - NOW % 3600, 0)]


# --- recording faults --------------------------------------------------------------------------


def test_a_result_that_cannot_be_recorded_exits_1_and_leaves_the_claim(rig: Rig) -> None:
    n = rig.notifier(sleep=0.4)
    rig.configure({"chat": {"notifier": n}})
    db = rig.state / "state.db"

    def hog() -> None:
        n.wait_for("invoke")
        conn = sqlite3.connect(db, timeout=0, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        log = rig.state / "log.jsonl"
        deadline = time.monotonic() + 30  # the log line is written once recording gave up
        while time.monotonic() < deadline and not (log.exists() and log.stat().st_size):
            time.sleep(0.02)
        conn.execute("ROLLBACK")
        conn.close()

    thread = threading.Thread(target=hog)
    thread.start()
    try:
        code, out, err = rig.run("send", "--message", "hi", "--source", "s", "--json")
    finally:
        thread.join()
    assert code == 1 and json.loads(out)["error"]["code"] == "internal"
    assert "a duplicate is possible" in err
    assert rig.query("SELECT outcome, attempts FROM deliveries") == [("sending", 0)]
    assert rig.query("SELECT COUNT(*) FROM queue WHERE claimed_by IS NOT NULL") == [(1,)]
    assert [x["outcome"] for x in rig.log_lines()] == ["sent"]  # logged: what the call said


def test_an_exception_escaping_the_call_is_unknown_and_the_row_stays_claimed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> CallResult:
        raise RuntimeError("EXCEPTIONTEXT")

    use_seams(monkeypatch, send.Seams(call=boom))
    rig.configure({"chat": {"notifier": rig.notifier()}})
    code, doc, err = rig.send("hi")
    d = only(doc)
    assert code == 12 and (d["outcome"], d["code"], d["attempts"]) == ("unknown", "killed", 1)
    assert "EXCEPTIONTEXT" not in json.dumps(doc) + err + rig.state_text()
    assert rig.query("SELECT outcome, code, attempts FROM deliveries") == [("unknown", "killed", 1)]
    assert rig.query("SELECT COUNT(*) FROM queue WHERE claimed_by IS NOT NULL") == [(1,)]
    assert rig.log_lines()[0]["code"] == "killed"


def test_a_call_that_was_not_started_is_an_internal_error_and_records_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_seams(monkeypatch, send.Seams(call=lambda *_a, **_k: CallResult("not_started")))
    rig.configure({"chat": {"notifier": rig.notifier()}})
    code, out, err = rig.run("send", "--message", "hi", "--source", "s", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "internal"
    assert rig.query("SELECT outcome FROM deliveries") == [("sending",)]
    assert rig.log_lines() == []


def test_an_error_from_the_send_lock_before_any_call_is_internal_and_counts_no_attempt(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_a: object, **_k: object) -> None:
        raise OSError("LOCKTEXT")

    monkeypatch.setattr(calling, "send_lock", broken)
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}})
    code, out, err = rig.run("send", "--message", "hi", "--source", "s", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "internal"
    assert "LOCKTEXT" not in out + err
    assert n.events("invoke") == []
    assert rig.query("SELECT outcome, attempts FROM deliveries") == [("sending", 0)]
    ((_, expires),) = rig.query("SELECT claimed_by, claim_expires FROM queue")
    assert expires <= NOW  # released, not left claimed
    assert rig.log_lines() == []


def test_the_surfacing_seam_runs_inside_the_recording_transaction(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[send.SurfaceEvent, bool]] = []

    def surface(conn: sqlite3.Connection, event: send.SurfaceEvent) -> None:
        seen.append((event, conn.in_transaction))

    use_seams(monkeypatch, send.Seams(surface=surface))
    rig.configure({"bad": {"notifier": rig.notifier("bad", error={"code": "credential_invalid"})}})
    rig.send("hi")
    ((event, in_txn),) = seen
    assert in_txn and (event.destination, event.outcome, event.code) == (
        "bad", "failed", "credential_invalid",
    )  # fmt: skip
    rig.configure({"ok": {"notifier": rig.notifier()}})
    rig.send("other")
    assert len(seen) == 1  # a sent result is not surfaced


def test_the_retry_schedule_seam_decides_the_due_time_and_the_end(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_seams(monkeypatch, send.Seams(retry_schedule=lambda kind, now: lambda attempts: now + 999))
    rig.configure({"chat": {"notifier": rig.notifier(error={"code": "rate_limited"})}})
    assert only(rig.send("a")[1])["deliver_after"] == NOW + 999
    use_seams(monkeypatch, send.Seams(retry_schedule=lambda kind, now: lambda attempts: None))
    d = only(rig.send("b")[1])
    assert (d["outcome"], d["code"]) == ("failed", "retries_exhausted")


def test_a_sender_killed_after_the_decision_and_before_the_call_leaves_a_claimed_row(
    rig: Rig, home: Path
) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}}, lock_wait="30s")
    held = hold_lock(rig)  # the call waits for the lock: decided, claimed, not called
    proc = rig.spawn("send", "--stdin-json", "--json", "--source", "s", stdin=b'{"message": "hi"}')
    deadline = time.monotonic() + 30
    claimed: list[tuple[Any, ...]] = []
    while time.monotonic() < deadline and not claimed:
        try:
            claimed = rig.query("SELECT claim_expires FROM queue WHERE claimed_by IS NOT NULL")
        except sqlite3.Error:
            pass
        time.sleep(0.02)
    assert claimed
    proc.kill()  # the exact handle this test started
    proc.communicate(timeout=10)
    held.close()
    assert n.events("invoke") == []
    assert rig.query("SELECT outcome, attempts FROM deliveries") == [("sending", 0)]
    ((expires,),) = claimed
    gw = Gateway(passwd_home=lambda: str(home))
    with open_store(gw) as store, store.write() as conn:
        assert claim_due(conn, "chat", Lease(expires - 1, 60), "t1") == []  # still claimed
        assert len(claim_due(conn, "chat", Lease(expires + 1, 60), "t2")) == 1  # due after it


def test_a_claim_lost_during_the_call_is_logged_and_shown_as_classified(rig: Rig) -> None:
    n = rig.notifier(sleep=0.5, error={"code": "rate_limited"})
    rig.configure({"chat": {"notifier": n}})

    def steal() -> None:
        n.wait_for("invoke")
        conn = sqlite3.connect(rig.state / "state.db", isolation_level=None)
        conn.execute("UPDATE queue SET claimed_by = 'another-process'")
        conn.close()

    thread = threading.Thread(target=steal)
    thread.start()
    try:
        code, doc, err = rig.send("hi")
    finally:
        thread.join()
    d = only(doc)
    assert code == 0 and (d["outcome"], d["code"], d["attempts"]) == (
        "queued_retry",
        "rate_limited",
        1,
    )
    assert d["deliver_after"] == NOW  # shown as classified: nothing is stored for a lost claim
    assert "the claim was lost" in err
    assert rig.query("SELECT outcome, attempts FROM deliveries") == [("sending", 0)]
    assert [(x["outcome"], x["code"]) for x in rig.log_lines()] == [
        ("queued_retry", "rate_limited")
    ]


# --- time --------------------------------------------------------------------------------------


def test_a_wall_clock_step_during_the_call_does_not_lose_the_result(rig: Rig) -> None:
    n = rig.notifier(sleep=0.5, error={"code": "rate_limited"})
    rig.configure({"chat": {"notifier": n}})

    def step() -> None:
        n.wait_for("invoke")
        rig.clock.jump_wall(-86400)

    thread = threading.Thread(target=step)
    thread.start()
    try:
        code, doc, _ = rig.send("hi")
    finally:
        thread.join()
    d = only(doc)
    assert code == 0 and d["outcome"] == "queued_retry"
    assert d["deliver_after"] == NOW - 86400 + 60 and doc["duration_ms"] >= 500


def test_a_call_past_its_deadline_is_killed_and_unknown(rig: Rig) -> None:
    rig.clock = RigClock(
        rate=60.0
    )  # the 11 s deadline and the grace pass in a fraction of a second
    n = rig.notifier(hang=True, max_seconds=60)
    rig.configure({"chat": {"notifier": n, "timeout": 1}})
    started = time.monotonic()
    code, doc, _ = rig.send("hi")
    d = only(doc)
    assert code == 12 and (d["outcome"], d["code"]) == ("unknown", "killed")
    assert doc["duration_ms"] >= 11_000  # the deadline (timeout plus 10 s) passed on the clock
    assert time.monotonic() - started < 30  # generous: only a hung run fails this
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]


def test_a_hung_call_with_the_default_settings_ends_within_the_worst_case(rig: Rig) -> None:
    rig.clock = RigClock(rate=30.0)
    n = rig.notifier(hang=True, max_seconds=60)
    rig.configure({"chat": {"notifier": n}})
    config = load_valid_config(rig.gateway(), None)
    assert config is not None
    code, doc, _ = rig.send("hi")
    assert code == 12 and only(doc)["code"] == "killed"
    assert 20_000 <= doc["duration_ms"] <= worst_case_s(config) * 1000  # default timeout 10 s


def test_a_descendant_that_left_the_group_cannot_hold_the_router(rig: Rig) -> None:
    rig.clock = RigClock(rate=20.0)
    n = rig.notifier(grandchild={"setsid": True, "hold_stdout": True, "lifetime": 8})
    rig.configure({"chat": {"notifier": n}})
    started = time.monotonic()
    code, doc, _ = rig.send("hi")
    assert code == 0 and only(doc)["outcome"] == "sent"
    assert doc["duration_ms"] < 8_000  # the grandchild lives 8 s of real time: not waited for
    assert time.monotonic() - started < 30


# --- signals -----------------------------------------------------------------------------------


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_after_the_decision_and_before_the_call_releases_the_claims(
    rig: Rig, signum: int
) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}, "late": {"notifier": n, "quiet": "night"}})

    class Signalling(Gateway):
        def new_token(self) -> str:  # called inside the decision transaction
            os.kill(os.getpid(), signum)
            return super().new_token()

    base = rig.gateway(b'{"message": "hi", "level": "warn"}')
    gw = Signalling(
        passwd_home=base.passwd_home,
        clock=rig.clock,
        exec_check_from=base.exec_check_from,
        stdin_fd=base.stdin_fd,
        environ=base.environ,
    )
    code, out, err = rig.run("send", "--stdin-json", "--json", "--source", "s", gateway=gw)
    assert (code, out, err) == (128 + signum, "", "")
    assert n.events("invoke") == []
    assert rig.query("SELECT destination, outcome FROM deliveries ORDER BY id") == [
        ("chat", "sending"), ("late", "deferred"),
    ]  # fmt: skip
    ((owner, expires),) = rig.query(
        "SELECT claimed_by, claim_expires FROM queue WHERE claimed_by IS NOT NULL"
    )
    assert owner is not None and expires <= NOW  # released: the next flush may take it
    assert [(x["destination"], x["outcome"]) for x in rig.log_lines()] == [("late", "deferred")]


def test_a_signal_while_reading_input_exits_128_plus_n_and_writes_nothing(
    rig: Rig, home: Path
) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    proc = rig.spawn("send", "--stdin-json", "--json", stdin=None)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:  # wait until the handler is installed (SigCgt bit 14)
        try:
            text = Path(f"/proc/{proc.pid}/status").read_text()
        except OSError:
            break
        mask = next(int(x.split()[1], 16) for x in text.splitlines() if x.startswith("SigCgt"))
        if mask & (1 << (signal.SIGTERM - 1)):
            break
        time.sleep(0.02)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=10)
    assert (proc.returncode, out, err) == (128 + signal.SIGTERM, b"", b"")
    assert not (home / ".local").exists()


def test_a_signal_while_the_output_is_blocked_ends_the_process(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    read_end, write_end = os.pipe()
    try:
        fcntl.fcntl(write_end, _F_SETPIPE_SZ, 4096)
        os.set_blocking(write_end, False)
        try:
            while True:  # a reader that never reads: the pipe is full before the child starts
                os.write(write_end, b"x" * 512)
        except BlockingIOError:
            pass
        os.set_blocking(write_end, True)
        proc = rig.spawn(
            "send", "--stdin-json", "--json", "--source", "s",
            stdin=b'{"message": "hi"}', stdout=write_end,
        )  # fmt: skip
        # the call and its recording take a moment; the document then blocks on the full pipe
        deadline = time.monotonic() + 30
        sent: list[tuple[Any, ...]] = []
        while time.monotonic() < deadline and not sent:
            try:
                sent = rig.query("SELECT 1 FROM deliveries WHERE outcome = 'sent'")
            except sqlite3.Error:
                pass
            time.sleep(0.05)
        assert sent
        time.sleep(0.5)
        assert proc.poll() is None  # blocked in the write
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        assert proc.returncode == 128 + signal.SIGTERM
        assert time.monotonic() - started < 5
    finally:
        os.close(read_end)
        os.close(write_end)


def test_a_signal_after_the_call_started_still_reports_the_result(rig: Rig) -> None:
    n = rig.notifier(sleep=0.6)
    rig.configure({"chat": {"notifier": n}})

    def hit() -> None:
        n.wait_for("invoke")
        os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=hit)
    thread.start()
    try:
        code, doc, _ = rig.send("hi")
    finally:
        thread.join()
    assert code == 0 and only(doc)["outcome"] == "sent"
    assert rig.query("SELECT outcome FROM deliveries") == [("sent",)]


# --- processes ---------------------------------------------------------------------------------


def test_parallel_senders_of_one_event_make_exactly_one_call(rig: Rig) -> None:
    n = rig.notifier(sleep=0.3)
    rule = {
        "name": "h",
        "match": {"source": "agent-spawn"},
        "notify": ["chat"],
        "level": "warn",
        "message": "state {{state}}",
    }
    rig.configure({"chat": {"notifier": n}}, rules=[rule])
    body = json.dumps(
        {"tool": "agent-spawn", "hook": "post-state-change", "event_id": 5, "state": "done"}
    ).encode()
    procs = [rig.spawn("send", "--stdin-json", "--json", stdin=body) for _ in range(4)]
    docs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        docs.append(json.loads(out)["destinations"][0])
    assert sorted(d["outcome"] for d in docs) == ["sent", "suppressed", "suppressed", "suppressed"]
    assert {d["reason"] for d in docs if d["outcome"] == "suppressed"} <= {"redelivered", "repeat"}
    assert len(n.events("invoke")) == 1


def test_parallel_senders_to_one_destination_never_overlap_their_calls(rig: Rig) -> None:
    n = rig.notifier(sleep=0.2)
    rig.configure({"chat": {"notifier": n}}, lock_wait="30s")
    procs = [
        rig.spawn(
            "send",
            "--stdin-json",
            "--json",
            "--source",
            "s",
            stdin=f'{{"message": "m{i}"}}'.encode(),
        )
        for i in range(3)
    ]
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        assert json.loads(out)["destinations"][0]["outcome"] == "sent"
    order = [r["event"] for r in n.records() if r["event"] in ("invoke", "exit")]
    assert order == ["invoke", "exit"] * 3


def test_a_sender_killed_during_the_call_leaves_one_claimed_row_a_later_run_resends(
    rig: Rig, home: Path
) -> None:
    n = rig.notifier(hang=True, max_seconds=30)
    rig.configure({"chat": {"notifier": n}})
    proc = rig.spawn("send", "--stdin-json", "--json", "--source", "s", stdin=b'{"message": "hi"}')
    n.wait_for("invoke", timeout=30)
    proc.kill()  # the exact handle this test started
    proc.communicate(timeout=10)
    n.kill_tracked()
    assert rig.query("SELECT outcome, attempts FROM deliveries") == [("sending", 0)]
    ((owner, expires),) = rig.query("SELECT claimed_by, claim_expires FROM queue")
    assert owner is not None and expires > time.time()
    gw = Gateway(passwd_home=lambda: str(home))
    with open_store(gw) as store:
        with store.write() as conn:  # while the claim lives nobody may take the row
            assert claim_due(conn, "chat", Lease(int(time.time()), 60), "t1") == []
        with store.write() as conn:  # after it lapses exactly one later run does
            late = Lease(expires + 1, 60)
            assert len(claim_due(conn, "chat", late, "t2")) == 1
            assert claim_due(conn, "chat", late, "t3") == []
