"""``prune`` end to end (#26): every table, every retention boundary by one second, live claims,
the dry run, batches, clock steps and the refusals, through the real entry point with a real
state directory under a private home."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from notice_route import cli, prune as prune_mod
from notice_route.send import Seams, SurfaceEvent
from tests.flush_support import checked_log, seed
from tests.prune_support import (
    DAY,
    NOW,
    add_delivery,
    add_event_id,
    add_many_deliveries,
    add_many_event_ids,
    add_rate,
    add_surfaced,
    add_window,
    counts,
    deleted,
    prune,
)
from tests.send_support import Rig
from tests.test_cli_rules_validate import write_config

pytestmark = pytest.mark.usefixtures("send_environment")

HOUR = 3600


def configure(rig: Rig, *, enabled: bool = True, **top: Any) -> Any:
    n = rig.notifier()
    config: dict[str, Any] = {
        "destinations": {"chat": {"kind": "slack-notice", "cmd": n.cmd, "enabled": enabled}},
        "rules": [{"name": "pt", "match": {"level_min": "info"}, "notify": ["chat"]}],
    }
    config.update(top)
    write_config(rig.home, config)
    return n


def make_db(rig: Rig) -> None:
    """Create ``state.db`` (migrated, empty)."""
    add_delivery(rig, NOW)


def total(rig: Rig) -> int:
    return sum(counts(rig).values())


# --- no database -----------------------------------------------------------------------------


@pytest.mark.parametrize("dry", [[], ["--dry-run"]])
def test_no_state_db_deletes_nothing_exit_0_and_creates_nothing(rig: Rig, dry: list[str]) -> None:
    configure(rig)
    code, doc, _ = prune(rig, *dry)
    assert code == 0
    assert doc == {"dry_run": bool(dry), "deleted": deleted()}
    assert not rig.state.exists()


def test_no_config_and_no_state_db_is_exit_0(rig: Rig) -> None:
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted())
    assert not rig.state.exists()


# --- history -----------------------------------------------------------------------------------


def test_history_boundary_is_one_second_and_default_is_30_days(rig: Rig) -> None:
    configure(rig)
    keep = add_delivery(rig, NOW - 30 * DAY)
    gone = add_delivery(rig, NOW - 30 * DAY - 1)
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted(history=1))
    assert [r[0] for r in rig.query("SELECT id FROM deliveries")] == [keep]
    assert gone != keep


def test_history_retention_comes_from_the_config(rig: Rig) -> None:
    configure(rig, retention={"history": "2d"})
    add_delivery(rig, NOW - 2 * DAY)
    add_delivery(rig, NOW - 2 * DAY - 1)
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted(history=1))
    assert counts(rig)["history"] == 1


def test_history_never_deletes_a_row_a_queue_row_or_a_pending_alert_needs(rig: Rig) -> None:
    configure(rig, retention={"history": "1d"})
    old = NOW - 5 * DAY
    # a claimed row (live lease) whose delivery is old, and an unclaimed one that is too young to
    # expire is impossible here: its created_at is old as well, so give it a live claim
    live, _ = seed(rig, now=old, created_at=NOW - 100, claim="t", claim_expires=NOW + 50)
    pending = add_delivery(rig, old)
    add_surfaced(rig, "2023-11-14", processed=0, delivery_id=pending)
    done = add_delivery(rig, old)
    add_surfaced(rig, "2023-11-14", processed=1, delivery_id=done, code="other")
    plain = add_delivery(rig, old)
    code, doc, _ = prune(rig)
    assert code == 0
    ids = {r[0] for r in rig.query("SELECT id FROM deliveries")}
    assert ids == {live, pending}
    assert doc["deleted"]["history"] == 2  # done and plain
    assert plain not in ids and done not in ids


# --- event ids and dedup ---------------------------------------------------------------------


def test_event_id_boundary_is_one_second(rig: Rig) -> None:
    configure(rig, retention={"event_ids": "1d"})
    add_event_id(rig, NOW - DAY, 1)
    add_event_id(rig, NOW - DAY - 1, 2)
    add_event_id(rig, NOW + 5, 3)  # from the future (a clock that stepped back): not pruned
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted(event_ids=1))
    assert sorted(r[0] for r in rig.query("SELECT event_id FROM event_ids")) == ["1", "3"]


def test_dedup_window_is_deleted_when_it_has_ended(rig: Rig) -> None:
    configure(rig)
    add_window(rig, NOW - 100, 100, "a")  # ends exactly now: ended
    add_window(rig, NOW - 100, 101, "b")  # one second left
    add_window(rig, NOW + 10, 5, "c")  # starts in the future: kept
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted(dedup=1))
    assert sorted(r[0][0] for r in rig.query("SELECT key_digest FROM dedup")) == ["b", "c"]


# --- surfaced --------------------------------------------------------------------------------


def test_surfaced_rows_go_when_their_period_is_before_the_day_two_days_ago(rig: Rig) -> None:
    configure(rig)
    # NOW is 2023-11-14 22:13:20 UTC; two days earlier is 2023-11-12
    add_surfaced(rig, "2023-11-11", code="a")
    add_surfaced(rig, "2023-11-11T23", code="b")
    add_surfaced(rig, "2023-11-12", code="c")
    add_surfaced(rig, "2023-11-12T00", code="d")
    add_surfaced(rig, "2023-11-14", code="e")
    add_surfaced(rig, "2023-11-01", code="f", processed=0)  # a pending alert is never deleted
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted(surfaced=2))
    assert sorted(r[0] for r in rig.query("SELECT code FROM surfaced")) == ["c", "d", "e", "f"]


# --- counters --------------------------------------------------------------------------------


def test_counters_older_than_two_days_go_and_younger_stay_by_one_second(rig: Rig) -> None:
    configure(rig)
    hour = NOW - NOW % HOUR
    old = hour - 3 * DAY
    edge = (NOW - 2 * DAY) - (NOW - 2 * DAY) % HOUR  # the hour that contains now minus 2 days
    add_rate(rig, old)
    add_rate(rig, old + HOUR, cls="error")
    add_rate(rig, edge)  # its end is after now minus 2 days: kept
    add_rate(rig, hour - HOUR)  # the previous hour
    add_rate(rig, hour)  # the current hour
    add_rate(rig, hour + HOUR)  # the future
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted(rate=2))
    assert counts(rig)["rate"] == 4


@pytest.mark.parametrize(("extra", "gone"), [(0, 0), (1, 1)])
def test_a_counter_hour_is_older_than_two_days_one_second_after_its_end(
    rig: Rig, extra: int, gone: int
) -> None:
    configure(rig)
    hour = 1_699_999_200  # a multiple of 3600
    assert hour % HOUR == 0
    add_rate(rig, hour)
    rig.clock.jump_wall(hour + HOUR + 2 * DAY + extra - NOW)
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]["rate"]) == (0, gone)


# --- the queue -------------------------------------------------------------------------------


def test_queue_expiry_boundary_and_what_the_delivery_becomes(rig: Rig) -> None:
    configure(rig)
    did, _ = seed(rig, now=NOW - DAY, created_at=NOW - DAY, outcome="deferred", reason="quiet")
    young, _ = seed(rig, now=NOW - DAY + 1, created_at=NOW - DAY + 1)
    add_window(rig, NOW, 1000, "z", delivery_id=did)
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted(queue=1))
    assert [r[0] for r in rig.query("SELECT delivery_id FROM queue")] == [young]
    assert rig.query("SELECT outcome, code, reason, deliver_after FROM deliveries WHERE id = ?", (did,)) == [
        ("expired", "expired", "quiet", None)
    ]
    assert rig.query("SELECT COUNT(*) FROM dedup") == [(0,)]  # released with the delivery


def test_a_live_claim_survives_however_old_the_row_is(rig: Rig) -> None:
    configure(rig)
    _, live = seed(rig, now=NOW - 9 * DAY, created_at=NOW - 9 * DAY, claim="t", claim_expires=NOW + 1)
    _, lapsed = seed(rig, now=NOW - 9 * DAY, created_at=NOW - 9 * DAY, claim="t", claim_expires=NOW)
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted(queue=1))
    assert [r[0] for r in rig.query("SELECT id FROM queue")] == [live]
    assert lapsed != live


def test_a_claim_far_beyond_one_lease_counts_as_lapsed(rig: Rig) -> None:
    """The one lease predicate: a claim that outlasts a whole lease (a clock that stepped back,
    or a bad writer) is unclaimed, as for flush."""
    configure(rig)
    seed(rig, now=NOW - 2 * DAY, created_at=NOW - 2 * DAY, claim="t", claim_expires=NOW + 10**7)
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted(queue=1))


@pytest.mark.parametrize("state", ["unknown", "disabled", "env_missing"])
def test_rows_of_any_destination_state_expire_by_age(rig: Rig, state: str) -> None:
    if state == "env_missing":
        n = rig.notifier()
        write_config(
            rig.home,
            {
                "destinations": {
                    "chat": {"kind": "slack-notice", "cmd": n.cmd, "env": ["NR_UNSET_VAR"]}
                },
                "rules": [{"name": "pt", "match": {"level_min": "info"}, "notify": ["chat"]}],
            },
        )
        dest = "chat"
    elif state == "disabled":
        configure(rig, enabled=False)
        dest = "chat"
    else:
        configure(rig)
        dest = "gone"
    did, _ = seed(rig, dest, now=NOW - 3 * DAY, created_at=NOW - 3 * DAY, reason="env_missing")
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted(queue=1))
    assert rig.query("SELECT outcome, code, reason FROM deliveries WHERE id = ?", (did,)) == [
        ("expired", "expired", "env_missing")
    ]


def test_prune_works_without_a_config_and_with_a_disabled_router(rig: Rig) -> None:
    seed(rig, now=NOW - 3 * DAY, created_at=NOW - 3 * DAY)
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted(queue=1))
    configure(rig, enabled=True, **{"enabled": False})
    seed(rig, now=NOW - 3 * DAY, created_at=NOW - 3 * DAY)
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted(queue=1))


def test_expired_rows_are_logged_and_surfaced_only_for_a_configured_enabled_destination(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = rig.notifier()
    other = rig.notifier("ha-notice")
    write_config(
        rig.home,
        {
            "destinations": {
                "chat": {"kind": "slack-notice", "cmd": n.cmd},
                "off": {"kind": "ha-notice", "cmd": other.cmd, "enabled": False},
            },
            "rules": [{"name": "pt", "match": {"level_min": "info"}, "notify": ["chat"]}],
        },
    )
    events: list[SurfaceEvent] = []
    real = prune_mod.run_prune
    seams = Seams(surface=lambda conn, event: events.append(event))
    monkeypatch.setattr(cli, "run_prune", lambda *a, **k: real(*a, **{**k, "seams": seams}))
    chat, _ = seed(rig, "chat", now=NOW - 2 * DAY, created_at=NOW - 2 * DAY, source="alpha", level="warn")
    seed(rig, "off", now=NOW - 2 * DAY, created_at=NOW - 2 * DAY)
    seed(rig, "gone", now=NOW - 2 * DAY, created_at=NOW - 2 * DAY)
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]["queue"]) == (0, 3)
    assert [(e.delivery_id, e.destination, e.kind, e.source, e.level, e.outcome, e.code) for e in events] == [
        (chat, "chat", "slack-notice", "alpha", "warn", "expired", "expired")
    ]
    lines = checked_log(rig)
    assert sorted(x["destination"] for x in lines) == ["chat", "gone", "off"]
    assert {x["outcome"] for x in lines} == {"expired"}
    assert all(x["actor"].startswith("auto:") for x in lines)


def test_no_log_line_when_the_config_turns_the_log_off(rig: Rig) -> None:
    configure(rig, log=False)
    seed(rig, now=NOW - 2 * DAY, created_at=NOW - 2 * DAY)
    assert prune(rig)[1]["deleted"]["queue"] == 1
    assert rig.log_lines() == []


# --- the dry run -----------------------------------------------------------------------------


def populate(rig: Rig) -> None:
    configure(rig, retention={"history": "1d", "event_ids": "1d"})
    for i in range(7):
        add_delivery(rig, NOW - 3 * DAY - i)
    add_delivery(rig, NOW - 10)
    for i in range(5):
        add_event_id(rig, NOW - 2 * DAY, i)
    add_event_id(rig, NOW - 5, 99)
    for i, key in enumerate("abc"):
        add_window(rig, NOW - 500, 100 + i, key)
    add_window(rig, NOW, 1000, "d")
    add_surfaced(rig, "2023-11-01", code="a")
    add_surfaced(rig, "2023-11-14", code="b")
    add_rate(rig, 1_699_000_000 - 1_699_000_000 % HOUR)
    add_rate(rig, NOW - NOW % HOUR)
    seed(rig, now=NOW - 2 * DAY, created_at=NOW - 2 * DAY)
    seed(rig, now=NOW - 2 * DAY, created_at=NOW - 2 * DAY, claim="t", claim_expires=NOW + 9)
    seed(rig, now=NOW - 10, created_at=NOW - 10)


def test_dry_run_counts_exactly_what_the_real_run_deletes_and_writes_nothing(rig: Rig) -> None:
    populate(rig)
    before_counts = counts(rig)
    before_files = {p.name: p.read_bytes() for p in rig.state.glob("state.db*") if p.suffix != ".shm"}
    code, dry, _ = prune(rig, "--dry-run")
    assert code == 0 and dry["dry_run"] is True
    assert counts(rig) == before_counts
    after_files = {p.name: p.read_bytes() for p in rig.state.glob("state.db*") if p.suffix != ".shm"}
    assert after_files == before_files
    assert rig.log_lines() == []
    code, real, _ = prune(rig)
    assert code == 0 and real["dry_run"] is False
    assert real["deleted"] == dry["deleted"]
    assert dry["deleted"] == deleted(history=7, event_ids=5, dedup=3, surfaced=1, rate=1, queue=1)
    code, again, _ = prune(rig, "--dry-run")
    assert again["deleted"] == deleted()


def test_prune_is_idempotent(rig: Rig) -> None:
    populate(rig)
    prune(rig)
    snapshot = counts(rig)
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted())
    assert counts(rig) == snapshot


# --- batches ---------------------------------------------------------------------------------


def test_more_than_one_batch_completes_and_each_transaction_deletes_at_most_1000(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(rig, retention={"history": "1d", "event_ids": "1d"})
    add_many_deliveries(rig, 2_345, NOW - 5 * DAY)
    add_many_event_ids(rig, 2_100, NOW - 5 * DAY)
    keep = add_delivery(rig, NOW)
    sizes: list[int] = []
    real = sqlite3.Cursor
    del real
    original = prune_mod.run_prune

    def traced(*a: Any, **k: Any) -> Any:
        return original(*a, **k)

    monkeypatch.setattr(cli, "run_prune", traced)
    monkeypatch.setattr(prune_mod, "BATCH", 1000)
    monkeypatch.setattr(prune_mod, "_on_batch", sizes.append)
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted(history=2_345, event_ids=2_100))
    assert [r[0] for r in rig.query("SELECT id FROM deliveries")] == [keep]
    assert sizes and max(sizes) <= 1000
    assert sorted(sizes, reverse=True)[:2] == [1000, 1000]


def test_dry_run_counts_beyond_one_batch(rig: Rig) -> None:
    configure(rig, retention={"history": "1d", "event_ids": "1d"})
    add_many_deliveries(rig, 1_500, NOW - 5 * DAY)
    code, doc, _ = prune(rig, "--dry-run")
    assert (code, doc["deleted"]) == (0, deleted(history=1_500))


# --- clocks ----------------------------------------------------------------------------------


def test_a_clock_stepped_back_deletes_nothing_and_does_not_stall(rig: Rig) -> None:
    populate(rig)
    rig.clock.jump_wall(-30 * DAY)
    before = counts(rig)
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted())
    assert counts(rig) == before


def test_a_clock_stepped_forward_ends_old_rows_but_never_a_live_claim(rig: Rig) -> None:
    configure(rig)
    _, live = seed(rig, now=NOW, created_at=NOW, claim="t", claim_expires=NOW + 30)
    seed(rig, now=NOW, created_at=NOW)
    rig.clock.jump_wall(DAY)  # the claim lapsed at +30 s: only a forward step of a day reaches it
    code, doc, _ = prune(rig)
    assert code == 0 and doc["deleted"]["queue"] == 2
    assert live


# --- the command line ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--config", "x.yaml"],
        ["--actor", "auto:x"],
        ["--decision-id", "x"],
        ["--destination", "chat"],
        ["--dry"],
        ["extra"],
        ["--quiet", "--verbose"],
    ],
)
def test_usage_errors_are_exit_2_and_change_nothing(rig: Rig, argv: list[str]) -> None:
    configure(rig)
    seed(rig, now=NOW - 3 * DAY, created_at=NOW - 3 * DAY)
    before = counts(rig)
    code, out, _ = rig.run("prune", "--json", *argv)
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage"
    assert counts(rig) == before


def test_a_router_hook_environment_does_not_stop_prune(rig: Rig) -> None:
    configure(rig)
    seed(rig, now=NOW - 3 * DAY, created_at=NOW - 3 * DAY)
    code, doc, _ = prune(rig, env={"NOTICE_ROUTE_HOOK": "1"})
    assert (code, doc["deleted"]) == (0, deleted(queue=1))


def test_text_output_lists_each_table_and_quiet_prints_nothing(rig: Rig) -> None:
    populate(rig)
    code, out, err = rig.run("prune", "--dry-run")
    assert code == 0 and err == ""
    assert out.splitlines() == [
        "history: 7",
        "event_ids: 5",
        "dedup: 3",
        "surfaced: 1",
        "rate: 1",
        "queue: 1",
    ]
    code, out, err = rig.run("prune", "--quiet")
    assert (code, out, err) == (0, "", "")
    assert counts(rig)["queue"] == 2


def test_a_bad_config_is_exit_2_and_deletes_nothing(rig: Rig) -> None:
    configure(rig, retention={"history": "1h"})
    seed(rig, now=NOW - 3 * DAY, created_at=NOW - 3 * DAY)
    code, out, _ = rig.run("prune", "--json")
    assert code == 2
    assert json.loads(out)["error"]["code"] == "config_invalid"
    assert counts(rig)["queue"] == 1


def test_a_database_that_cannot_be_used_is_exit_1(rig: Rig) -> None:
    configure(rig)
    rig.state.mkdir(parents=True)
    rig.state.chmod(0o700)
    path = rig.state / "state.db"
    path.write_bytes(b"not a database" * 100)
    path.chmod(0o600)
    for argv in (["prune", "--json"], ["prune", "--dry-run", "--json"]):
        code, out, _ = rig.run(*argv)
        assert code == 1
        assert json.loads(out)["error"]["code"] == "internal"
    assert path.read_bytes() == b"not a database" * 100


def test_the_state_dir_may_exist_without_a_database(rig: Rig) -> None:
    configure(rig)
    rig.state.mkdir(parents=True)
    rig.state.chmod(0o700)
    code, doc, _ = prune(rig)
    assert (code, doc["deleted"]) == (0, deleted())
    assert not (rig.state / "state.db").exists()
    assert isinstance(rig.state, Path)
