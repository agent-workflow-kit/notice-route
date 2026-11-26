"""``history`` end to end (#26): the filters, the order, the limit, the formats, hostile rows and
the refusals, through the real entry point with a real state directory.

The golden files are ``tests/golden/history/<name>.json``; rewrite them after a deliberate change
with ``NOTICE_ROUTE_UPDATE_GOLDEN=1`` and review the diff."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from notice_route.store import open_store
from tests.prune_support import DAY, NOW, add_delivery
from tests.send_support import Rig
from tests.test_cli_rules_validate import schema_errors, write_config

pytestmark = pytest.mark.usefixtures("send_environment")

GOLDEN = Path(__file__).resolve().parent / "golden" / "history"


def history(
    rig: Rig, *argv: str, env: dict[str, str] | None = None
) -> tuple[int, list[dict[str, Any]], str]:
    code, out, err = rig.run("history", "--json", *argv, env=env)
    assert out.endswith("\n") and out.count("\n") == 1, out
    out.encode("ascii")  # JSON is written as ASCII: no raw text of a row reaches a terminal
    doc = json.loads(out)
    if code == 0:
        assert schema_errors(doc, "history.json") == [], doc
    return code, doc, err


def ids(doc: list[dict[str, Any]]) -> list[int]:
    return [r["id"] for r in doc]


def check(name: str, doc: list[dict[str, Any]]) -> None:
    text = json.dumps(doc, indent=2) + "\n"
    path = GOLDEN / f"{name}.json"
    if os.environ.get("NOTICE_ROUTE_UPDATE_GOLDEN"):
        path.write_text(text)
    assert path.read_text() == text


def dataset(rig: Rig) -> None:
    """Eight rows with distinct sources, destinations, levels, outcomes and labels; ids 1 to 8."""
    add_delivery(
        rig, NOW - 3 * DAY, source="alpha", level="info", outcome="sent", object_id=5,
        event_id="17", labels='["repo=a","team=x"]', title="Build ok", rule="r1",
    )  # fmt: skip
    add_delivery(
        rig, NOW - 2 * DAY, source="beta", hook="post-fail", destination="chat2", level="warn",
        outcome="deferred", reason="quiet", deliver_after=NOW - DAY, labels='["repo=a"]',
    )  # fmt: skip
    add_delivery(
        rig, NOW - DAY, source="alpha", level="error", outcome="failed", code="credential_invalid",
        attempts=3, surfaced=1, labels='["repo=b"]', event_id='"abc"', object_id="pr-7",
    )  # fmt: skip
    add_delivery(
        rig, NOW - DAY, source="alpha", destination="chat2", level="action", outcome="dropped",
        reason="quiet",
    )  # fmt: skip
    add_delivery(
        rig, NOW - 3600, source="beta", destination=None, level="info", outcome="dropped",
        reason="no_rule",
    )  # fmt: skip
    add_delivery(rig, NOW - 100, source="gamma", level="warn", outcome="expired", code="expired")
    add_delivery(rig, NOW - 100, source="gamma", level="warn", outcome="sending")
    add_delivery(
        rig, NOW - 10, source="alpha", outcome="sent", count=3, dedupe="ab" * 32,
        title="t" * 100, labels='["repo=a"]',
    )  # fmt: skip


# --- empty and unreadable ------------------------------------------------------------------------


def test_no_state_db_is_an_empty_list_and_nothing_is_created(rig: Rig) -> None:
    code, doc, err = history(rig)
    assert (code, doc, err) == (0, [], "")
    assert not rig.state.exists()
    code, out, err = rig.run("history")
    assert (code, out, err) == (0, "", "")
    assert not rig.state.exists()


def test_it_needs_no_config(rig: Rig) -> None:
    dataset(rig)
    code, doc, _ = history(rig)
    assert code == 0 and len(doc) == 8


def test_a_database_that_cannot_be_used_is_exit_1_and_is_left_alone(rig: Rig) -> None:
    rig.state.mkdir(parents=True)
    rig.state.chmod(0o700)
    path = rig.state / "state.db"
    path.write_bytes(b"not a database" * 100)
    path.chmod(0o600)
    code, out, _ = rig.run("history", "--json")
    assert code == 1
    assert json.loads(out)["error"]["code"] == "internal"
    assert path.read_bytes() == b"not a database" * 100


def test_an_empty_state_db_file_is_an_empty_list(rig: Rig) -> None:
    rig.state.mkdir(parents=True)
    rig.state.chmod(0o700)
    path = rig.state / "state.db"
    path.write_bytes(b"")
    path.chmod(0o600)
    assert history(rig)[:2] == (0, [])
    assert path.read_bytes() == b""


# --- order, limit ----------------------------------------------------------------------------


def test_newest_first_ties_by_id_descending(rig: Rig) -> None:
    dataset(rig)
    code, doc, _ = history(rig)
    assert code == 0
    # ts: 8 (-10), then 7 and 6 tie at -100, 5 (-3600), 4 and 3 tie at -1d, 2, 1
    assert ids(doc) == [8, 7, 6, 5, 4, 3, 2, 1]
    check("all", doc)


def test_the_order_follows_ts_not_id(rig: Rig) -> None:
    add_delivery(rig, NOW - 50)
    add_delivery(rig, NOW - 500)
    add_delivery(rig, NOW - 5)
    assert ids(history(rig)[1]) == [3, 1, 2]


def test_limit_default_is_100_and_the_cap_is_1000(rig: Rig) -> None:
    from tests.prune_support import add_many_deliveries

    add_many_deliveries(rig, 1_100, NOW - 10)
    assert len(history(rig)[1]) == 100
    assert len(history(rig, "--limit", "1000")[1]) == 1000
    assert len(history(rig, "--limit", "7")[1]) == 7
    assert ids(history(rig, "--limit", "2")[1]) == [1100, 1099]
    assert len(history(rig, "--limit", "1")[1]) == 1


@pytest.mark.parametrize(
    "value", ["0", "1001", "-1", "+5", "05", " 5", "5 ", "1e2", "5.0", "", "x", "٣", "99999", "0x10"]
)
def test_a_limit_outside_1_to_1000_is_exit_2(rig: Rig, value: str) -> None:
    dataset(rig)
    code, out, _ = rig.run("history", "--json", "--limit", value)
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage"


# --- filters (golden) ------------------------------------------------------------------------

CASES: dict[str, list[str]] = {
    "since_duration": ["--since", "1d"],
    "since_time": ["--since", "2023-11-13T22:13:20Z"],
    "source": ["--source", "alpha"],
    "destination": ["--destination", "chat2"],
    "level": ["--level", "warn"],
    "outcome": ["--outcome", "dropped"],
    "label": ["--label", "repo=a"],
    "labels_both": ["--label", "repo=a", "--label", "team=x"],
    "limit": ["--limit", "3"],
    "combined": ["--source", "alpha", "--level", "info", "--since", "4d", "--label", "repo=a"],
    "combined_all": [
        "--since", "3d", "--source", "alpha", "--destination", "chat", "--level", "info",
        "--outcome", "sent", "--label", "repo=a", "--limit", "5",
    ],  # fmt: skip
    "none": ["--source", "nobody"],
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_filters(rig: Rig, name: str) -> None:
    dataset(rig)
    code, doc, _ = history(rig, *CASES[name])
    assert code == 0
    check(name, doc)


def test_filters_select_exactly_these_rows(rig: Rig) -> None:
    dataset(rig)
    expect = {
        "since_duration": [8, 7, 6, 5, 4, 3],
        "source": [8, 4, 3, 1],
        "destination": [4, 2],
        "level": [7, 6, 2],
        "outcome": [5, 4],
        "label": [8, 2, 1],
        "labels_both": [1],
        "limit": [8, 7, 6],
        "combined": [8, 1],
        "combined_all": [8, 1],
        "none": [],
    }
    for name, want in expect.items():
        assert ids(history(rig, *CASES[name])[1]) == want, name
    assert ids(history(rig, "--since", "4d")[1]) == [8, 7, 6, 5, 4, 3, 2, 1]


def test_since_is_inclusive_by_one_second_for_a_duration_and_a_time(rig: Rig) -> None:
    add_delivery(rig, NOW - 1000 - 1)
    add_delivery(rig, NOW - 1000)
    add_delivery(rig, NOW - 1000 + 1)
    # the wall clock is fixed at NOW
    assert ids(history(rig, "--since", "1000s")[1]) == [3, 2]
    assert ids(history(rig, "--since", "2023-11-14T21:56:40Z")[1]) == [3, 2]  # NOW - 1000
    assert ids(history(rig, "--since", "2023-11-14T21:56:41Z")[1]) == [3]
    assert ids(history(rig, "--since", "2023-11-14T21:56:40.999Z")[1]) == [3, 2]  # floored
    assert ids(history(rig, "--since", "0s")[1]) == []
    assert ids(history(rig, "--since", "1m")[1]) == []
    assert ids(history(rig, "--since", "999999999999999999d")[1]) == [3, 2, 1]
    assert ids(history(rig, "--since", "2100-01-01T00:00:00Z")[1]) == []


@pytest.mark.parametrize(
    "value",
    [
        "", "d", "5", "5x", "5 d", "-5d", "+5d", "5D", "1.5h", "1h30m", "٣d",
        "2023-11-14", "2023-11-14T00:00:00", "2023-11-14T00:00:00+00:00", "2023-11-14t00:00:00z",
        "2023-13-14T00:00:00Z", "2023-02-30T00:00:00Z", "2023-11-14T24:00:00Z",
        "2023-11-14T23:59:60Z", "0000-01-01T00:00:00Z", "2023-11-14T00:00:00Z\n", " 1d",
    ],
)  # fmt: skip
def test_a_since_outside_the_grammar_is_exit_2(rig: Rig, value: str) -> None:
    dataset(rig)
    code, out, _ = rig.run("history", "--json", "--since", value)
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage"


@pytest.mark.parametrize(
    "argv",
    [
        ["--source", "Alpha"],
        ["--source", ""],
        ["--source", "a" * 65],
        ["--source", "x' OR '1'='1"],
        ["--source", "alpha; DROP TABLE deliveries; --"],
        ["--source", "%"],
        ["--source", "alpha\n"],
        ["--destination", "c" * 33],
        ["--destination", "_x"],
        ["--destination", "chat%"],
        ["--level", "INFO"],
        ["--level", "critical"],
        ["--level", ""],
        ["--outcome", "suppressed"],
        ["--outcome", "redelivered"],
        ["--outcome", "SENT"],
        ["--label", "repo"],
        ["--label", "repo=a b"],
        ["--label", "Repo=a"],
        ["--label", "repo=a'; DROP TABLE deliveries; --"],
        ["--label", "repo=%"],
        ["--label", "repo=a\n"],
        ["--label", "="],
        ["--label", "k=" + "v" * 257],
    ],
)
def test_a_filter_value_outside_its_grammar_is_exit_2_and_never_reaches_sql(
    rig: Rig, argv: list[str]
) -> None:
    dataset(rig)
    code, out, err = rig.run("history", "--json", *argv)
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage"
    assert argv[-1][:8] not in out or len(argv[-1]) < 3  # the value is never echoed
    assert rig.query("SELECT COUNT(*) FROM deliveries") == [(8,)]


@pytest.mark.parametrize(
    "flag, one, two",
    [
        ("--since", "1d", "2d"),
        ("--source", "alpha", "beta"),
        ("--destination", "chat", "chat2"),
        ("--level", "info", "warn"),
        ("--outcome", "sent", "failed"),
        ("--limit", "1", "2"),
        ("--source", "alpha", "alpha"),
    ],
)
def test_a_repeated_filter_other_than_label_is_exit_2(
    rig: Rig, flag: str, one: str, two: str
) -> None:
    dataset(rig)
    code, out, _ = rig.run("history", "--json", flag, one, flag, two)
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage"


def test_repeated_labels_are_ands_and_a_repeat_of_one_label_is_harmless(rig: Rig) -> None:
    dataset(rig)
    assert ids(history(rig, "--label", "repo=a", "--label", "repo=a")[1]) == [8, 2, 1]
    assert ids(history(rig, "--label", "repo=a", "--label", "repo=b")[1]) == []


def test_a_label_matches_exactly_never_as_a_prefix_a_suffix_or_a_pattern(rig: Rig) -> None:
    add_delivery(rig, NOW - 1, labels='["repo=ab"]')
    add_delivery(rig, NOW - 2, labels='["xrepo=a"]')
    add_delivery(rig, NOW - 3, labels='["repo=a"]')
    add_delivery(rig, NOW - 4, labels='["repo=a,b"]')
    add_delivery(rig, NOW - 5, labels="not json")
    add_delivery(rig, NOW - 6, labels="{}")
    add_delivery(rig, NOW - 7, labels=None)
    assert ids(history(rig, "--label", "repo=a")[1]) == [3]


def test_more_than_16_distinct_labels_cannot_match_and_do_not_fail(rig: Rig) -> None:
    dataset(rig)
    argv = [x for i in range(17) for x in ("--label", f"k{i}=v")]
    assert history(rig, *argv)[:2] == (0, [])
    argv16 = [x for i in range(16) for x in ("--label", f"k{i}=v")]
    assert history(rig, *argv16)[:2] == (0, [])


def test_the_destination_filter_never_matches_the_rows_without_one(rig: Rig) -> None:
    dataset(rig)
    assert 5 not in ids(history(rig, "--destination", "chat")[1])


# --- what a row shows ------------------------------------------------------------------------


def test_a_row_holds_every_field_and_a_title_cut_to_100_characters(rig: Rig) -> None:
    dataset(rig)
    doc = history(rig)[1]
    row = {r["id"]: r for r in doc}
    assert row[1]["object_id"] == 5 and row[1]["event_id"] == 17 and row[1]["rule"] == "r1"
    assert row[3]["object_id"] == "pr-7" and row[3]["event_id"] == "abc"
    assert row[3]["surfaced"] is True and row[3]["code"] == "credential_invalid"
    assert row[2]["deliver_after"] == NOW - DAY and row[2]["hook"] == "post-fail"
    assert row[5]["destination"] is None and row[5]["reason"] == "no_rule"
    assert row[8]["count"] == 3 and row[8]["dedupe"] == "ab" * 32
    assert len(row[8]["title"]) == 100
    assert row[1]["ts"] == "2023-11-11T22:13:20.000Z"
    assert set(row[1]) == {
        "id", "ts", "source", "hook", "object_id", "rule", "destination", "level", "outcome",
        "reason", "code", "attempts", "count", "event_id", "dedupe", "labels", "surfaced",
        "deliver_after", "title",
    }  # fmt: skip


def test_a_stored_title_over_100_characters_is_cut_on_output(rig: Rig) -> None:
    add_delivery(rig, NOW, title="é" * 300)
    (row,) = history(rig)[1]
    assert row["title"] == "é" * 100


def test_a_templated_title_is_shown_and_message_text_never_is(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure(
        {"chat": {"notifier": n}},
        [
            {
                "name": "templ",
                "match": {"source": "tm*"},
                "level": "info",
                "notify": ["chat"],
                "title": "Deploy {{source}} {{source}} {{source}}",
                "message": "SECRETBODY {{message}}",
            },
            {"name": "pt", "match": {"level_min": "info"}, "notify": ["chat"]},
        ],
    )
    src = "tm" + "z" * 38
    body = json.dumps({"title": "build", "message": "hello"}).encode()
    assert rig.run("send", "--stdin-json", "--json", "--source", src, stdin=body)[0] == 0
    assert rig.send("passthrough text", source="plainsrc")[0] == 0
    code, out, _ = rig.run("history", "--json")
    rows = json.loads(out)
    assert schema_errors(rows, "history.json") == []
    by_rule = {r["rule"]: r for r in rows}
    assert by_rule["templ"]["title"] == (f"Deploy {src} {src} {src}")[:100]
    assert by_rule["pt"]["title"] is None
    assert "SECRETBODY" not in out and "hello" not in out and "passthrough" not in out


def test_rows_written_by_send_validate_against_the_schema(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure(
        {"chat": {"notifier": n}},
        [
            {"name": "drop-it", "match": {"source": "dropme"}, "drop": True},
            {"name": "pt", "match": {"source": "plain*", "level_min": "info"}, "notify": ["chat"]},
        ],
    )
    assert rig.send("one", source="plainsrc", extra=("--label", "repo=a"))[0] == 0
    assert rig.send("two", source="dropme")[0] == 0  # rule_drop
    assert rig.send("three", source="other")[0] == 22  # no_rule
    code, doc, _ = history(rig)
    assert code == 0
    by_reason = {r["reason"]: r for r in doc}
    assert by_reason["rule_drop"]["destination"] is None
    assert by_reason["no_rule"]["destination"] is None
    sent = [r for r in doc if r["outcome"] == "sent"]
    assert len(sent) == 1 and sent[0]["labels"] == ["repo=a"] and sent[0]["rule"] == "pt"
    assert history(rig, "--label", "repo=a")[1] == sent
    assert [r["reason"] for r in history(rig, "--outcome", "dropped")[1]] == ["no_rule", "rule_drop"]


# --- hostile rows ----------------------------------------------------------------------------


HOSTILE_TITLE = "\x1b[31mRED\x1b]0;pwned\x07\r\nSecond\x00line ‮⁦ ﻿‍😀"


def test_hostile_title_text_is_escaped_in_both_formats(rig: Rig) -> None:
    add_delivery(rig, NOW, title=HOSTILE_TITLE)
    (row,) = history(rig)[1]
    assert row["title"] == HOSTILE_TITLE[:100]
    code, out, err = rig.run("history")
    assert code == 0 and err == ""
    out.encode("ascii")
    assert out.count("\n") == 1  # the newline of the title did not split the line
    assert not any(ord(c) < 32 and c != "\n" for c in out) and "\x7f" not in out
    assert "\\x1b" in out and "\\x0a" in out and "\\x0d" in out and "\\x00" in out
    assert "\\\\" not in out.replace("\\\\x", "")  # nothing doubled but the escapes themselves


def test_a_title_that_is_not_utf8_never_fails_the_command(rig: Rig) -> None:
    with open_store(rig.gateway()) as store, store.write() as conn:
        conn.execute(
            "INSERT INTO deliveries (ts, source, level, outcome, destination, title)"
            " VALUES (?, 's', 'info', 'sent', 'chat', CAST(x'ff1b5b33316d41fe' AS TEXT))",
            (NOW,),
        )
    code, doc, _ = history(rig)
    assert code == 0 and len(doc) == 1
    assert doc[0]["title"] == "�\x1b[31mA�"
    code, out, _ = rig.run("history")
    assert code == 0
    out.encode("ascii")
    assert "\x1b" not in out


def test_a_row_with_a_column_outside_its_grammar_is_left_out_with_one_warning(rig: Rig) -> None:
    add_delivery(rig, NOW - 1)
    add_delivery(rig, NOW - 2, source="BAD\x1b[31m")
    add_delivery(rig, NOW - 3, destination="x\ny")
    add_delivery(rig, NOW - 4, outcome="suppressed")
    add_delivery(rig, NOW - 5, level="loud")
    add_delivery(rig, NOW - 6, reason="because")
    add_delivery(rig, NOW - 7, code="Not A Code")
    add_delivery(rig, NOW - 8, attempts=-1)
    add_delivery(rig, NOW - 9, count=0)
    add_delivery(rig, NOW - 10, dedupe="xyz")
    add_delivery(rig, NOW - 11, labels='["bad label"]')
    add_delivery(rig, NOW - 12, labels='[1]')
    add_delivery(rig, NOW - 13, labels=json_labels(17))
    add_delivery(rig, NOW - 14, event_id="[1]")
    add_delivery(rig, NOW - 15, event_id="not json")
    add_delivery(rig, NOW - 16, surfaced=0, deliver_after=-5)
    add_delivery(rig, NOW - 18, reason="no_rule", outcome="sent", destination=None)
    add_delivery(rig, NOW - 19, reason="quiet", destination=None)
    code, doc, err = history(rig)
    assert code == 0
    assert ids(doc) == [1]
    assert err == "notice-route: warning: 17 unreadable history rows left out\n"
    code, out, err = rig.run("history")
    assert code == 0 and out.count("\n") == 1 and "BAD" not in out + err


def json_labels(n: int) -> str:
    return json.dumps([f"k{i}=v" for i in range(n)])


def test_an_object_id_that_is_not_an_int_or_a_plain_string_shows_null(rig: Rig) -> None:
    add_delivery(rig, NOW - 1, object_id=1.5)
    add_delivery(rig, NOW - 2, object_id=b"\x00\x01")
    add_delivery(rig, NOW - 3, object_id=None)
    add_delivery(rig, NOW - 4, object_id=2**62)
    doc = history(rig)[1]
    assert [r["object_id"] for r in doc] == [None, None, None, 2**62]


def test_the_text_form_shows_one_line_per_row(rig: Rig) -> None:
    dataset(rig)
    code, out, err = rig.run("history", "--limit", "3")
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        '8 2023-11-14T22:13:10.000Z sent alpha chat info rule=- attempts=0 count=3 "' + "t" * 100 + '"',
        "7 2023-11-14T22:11:40.000Z sending gamma chat warn rule=- attempts=0 count=1",
        "6 2023-11-14T22:11:40.000Z expired gamma chat warn rule=- attempts=0 count=1",
    ]


# --- the command line ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--config", "x.yaml"],
        ["--actor", "auto:x"],
        ["--decision-id", "x"],
        ["--dry-run"],
        ["--destinations", "x"],
        ["--sourc", "alpha"],
        ["extra"],
        ["--quiet", "--verbose"],
        ["--source"],
        ["--limit"],
    ],
)
def test_usage_errors_are_exit_2(rig: Rig, argv: list[str]) -> None:
    dataset(rig)
    code, out, _ = rig.run("history", "--json", *argv)
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage"


def test_a_router_hook_environment_does_not_stop_history(rig: Rig) -> None:
    dataset(rig)
    code, doc, _ = history(rig, env={"NOTICE_ROUTE_HOOK": "1"})
    assert code == 0 and len(doc) == 8


def test_history_changes_nothing(rig: Rig) -> None:
    dataset(rig)
    before = {p.name: p.read_bytes() for p in rig.state.glob("state.db*") if p.suffix != ".shm"}
    history(rig, "--label", "repo=a")
    after = {p.name: p.read_bytes() for p in rig.state.glob("state.db*") if p.suffix != ".shm"}
    assert after == before
    assert rig.log_lines() == []
    write_config(rig.home, {"destinations": {}, "rules": []})  # a broken config does not matter
    assert history(rig)[0] == 0
