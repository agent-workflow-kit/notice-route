"""The digest text of #24 (docs/interface.md, Deferral and digest): ordering, lines, caps and
the handling of stored text, on :func:`notice_route.digest.build`."""

from __future__ import annotations

import pytest

from notice_route.digest import HEAD_CHARS, MAX_LINES, build
from notice_route.queue import QueueRow


def row(
    id: int,
    *,
    level: str = "warn",
    source: str = "s",
    title: str | None = "T",
    message: str = "m",
    rule: str | None = "r",
    link: str | None = None,
) -> QueueRow:
    return QueueRow(
        id, id * 10, "chat", "held", title, message, link, level, source, rule, "ab" * 32, 0, 0,
        None, None,
    )  # fmt: skip


def test_the_title_counts_every_row_and_the_level_is_the_highest() -> None:
    d = build([row(1, level="info"), row(2, level="error"), row(3, level="warn")])
    assert (d.title, d.level) == ("3 held notifications", "error")


def test_lines_are_ordered_by_level_from_the_highest_then_oldest_first() -> None:
    rows = [
        row(1, level="info", title="i1"),
        row(2, level="warn", title="w2"),
        row(3, level="error", title="e3"),
        row(4, level="warn", title="w4"),
        row(5, level="action", title="a5"),
        row(6, level="error", title="e6"),
    ]
    d = build(rows)
    assert d.message.splitlines() == [
        "error s: e3 - m",
        "error s: e6 - m",
        "action s: a5 - m",
        "warn s: w2 - m",
        "warn s: w4 - m",
        "info s: i1 - m",
    ]
    assert [r.id for r in d.rows] == [3, 6, 5, 2, 4, 1]


def test_the_order_does_not_depend_on_the_input_order() -> None:
    rows = [row(i, level="warn", title=f"t{i}") for i in (4, 2, 3, 1)]
    assert [r.id for r in build(rows).rows] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("n", "lines", "tail"),
    [(2, 2, None), (10, 10, None), (11, 10, "and 1 more"), (25, 10, "and 15 more")],
)
def test_at_most_ten_lines_then_and_k_more(n: int, lines: int, tail: str | None) -> None:
    d = build([row(i) for i in range(1, n + 1)])
    got = d.message.splitlines()
    assert d.title == f"{n} held notifications"
    assert len(got) == lines + (1 if tail else 0)
    assert (got[-1] == tail) if tail else all(x.startswith("warn s: ") for x in got)
    assert MAX_LINES == 10


def test_the_lines_kept_are_the_first_ten_in_order() -> None:
    rows = [row(i, level="info", title=f"t{i}") for i in range(1, 13)]
    rows.append(row(13, level="error", title="late-error"))
    got = build(rows).message.splitlines()
    assert got[0] == "error s: late-error - m"
    assert got[1] == "info s: t1 - m" and got[9] == "info s: t9 - m"
    assert got[10] == "and 3 more"


def test_a_digest_has_no_link_even_when_a_row_has_one() -> None:
    d = build([row(1, link="https://h.example/x"), row(2)])
    assert d.link is None


def test_the_head_is_the_title_then_the_first_line_of_the_message() -> None:
    d = build([row(1, title="Build", message="first\nsecond"), row(2)])
    assert d.message.splitlines()[0] == "warn s: Build - first"


def test_a_row_with_no_title_uses_the_rule_name_and_with_neither_just_the_message() -> None:
    d = build(
        [
            row(1, title=None, rule="deploys", message="done"),
            row(2, title="", rule=None, message="bare"),
        ]
    )
    assert d.message.splitlines() == ["warn s: deploys - done", "warn s: bare"]


def test_a_head_is_cut_to_120_code_points_and_the_cut_leaves_no_trailing_space() -> None:
    title = "t"
    body = "x" * 118  # "t - " is 4 characters
    d = build([row(1, title=title, message=body), row(2, title=title, message=body + "ZZ")])
    first, second = d.message.splitlines()
    prefix = "warn s: "
    assert len(first) == len(prefix) + HEAD_CHARS
    assert first == prefix + "t - " + "x" * 116
    assert second == first
    spaced = build([row(1, title="t", message="y" * 115 + " tail"), row(2)]).message.splitlines()[0]
    assert spaced == "warn s: t - " + "y" * 115 and not spaced.endswith(" ")  # cut fell on a space


def test_the_cut_never_splits_a_code_point() -> None:
    astral = "\U0001f600"  # one code point, four UTF-8 bytes, two UTF-16 units
    d = build([row(1, title="t", message=astral * 200), row(2)])
    head = d.message.splitlines()[0][len("warn s: ") :]
    assert len(head) == HEAD_CHARS and head == "t - " + astral * 116
    head.encode("utf-8")  # no lone surrogate


HOSTILE = "\x1b[31mred\x1b[0m\r\nnext\x00line‮bidi sep\ttab"


def test_stored_text_is_sanitized_again_and_only_its_first_line_is_used() -> None:
    d = build([row(1, title=HOSTILE, message=HOSTILE, source="s\x1b[2J"), row(2)])
    (first, _) = d.message.splitlines()
    for bad in ("\x1b", "\x00", "‮", " ", "\t", "\r"):
        assert bad not in d.message
    assert first.startswith("warn s")
    assert "next" not in d.message  # the second line of a message never appears


def test_placeholder_text_in_a_row_stays_literal() -> None:
    d = build(
        [row(1, title="{{source}}", message="{{labels.x}} {{message}}", source="{{hook}}"), row(2)]
    )
    assert d.message.splitlines()[0] == "warn {{hook}}: {{source}} - {{labels.x}} {{message}}"


def test_a_message_with_leading_blank_lines_uses_its_first_non_empty_line() -> None:
    d = build([row(1, title="Only", message="\n \t\nsecond\nthird"), row(2)])
    assert d.message.splitlines()[0] == "warn s: Only - second"


def test_a_message_with_no_text_at_all_leaves_only_the_title() -> None:
    d = build([row(1, title="Only", message="\n \n"), row(2)])
    assert d.message.splitlines()[0] == "warn s: Only"


def test_a_cut_that_leaves_the_separator_dangling_drops_it() -> None:
    d = build([row(1, title="a" * 117, message="mm"), row(2)])
    assert d.message.splitlines()[0] == "warn s: " + "a" * 117


def test_a_cut_that_ends_on_a_zero_width_joiner_drops_it() -> None:
    d = build([row(1, title="a" * 119 + "\u200d", message="mm"), row(2)])
    assert d.message.splitlines()[0] == "warn s: " + "a" * 119


def test_a_hostile_long_source_cannot_push_the_message_past_4000_code_points() -> None:
    d = build([row(i, source="s" * 5000) for i in range(1, 12)])
    assert len(d.message) <= 4000 and not d.message.endswith(" ")


def test_fewer_than_two_rows_or_an_unknown_level_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        build([row(1)])
    with pytest.raises(ValueError):
        build([row(1), row(2, level="loud")])
