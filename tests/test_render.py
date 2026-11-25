"""Rendering (#11): templates, the plain and text value rule, and sanitizing.

Pure logic: tests build ``Rule`` and ``Notification`` objects directly and read no files (the
examples are the pinned producer payloads under ``tests/sibling-schemas``)."""

from __future__ import annotations

import json
import time
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from notice_route.payload import Notification
from notice_route.render import Rendered, render, sanitize
from notice_route.validate import Match, Rule

EMPTY = Match(source=None, hook=None, repo=None, labels=(), where=(), level_min=None)
SIBLINGS = Path(__file__).resolve().parent / "sibling-schemas"


def rule(message: str | None = "body", **kw: Any) -> Rule:
    base: dict[str, Any] = {
        "name": "r",
        "match": EMPTY,
        "drop": False,
        "notify": ("chat",),
        "level": "info",
        "title": None,
        "message": message,
        "link": None,
        "dedup_window": 600.0,
        "include_text": (),
    }
    base.update(kw)
    return Rule(**base)


def hook(payload: dict[str, Any] | None = None, **kw: Any) -> Notification:
    base: dict[str, Any] = {
        "shape": "hook",
        "source": "agent-spawn",
        "hook": "post-fail",
        "payload": payload if payload is not None else {},
    }
    base.update(kw)
    return Notification(**base)


def notice(**kw: Any) -> Notification:
    base: dict[str, Any] = {"shape": "notice", "source": "direct", "title": "t", "message": "m"}
    base.update(kw)
    return Notification(**base)


def msg(template: str, payload: dict[str, Any], **kw: Any) -> Rendered:
    return render(rule(template, **kw), hook(payload), level="warn")


SAFE = {"reads_third_party": False, "tainted": False}

# --- sanitize ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "lines", "expected"),
    [
        ("", False, ""),
        ("plain", False, "plain"),
        ("a\r\nb", True, "a\nb"),
        ("a\rb", True, "a\nb"),
        ("a\r\n\r\nb", True, "a\n\nb"),
        ("a\nb", False, "a"),  # cut at the first newline
        ("a\r\nb", False, "a"),
        ("a\rb", False, "a"),
        ("\nb", False, ""),
        ("a\tb", True, "a b"),
        ("a\tb", False, "a b"),
        ("a\x00b", True, "a b"),  # NUL is Cc
        ("a\x1b[31mred", True, "a [31mred"),  # ESC is Cc: the sequence loses its lead
        ("a\x85b", True, "a b"),  # NEL is Cc
        ("a\x7fb", True, "a b"),
        ("a b", True, "a b"),  # Zl
        ("a b", True, "a b"),  # Zp
        ("a b　c d", True, "a b c d"),  # Zs
        ("a​b", True, "ab"),  # zero-width space, Cf
        ("a‌b", True, "ab"),
        ("a‍b", True, "a‍b"),  # ZWJ is kept
        ("a‎b‏c", True, "abc"),
        ("a‮b‪c‬d", True, "abcd"),  # bidi embedding and override, Cf
        ("a⁦b⁧c⁨d⁩e", True, "abcde"),  # bidi isolates, Cf
        ("a﻿b", True, "ab"),  # BOM, Cf
        ("a­b", True, "ab"),  # soft hyphen, Cf
        ("a\U000e0001b\U000e0041c", True, "abc"),  # tag characters, Cf
        ("a\ud800b", True, "ab"),  # lone surrogate, Cs
        ("a\udfffb", True, "ab"),
        ("ab\U000f0000c", True, "abc"),  # Co
        ("a͏b", True, "ab"),
        ("aᅟbᅠc", True, "abc"),
        ("a឴b឵c", True, "abc"),
        ("a᠋b᠌c᠍d", True, "abcd"),
        ("a⠀b", True, "ab"),  # braille blank
        ("aㅤbﾠc", True, "abc"),
        ("a￼b", True, "ab"),
        ("a︀b️c", True, "abc"),
        ("a\U000e0100b\U000e01efc", True, "abc"),
        ("a   b", True, "a b"),  # runs of spaces
        ("a \t   b", True, "a b"),  # runs made by replacement
        ("a ​ b", True, "a b"),  # a run made by a deletion
        ("  a  ", True, "a"),
        ("  a  \n  b  ", True, "a\nb"),  # each line is stripped
        ("a \n\n b", True, "a\n\nb"),  # blank lines stay
        ("​ ​", True, ""),
        ("é", True, "é"),  # a combining mark is kept
        ("é\U0001f600", True, "é\U0001f600"),  # unicode text and emoji are kept
        ("{{x}}", True, "{{x}}"),  # sanitize never expands
    ],
)
def test_sanitize_cases(text: str, lines: bool, expected: str) -> None:
    assert sanitize(text, lines) == expected


def test_sanitize_uses_the_unicodedata_categories_for_every_code_point() -> None:
    """Whatever survives is never in a deleted or replaced category; ``\\n`` only when lines."""
    keep_cf = {0x200D}
    for cp in range(0x110000):
        ch = chr(cp)
        if ch in "\r\n":
            continue
        out = sanitize("a" + ch + "b", True)
        cat = unicodedata.category(ch)
        if cat in ("Cs", "Co") or (cat == "Cf" and cp not in keep_cf):
            assert out == "ab", hex(cp)
        elif cat in ("Cc", "Zl", "Zp", "Zs"):
            assert out == "a b", hex(cp)
        elif out != "a" + ch + "b":
            assert cp in LISTED and out == "ab", hex(cp)  # only the listed code points differ


LISTED = (
    {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0, 0xFFFC}
    | set(range(0x180B, 0x180E))
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
)


def test_sanitize_is_idempotent() -> None:
    hostile = "\r\n a​\t  b\x00\ud800\n\n  c   d‍"
    for lines in (True, False):
        once = sanitize(hostile, lines)
        assert sanitize(once, lines) == once


def test_sanitize_output_has_no_control_or_newline_when_single_line() -> None:
    out = sanitize("".join(map(chr, range(0x2100))) * 3, False)
    assert "\n" not in out
    assert all(unicodedata.category(c) not in ("Cc", "Cs", "Co") for c in out)


# --- value rule -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("word", "word"),
        ("a.b_c/d#e-f", "a.b_c/d#e-f"),
        ("9", "9"),
        ("A" * 256, "A" * 256),
        (0, "0"),
        (-7, "-7"),
        (41, "41"),
        (2**63 - 1, "9223372036854775807"),
        (1.5, "1.5"),
        (1.0, "1.0"),
        (1e20, "1e+20"),
        (0.1, "0.1"),
        (True, "true"),
        (False, "false"),
    ],
)
def test_plain_values_render_in_title_and_message(value: Any, text: str) -> None:
    got = render(rule("m {{v}}", title="t {{v}}"), hook({"v": value}), level="info")
    assert got.title == f"t {text}"[:256]
    assert got.message == f"m {text}"
    assert got.missing == () and got.excluded == () and not got.text_excluded


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-lead",
        ".lead",
        "has space",
        "tab\tsep",
        "line\nfeed",
        "trail\n",  # a regex dollar would accept this
        "A" * 257,
        "café",
        "Ａbc",  # fullwidth letter
        "a:b",
        "a@b",
        "https://x.example/p?q=1",
        "​zero",
        "a\x00",
        "a\ud800",
        "{{title}}",
    ],
)
def test_non_plain_strings_are_text_and_render_empty_in_a_title(value: str) -> None:
    got = render(rule("m", title="t[{{v}}]"), hook({"v": value}), level="info")
    assert got.title == "t[]"
    assert got.excluded == ("v",) and got.text_excluded
    assert got.missing == ()


@pytest.mark.parametrize("value", ["has space", "a:b", "", "café", "x" * 300])
def test_text_renders_empty_in_a_message_without_include_text(value: str) -> None:
    got = msg("m[{{v}}]", {"v": value, **SAFE})
    assert got.message == "m[]"
    assert got.excluded == ("v",) and got.text_excluded


def test_text_renders_in_a_message_with_include_text_and_both_flags_false() -> None:
    got = msg("m {{v}}.", {"v": "hello there", **SAFE}, include_text=("v",))
    assert got.message == 'm "hello there".'
    assert got.excluded == () and not got.text_excluded


@pytest.mark.parametrize(
    "flags",
    [
        {},
        {"reads_third_party": False},
        {"tainted": False},
        {"reads_third_party": True, "tainted": False},
        {"reads_third_party": False, "tainted": True},
        {"reads_third_party": True, "tainted": True},
        {"reads_third_party": None, "tainted": False},
        {"reads_third_party": False, "tainted": None},
        {"reads_third_party": "false", "tainted": False},
        {"reads_third_party": False, "tainted": 0},
        {"reads_third_party": 0, "tainted": 0},
        {"reads_third_party": [], "tainted": ""},
    ],
)
def test_text_needs_both_flags_as_boolean_false(flags: dict[str, Any]) -> None:
    got = msg("m {{v}}", {"v": "two words", **flags}, include_text=("v",))
    assert got.message == "m"
    assert got.excluded == ("v",) and got.text_excluded


def test_text_never_renders_for_a_notice_rule_message() -> None:
    n = notice(payload={"v": "two words", **SAFE})  # payload is not read for a notice
    got = render(rule("m {{v}}", include_text=("v",)), n, level="info")
    assert got.message == "m" and got.missing == ("v",)


def test_include_text_names_the_exact_path() -> None:
    p = {"a": {"b": "two words"}, "c": "other words", **SAFE}
    got = msg("{{a.b}}|{{c}}", p, include_text=("a.b",))
    assert got.message == '"two words"|'
    assert got.excluded == ("c",)


def test_an_include_text_entry_no_message_placeholder_uses_has_no_effect() -> None:
    p = {"a": "two words", "b": "other words", **SAFE}
    with_entry = msg("{{b}}", p, include_text=("a",))
    without = msg("{{b}}", p)
    assert with_entry == without
    # and a title placeholder does not use it either
    t = render(rule("m", title="{{a}}", include_text=("a",)), hook(p), level="info")
    assert t.title == "" and t.excluded == ("a",)


def test_text_is_never_included_in_a_title_even_with_include_text() -> None:
    p = {"a": "two words", **SAFE}
    got = render(rule("{{a}}", title="[{{a}}]", include_text=("a",)), hook(p), level="info")
    assert got.title == "[]"
    assert got.message == '"two words"'
    assert got.excluded == ("a",)  # excluded from the title


def test_text_is_sanitized_cut_and_quoted() -> None:
    value = 'say "hi"‮\r\nsecond\tline'
    got = msg("{{v}}", {"v": value, **SAFE}, include_text=("v",))
    assert got.message == "\"say 'hi'\""  # cut at the first newline; quotes become single
    got = msg("{{v}}", {"v": 'a\tb "c"\x1b[0m', **SAFE}, include_text=("v",))
    assert got.message == "\"a b 'c' [0m\""


@pytest.mark.parametrize(
    ("length", "kept"),
    [(3, 3), (199, 199), (200, 200), (201, 200), (5000, 200)],
)
def test_text_cut_boundaries_count_characters(length: int, kept: int) -> None:
    got = msg("{{v}}", {"v": "x y" + "z" * (length - 3), **SAFE}, include_text=("v",))
    assert got.message == '"' + ("x y" + "z" * (length - 3))[:kept] + '"'


def test_text_cut_counts_code_points_not_bytes_and_never_splits_one() -> None:
    value = "\U0001f600 " + "é" * 400  # 4-byte and 2-byte code points
    got = msg("{{v}}", {"v": value, **SAFE}, include_text=("v",))
    inner = got.message[1:-1]
    assert len(inner) == 200 and inner == value[:200]
    got.message.encode("utf-8")  # strict encoding: no lone surrogate was produced


def test_text_that_sanitizes_to_nothing_renders_empty_and_is_excluded() -> None:
    got = msg("m[{{v}}]", {"v": "​​ \n x", **SAFE}, include_text=("v",))
    assert got.message == "m[]" and got.excluded == ("v",) and got.text_excluded


def test_a_value_with_a_placeholder_is_not_expanded_again() -> None:
    p = {"x": "SECOND", "y": "{{x}}", "z": "a {{x}} {{title}} b", **SAFE}
    got = msg("{{y}}|{{z}}", p, include_text=("z",))
    assert got.message == '|"a {{x}} {{title}} b"'
    got = render(rule("{{y}}", title="{{y}}"), hook(p), level="info")
    assert got.title == "" and got.message == "agent-spawn post-fail"  # fallback: '{{x}}' is text
    assert "SECOND" not in got.title + got.message


def test_a_plain_value_cannot_hold_a_placeholder() -> None:
    got = render(rule("{{a}}{{b}}"), hook({"a": "{", "b": "{x}}"}), level="info")
    assert got.message == "agent-spawn post-fail" and got.excluded == ("a", "b")


def test_derived_fields_are_never_expanded_again() -> None:
    n = hook({}, source="agent-spawn")
    got = render(rule("{{source}}/{{hook}}/{{level}}"), n, level="warn")
    assert got.message == "agent-spawn/post-fail/warn"


# --- templates --------------------------------------------------------------------------------


def test_literals_and_double_close_braces_pass_through() -> None:
    got = msg("a }} b {{v}} }} c", {"v": "x"})
    assert got.message == "a }} b x }} c"


def test_the_same_placeholder_twice_and_adjacent() -> None:
    got = msg("{{v}}{{v}}-{{w}}{{v}}", {"v": "a", "w": 2})
    assert got.message == "aa-2a"


def test_field_paths_walk_objects_up_to_four_parts() -> None:
    p = {"a": {"b": {"c": {"d": "deep"}}}, "x": {"y": [1]}, "n": None}
    got = msg("{{a.b.c.d}}|{{a.b}}|{{x.y}}|{{n}}|{{x.y.z}}|{{a.q}}|{{n.k}}", p)
    assert got.message == "deep||||||"
    assert got.missing == ("a.b", "x.y", "n", "x.y.z", "a.q", "n.k")


@pytest.mark.parametrize("value", [None, [], [1, 2], {}, {"k": "v"}])
def test_absent_null_list_and_object_are_missing(value: Any) -> None:
    got = msg("[{{v}}]", {"v": value})
    assert got.message == "[]" and got.missing == ("v",) and got.excluded == ()
    assert not got.text_excluded
    got = msg("[{{nope}}]", {})
    assert got.missing == ("nope",)


def test_missing_and_excluded_are_listed_once_in_first_seen_order() -> None:
    got = render(
        rule("{{b}}{{a}}{{b}}{{t}}{{t}}", title="{{a}}{{t}}"),
        hook({"t": "two words"}),
        level="info",
    )
    assert got.missing == ("a", "b") or got.missing == ("b", "a")
    assert got.missing == ("a", "b")  # title is rendered first
    assert got.excluded == ("t",)


def test_a_payload_field_of_a_derived_name_is_not_reachable() -> None:
    p = {"source": "evil", "hook": "evil", "level": "evil", "pr_url": "evil", "ticket_url": "evil"}
    got = render(
        rule("{{source}} {{hook}} {{level}}|{{pr_url}}|{{ticket_url}}"), hook(p), level="error"
    )
    assert got.message == "agent-spawn post-fail error||"
    assert "evil" not in got.message


def test_dotted_names_that_start_with_a_derived_name_read_the_payload() -> None:
    got = msg("{{source.k}}", {"source": {"k": "v"}})
    assert got.message == "v"


def test_derived_links_are_passed_in_and_sanitized() -> None:
    got = render(
        rule("{{pr_url}} {{ticket_url}}"),
        hook({}),
        level="info",
        pr_url="https://h.example/o/r/pull/1",
        ticket_url="https://t.example/browse/K-1‮",
    )
    assert got.message == "https://h.example/o/r/pull/1 https://t.example/browse/K-1"
    assert got.missing == ()


def test_an_empty_derived_link_on_a_hook_payload_is_missing() -> None:
    got = msg("{{pr_url}}{{ticket_url}}", {})
    assert got.message == "agent-spawn post-fail" and got.missing == ("pr_url", "ticket_url")


def test_notice_shape_reads_no_payload_and_reports_by_the_spec() -> None:
    n = notice(source="direct", labels=(("team", "ops"),))
    got = render(
        rule(
            "{{source}}|{{hook}}|{{pr_url}}|{{ticket_url}}|{{level}}|{{title}}|{{x.y}}|{{labels.team}}|{{labels.zz}}"
        ),
        n,
        level="action",
    )
    assert got.message == "direct||||action|||ops|"
    assert got.missing == ("title", "x.y", "labels.zz")
    assert got.excluded == ()


def test_a_rule_with_a_message_never_reads_a_notice_title_or_message() -> None:
    n = notice(title="T-TEXT", message="M-TEXT")
    got = render(rule("{{title}}{{message}}", title="{{title}}"), n, level="info")
    assert got.title == "" and got.message == "direct"  # the fallback; no notice text


# --- labels -----------------------------------------------------------------------------------


def test_labels_render_the_lexically_first_value_when_plain() -> None:
    n = hook({}, labels=(("team", "zeta"), ("team", "alpha"), ("team", "mid"), ("other", "o")))
    got = render(rule("{{labels.team}}/{{labels.other}}"), n, level="info")
    assert got.message == "alpha/o" and got.excluded == ()


@pytest.mark.parametrize("value", ["", "a:b", "x@y", "a=b", "-lead", "a,b", "a+b", "x" * 257])
def test_a_label_value_that_is_not_plain_renders_empty_and_is_excluded(value: str) -> None:
    n = hook({}, labels=(("k", value),))
    got = render(
        rule("[{{labels.k}}]", title="[{{labels.k}}]", include_text=("labels.k",)), n, level="info"
    )
    assert got.message == "[]" and got.title == "[]"
    assert got.excluded == ("labels.k",) and got.text_excluded and got.missing == ()


def test_the_first_label_value_decides_even_when_a_later_one_is_plain() -> None:
    n = hook({}, labels=(("k", "b"), ("k", "a:b")))  # "a:b" sorts before "b"
    got = render(rule("[{{labels.k}}]"), n, level="info")
    assert got.message == "[]" and got.excluded == ("labels.k",)


def test_an_absent_label_is_missing() -> None:
    got = render(rule("[{{labels.nope}}]"), hook({}), level="info")
    assert got.message == "[]" and got.missing == ("labels.nope",)


def test_the_labels_field_path_is_the_payload_list_not_the_labels() -> None:
    n = hook({"labels": ["a=b"]}, labels=(("a", "b"),))
    got = render(rule("{{labels}}"), n, level="info")
    assert got.message == "agent-spawn post-fail" and got.missing == ("labels",)


# --- the title, the message, the cuts and the fallback ----------------------------------------


def test_no_title_template_renders_an_empty_title() -> None:
    assert render(rule("m"), hook({}), level="info").title == ""


def test_the_title_is_single_line_and_the_message_keeps_newlines() -> None:
    got = render(rule("a\r\nb\n\n c  d ", title="t1\nt2"), hook({}), level="info")
    assert got.title == "t1"
    assert got.message == "a\nb\n\nc d"


def test_template_literals_are_sanitized_too() -> None:
    got = render(rule("a‮b\x1b[1m\t\ud800", title="x​y"), hook({}), level="info")
    assert got.message == "ab [1m" and got.title == "xy"


@pytest.mark.parametrize(("length", "kept"), [(255, 255), (256, 256), (257, 256), (2000, 256)])
def test_the_title_is_cut_to_256_characters(length: int, kept: int) -> None:
    got = render(rule("m", title="é" * length), hook({}), level="info")
    assert got.title == "é" * kept


@pytest.mark.parametrize(
    ("length", "kept"), [(3999, 3999), (4000, 4000), (4001, 4000), (6000, 4000)]
)
def test_the_message_is_cut_to_4000_characters(length: int, kept: int) -> None:
    got = render(rule(None), notice(message="\U0001f600" * length), level="info")
    assert got.message == "\U0001f600" * kept
    got = msg("é" * 2000 + "{{v}}" + "é" * 2000, {"v": "ab"})
    assert len(got.message) == 4000


def test_the_message_cut_is_after_substitution() -> None:
    got = msg("{{a}}{{b}}", {"a": "x" * 256, "b": "y" * 256})
    assert got.message == "x" * 256 + "y" * 256
    assert len(msg("{{v}}" * 400, {"v": "q" * 256}).message) == 4000


def test_the_empty_message_falls_back_to_source_and_hook() -> None:
    assert render(rule("{{v}}"), hook({}), level="info").message == "agent-spawn post-fail"
    assert render(rule("​ \n"), hook({}), level="info").message == "agent-spawn post-fail"
    got = render(rule("{{v}}"), notice(source="direct"), level="info")
    assert got.message == "direct"
    assert got.missing == ("v",)


def test_the_fallback_does_not_hide_a_non_empty_message() -> None:
    assert render(rule("{{v}}"), hook({"v": "ok"}), level="info").message == "ok"


# --- pass-through -----------------------------------------------------------------------------


def test_a_pass_through_rule_sends_the_sanitized_and_cut_notice_text() -> None:
    n = notice(title="a‮b\nsecond", message="x\r\ny​\tz\x00\n\n  q  ")
    got = render(rule(None), n, level="info")
    assert got.title == "ab"
    assert got.message == "x\ny z\n\nq"
    assert got.missing == () and got.excluded == () and not got.text_excluded


def test_a_pass_through_rule_cuts_to_the_title_and_message_limits() -> None:
    n = notice(title="t" * 300, message="m" * 5000)
    got = render(rule(None), n, level="info")
    assert (len(got.title), len(got.message)) == (256, 4000)


def test_a_pass_through_notice_text_is_not_a_template() -> None:
    n = notice(title="{{source}}", message="{{source}} {{labels.x}}")
    got = render(rule(None), n, level="info")
    assert got.title == "{{source}}" and got.message == "{{source}} {{labels.x}}"


def test_a_pass_through_notice_without_a_title_has_an_empty_title() -> None:
    got = render(rule(None), notice(title=None, message="m"), level="info")
    assert got.title == "" and got.message == "m"


def test_a_pass_through_message_that_sanitizes_empty_falls_back() -> None:
    got = render(rule(None), notice(message="​‎"), level="info")
    assert got.message == "direct"


# --- hostile input: never raises, deterministic ------------------------------------------------

HOSTILE = [
    "\x00" * 10,
    "\x1b]0;title\x07\x1b[2J",
    "‮⁦" * 50,
    "𐀀\udfff",
    "\U0010ffff￾￿",
    "a\r\n" * 5000,
    "{{" * 3000,
    "}}{{" * 1000,
    " " * 100000,
    "‍" * 100000,
]


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_values_never_raise_and_are_deterministic(value: str) -> None:
    p = {"v": value, "w": [value], "x": {"y": value}, **SAFE}
    r = rule("{{v}}|{{w}}|{{x.y}}|{{labels.k}}", title="{{v}}", include_text=("v", "x.y", "w"))
    n = hook(p, labels=(("k", value),))
    first = render(r, n, level="error")
    assert first == render(r, n, level="error")
    assert len(first.title) <= 256 and len(first.message) <= 4000
    for out in (first.title, first.message):
        out.encode("utf-8")
        assert all(unicodedata.category(c) not in ("Cs", "Co") for c in out)
        assert "\x1b" not in out and "\x00" not in out
    assert "\n" not in first.title


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        -float("inf"),
        10**5000,
        -(10**5000),
        object(),
        b"bytes",
        (1,),
        {1},
    ],
    ids=lambda v: type(v).__name__,
)
def test_values_the_reader_cannot_produce_do_not_raise(value: Any) -> None:
    got = msg("[{{v}}]", {"v": value}, include_text=("v",))
    assert got.message == "[]"


def test_non_string_keys_and_deep_payloads_do_not_raise() -> None:
    deep: dict[Any, Any] = {}
    cur = deep
    for _ in range(2000):
        cur["a"] = {}
        cur = cur["a"]
    got = msg("{{a.a.a.a}}{{1}}", {"a": deep, 1: "x"})
    assert got.message == "{{1}}"  # not a placeholder: it is literal text


def test_an_unvalidated_template_never_raises() -> None:
    for bad in ["{{ x }}", "{{", "{{x", "x}}{{", "{{{x}}}", "{{a-b}}", "{{labels.}}", "\ud800"]:
        got = render(rule(bad), hook({"x": "v"}), level="info")
        assert isinstance(got.message, str)


def test_huge_values_are_linear() -> None:
    big = "ab ​" * 250_000  # 1 MiB of characters
    p = {"v": big, **SAFE}
    r = rule("{{v}}" * 400, title="{{v}}{{v}}", include_text=("v",))
    start = time.perf_counter()
    got = render(r, hook(p), level="info")
    first = time.perf_counter() - start
    assert len(got.message) == 4000
    assert first < 5.0
    # doubling the value must not square the time
    p2 = {"v": big * 2, **SAFE}
    start = time.perf_counter()
    render(r, hook(p2), level="info")
    assert time.perf_counter() - start < max(first * 4, 1.0)


def test_sanitize_is_linear_on_adversarial_runs() -> None:
    for text in [" ​" * 500_000, "\n \n" * 300_000, "\r" * 1_000_000, "\ud800" * 1_000_000]:
        start = time.perf_counter()
        sanitize(text, True)
        sanitize(text, False)
        assert time.perf_counter() - start < 5.0


def test_a_large_payload_with_many_placeholders_stays_fast() -> None:
    p = {f"k{i}": "v" for i in range(5000)}
    start = time.perf_counter()
    render(rule("{{k1}}" * 300), hook(p), level="info")
    assert time.perf_counter() - start < 2.0


# --- realistic producer payloads --------------------------------------------------------------


def load_example(name: str) -> dict[str, Any]:
    path = SIBLINGS / "agent-spawn" / "0.38.0" / "examples" / name
    return json.loads(path.read_text("utf-8"))


@pytest.mark.parametrize(
    "name", ["post-escalate.json", "post-state-change-agent.json", "post-state-change-session.json"]
)
def test_the_pinned_producer_examples_render_without_surprises(name: str) -> None:
    payload = load_example(name)
    n = hook(payload, source=payload["tool"], hook=payload["hook"], object_id=payload["object_id"])
    r = rule(
        "{{object_type}} {{object_id}} {{kind}} is {{status}} ({{needs_attention}}), run {{run_kind}},"
        " labels {{labels.ticket}} {{labels.stage}}",
        title="{{source}} {{hook}} {{object_id}}",
    )
    n = replace(n, labels=(("ticket", "T-1"), ("stage", "build")))
    got = render(r, n, level="warn")
    assert got.missing == () and got.excluded == () and not got.text_excluded
    assert got.title == f"agent-spawn {payload['hook']} ag-abcdefgh"
    assert got.message.startswith("agent ag-abcdefgh implement is parked (blocked)")
    assert got.message.endswith("labels T-1 build")


def test_the_pinned_example_text_fields_are_excluded_unless_opted_in() -> None:
    payload = load_example("post-escalate.json")
    n = hook(payload, source="agent-spawn", hook="post-escalate")
    got = render(rule("{{outcome.report.summary}}|{{ts}}"), n, level="info")
    assert got.message == "|" and got.excluded == ("outcome.report.summary", "ts")
    got = render(
        rule("{{outcome.report.summary}}", include_text=("outcome.report.summary",)),
        n,
        level="info",
    )
    assert got.message == '"Needs a decision"' and got.excluded == ()


def test_the_config_example_failure_alert_renders_without_surprises() -> None:
    payload = {
        "tool": "notice-route",
        "hook": "post-fail",
        "object_type": "destination",
        "object_id": "team-chat",
        "event_id": 12,
        "labels": [],
        "code": "retries_exhausted",
        "outcome": "failed",
        "attempts": 3,
        "notification_source": "agent-spawn",
        "notification_level": "error",
        "kind": "slack-notice",
    }
    n = hook(payload, source="notice-route", hook="post-fail")
    r = rule(
        "Destination {{object_id}} ({{kind}}): {{code}}, {{outcome}} after {{attempts}} attempts,"
        " for a {{notification_level}} from {{notification_source}}.",
        title="Notification channel failed",
    )
    got = render(r, n, level="warn")
    assert got.message == (
        "Destination team-chat (slack-notice): retries_exhausted, failed after 3 attempts,"
        " for a error from agent-spawn."
    )
    assert got.title == "Notification channel failed"
    assert got.missing == () and got.excluded == ()
