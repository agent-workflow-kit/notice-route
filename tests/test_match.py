"""Rule matching (#10): conditions, the first-match decision, levels and pass-through rules.

Pure logic: tests build ``Notification`` and ``Match`` objects directly and read no files."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from notice_route.match import (
    Decision,
    decide,
    final_level,
    matches,
    reads_third_party,
    rule_covers,
    tainted,
    wildcard_matches,
)
from notice_route.payload import Notification
from notice_route.validate import Match, Rule


def m(**kw: Any) -> Match:
    base: dict[str, Any] = {
        "source": None,
        "hook": None,
        "repo": None,
        "labels": (),
        "where": (),
        "level_min": None,
    }
    base.update(kw)
    return Match(**base)


def rule(name: str = "r", match: Match | None = None, **kw: Any) -> Rule:
    base: dict[str, Any] = {
        "name": name,
        "match": match or m(),
        "drop": False,
        "notify": ("chat",),
        "level": "info",
        "title": None,
        "message": "body",
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
        "repo": "org/repo",
        "payload": payload if payload is not None else {},
    }
    base.update(kw)
    return Notification(**base)


def notice(**kw: Any) -> Notification:
    base: dict[str, Any] = {"shape": "notice", "source": "direct", "title": "t", "message": "x"}
    base.update(kw)
    return Notification(**base)


# --- exact-or-trailing-* ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "value", "expected"),
    [
        ("abc", "abc", True),
        ("abc", "abcd", False),
        ("abc", "ab", False),
        ("abc", "ABC", False),  # case-sensitive
        ("abc*", "abc", True),  # the star may match nothing
        ("abc*", "abcdef", True),
        ("abc*", "ab", False),
        ("abc*", "xabc", False),  # prefix, not substring
        ("*", "anything", True),
        ("*", "", True),  # a lone star matches any present value
        ("é*", "éa", True),
        ("é*", "ea", False),
        ("a*", "a" * 100_000, True),
        ("a" * 256 + "*", "a" * 255, False),
        ("a.c", "abc", False),  # no regex: a dot is a dot
        ("a+", "aa", False),
        ("(a|b)", "a", False),
        ("[ab]", "a", False),
    ],
)
def test_wildcard_matches(pattern: str, value: str, expected: bool) -> None:
    assert wildcard_matches(pattern, value) is expected


def test_wildcard_gives_correct_results_on_long_hostile_input() -> None:
    # The grammar is one literal prefix: the answer depends only on it, however long the value.
    long_value = "a" * 1_000_000
    assert not wildcard_matches("a" * 255 + "b*", long_value)
    assert wildcard_matches("a" * 255 + "*", long_value)
    assert wildcard_matches("a" * 255 + "b*", "a" * 255 + "b" + "a" * 1_000_000)
    assert not wildcard_matches("a" * 255 + "b*", "a" * 254)


# --- conditions per shape ----------------------------------------------------------------------


def test_empty_match_matches_both_shapes() -> None:
    assert matches(m(), hook())
    assert matches(m(), notice())


@pytest.mark.parametrize(
    ("match", "n", "expected"),
    [
        (m(source="agent-spawn"), hook(), True),
        (m(source="agent-*"), hook(), True),
        (m(source="other"), hook(), False),
        (m(source="direct"), notice(), True),
        (m(source="dir*"), notice(), True),
        (m(source="agent-spawn"), notice(), False),
    ],
)
def test_source_applies_to_both_shapes(match: Match, n: Notification, expected: bool) -> None:
    assert matches(match, n) is expected


@pytest.mark.parametrize(
    ("match", "n", "expected"),
    [
        (m(hook="post-fail"), hook(), True),
        (m(hook="post-*"), hook(), True),
        (m(hook="*"), hook(), True),
        (m(hook="post"), hook(), False),
        (m(hook="post-fail"), notice(), False),  # hook payloads only
        (m(hook="*"), notice(), False),  # even a lone star never matches a notice
    ],
)
def test_hook_condition(match: Match, n: Notification, expected: bool) -> None:
    assert matches(match, n) is expected


def test_hook_condition_without_a_hook_name_never_matches() -> None:
    assert not matches(m(hook="*"), hook(hook=None))


@pytest.mark.parametrize(
    ("match", "n", "expected"),
    [
        (m(repo="org/repo"), hook(), True),
        (m(repo="org/*"), hook(), True),
        (m(repo="*"), hook(), True),
        (m(repo="org/rep"), hook(), False),
        (m(repo="*"), hook(repo=None), False),  # null or absent repo never matches, not even *
        (m(repo="org/repo"), hook(repo=None), False),
        (m(repo="org/repo"), notice(), False),  # hook payloads only
        (m(repo="*"), notice(repo="org/repo"), False),
    ],
)
def test_repo_condition(match: Match, n: Notification, expected: bool) -> None:
    assert matches(match, n) is expected


@pytest.mark.parametrize(
    ("match", "n", "expected"),
    [
        (m(level_min="info"), notice(), True),  # no level counts as info
        (m(level_min="warn"), notice(), False),
        (m(level_min="warn"), notice(level="warn"), True),
        (m(level_min="warn"), notice(level="error"), True),
        (m(level_min="error"), notice(level="action"), False),
        (m(level_min="action"), notice(level="action"), True),
        (m(level_min="info"), hook(), False),  # channel notices only
        (m(level_min="info"), hook(payload={"level": "error"}), False),
        (m(level_min="warn"), notice(level="bogus"), False),  # fail closed on an unknown level
    ],
)
def test_level_min(match: Match, n: Notification, expected: bool) -> None:
    assert matches(match, n) is expected


@pytest.mark.parametrize(
    ("match", "n", "expected"),
    [
        (m(labels=("env=prod",)), notice(labels=(("env", "prod"),)), True),
        (m(labels=("env=prod",)), hook(labels=(("env", "prod"),)), True),
        (m(labels=("env=prod",)), notice(), False),
        (m(labels=("env=prod",)), notice(labels=(("env", "dev"),)), False),
        (m(labels=("env=prod",)), notice(labels=(("env", "prod2"),)), False),  # no prefix
        (m(labels=("env=*",)), notice(labels=(("env", "prod"),)), False),  # no wildcard
        (
            m(labels=("env=prod", "tier=web")),
            notice(labels=(("tier", "web"), ("env", "prod"))),
            True,
        ),
        (m(labels=("env=prod", "tier=web")), notice(labels=(("env", "prod"),)), False),
        (m(labels=("env=",)), notice(labels=(("env", ""),)), True),  # an empty value
        (m(labels=("k=a=b",)), notice(labels=(("k", "a=b"),)), True),  # value holds an equals
        (m(labels=("Env=prod",)), notice(labels=(("env", "prod"),)), False),  # case-sensitive
        # a list of labels never matches a differently split pair
        (m(labels=("a=b=c",)), notice(labels=(("a=b", "c"),)), False),
    ],
)
def test_labels(match: Match, n: Notification, expected: bool) -> None:
    assert matches(match, n) is expected


# --- where: JSON type equality -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("want", "have", "expected"),
    [
        ("open", "open", True),
        ("open", "Open", False),
        ("open", "open ", False),
        ("1", 1, False),  # a string never equals a number
        (1, "1", False),
        (1, 1, True),
        (1, 2, False),
        (1, True, False),  # a boolean never equals an integer
        (True, 1, False),
        (0, False, False),
        (False, 0, False),
        (True, True, True),
        (False, False, True),
        (True, False, False),
        (1, 1.0, False),  # 1.0 never equals 1
        (1, 1.5, False),
        (0, 0.0, False),
        (0, -0.0, False),
        (2**62, 2**62, True),
        (2**62, float(2**62), False),
        ("true", True, False),
        (True, "true", False),
        ("", "", True),
        ("é", "é", True),
        ("é", "é", False),  # no normalisation
        ("x", None, False),
        ("x", ["x"], False),
        ("x", {"x": "x"}, False),
        (1, [1], False),
        (True, None, False),
    ],
)
def test_where_type_equality(want: str | int | bool, have: Any, expected: bool) -> None:
    n = hook({"f": have})
    assert matches(m(where=(("f", want),)), n) is expected


def test_where_absent_field_does_not_match() -> None:
    assert not matches(m(where=(("f", "x"),)), hook({}))
    assert not matches(m(where=(("f", 0),)), hook({"g": 0}))
    assert not matches(m(where=(("f", False),)), hook({}))


def test_where_null_field_does_not_match_anything() -> None:
    for want in ("", "null", 0, False):
        assert not matches(m(where=(("f", want),)), hook({"f": None}))


def test_where_paths_descend_through_objects_only() -> None:
    payload = {"a": {"b": {"c": {"d": "deep"}}}, "list": [{"x": 1}], "s": "text"}
    assert matches(m(where=(("a.b.c.d", "deep"),)), hook(payload))
    assert not matches(m(where=(("a.b.c.d", "other"),)), hook(payload))
    assert not matches(m(where=(("a.b.c.e", "deep"),)), hook(payload))
    assert not matches(m(where=(("list.0.x", 1),)), hook(payload))  # no list indexing
    assert not matches(m(where=(("s.x", "t"),)), hook(payload))  # a string has no fields
    assert not matches(m(where=(("a.b.c.d.e", "deep"),)), hook(payload))


def test_where_all_pairs_must_hold() -> None:
    payload = {"a": "x", "b": 2}
    assert matches(m(where=(("a", "x"), ("b", 2))), hook(payload))
    assert not matches(m(where=(("a", "x"), ("b", 3))), hook(payload))
    assert not matches(m(where=(("a", "x"), ("c", 2))), hook(payload))


def test_where_is_not_a_wildcard_match() -> None:
    assert not matches(m(where=(("f", "a*"),)), hook({"f": "abc"}))
    assert matches(m(where=(("f", "a*"),)), hook({"f": "a*"}))


def test_where_reads_the_payload_not_the_derived_fields() -> None:
    # A key named like a Notification attribute still reads the payload mapping.
    n = hook({"repo": "payload-repo"}, repo="org/repo")
    assert matches(m(where=(("repo", "payload-repo"),)), n)
    assert not matches(m(where=(("repo", "org/repo"),)), n)


def test_where_does_not_apply_to_a_notice() -> None:
    assert not matches(m(where=(("f", "x"),)), notice(payload={"f": "x"}))


def test_where_dunder_and_odd_paths_are_plain_keys() -> None:
    payload = {"__class__": "x", "a": {"__dict__": 1}}
    assert matches(m(where=(("__class__", "x"),)), hook(payload))
    assert not matches(m(where=(("__class__.__name__", "dict"),)), hook({"__class__": {}}))
    assert not matches(m(where=(("a.__len__", 1),)), hook(payload))


def test_where_huge_values_and_unicode() -> None:
    big = "z" * 1_000_000
    assert not matches(m(where=(("f", "z" * 1024),)), hook({"f": big}))
    assert matches(m(where=(("f", big[:1024]),)), hook({"f": big[:1024]}))
    assert matches(m(where=(("f", "日本語‮"),)), hook({"f": "日本語‮"}))
    assert not matches(m(where=(("f", "x"),)), hook({"f": "x\x00"}))


def test_conditions_combine_with_and() -> None:
    n = hook({"k": "v"}, labels=(("a", "b"),))
    ok = m(source="agent-*", hook="post-fail", repo="org/*", labels=("a=b",), where=(("k", "v"),))
    assert matches(ok, n)
    for broken in (
        replace(ok, source="zzz"),
        replace(ok, hook="zzz"),
        replace(ok, repo="zzz"),
        replace(ok, labels=("a=c",)),
        replace(ok, where=(("k", "w"),)),
    ):
        assert not matches(broken, n)


# --- the first-match decision ------------------------------------------------------------------


def test_first_matching_rule_decides_and_later_rules_are_not_consulted() -> None:
    rules = (
        rule("first", m(source="agent-*"), notify=("a",), level="warn"),
        rule("second", m(source="agent-spawn"), notify=("b",), level="error"),
    )
    d = decide(rules, hook())
    assert d.rule is rules[0]
    assert d.index == 0
    assert d.outcome == "route"
    assert d.reason is None
    assert d.level == "warn"


def test_a_later_rule_decides_when_earlier_ones_do_not_match() -> None:
    rules = (rule("a", m(source="zzz")), rule("b", m(source="agent-*")), rule("c", m()))
    d = decide(rules, hook())
    assert (d.rule, d.index) == (rules[1], 1)


def test_catch_all_last_rule() -> None:
    rules = (rule("a", m(source="zzz")), rule("all", m()))
    assert decide(rules, notice()).rule is rules[1]


def test_no_match_is_dropped_no_rule() -> None:
    d = decide((rule("a", m(source="zzz")),), hook())
    assert d == Decision(rule=None, index=None, outcome="dropped", reason="no_rule", level=None)


def test_no_rules_is_dropped_no_rule() -> None:
    assert decide((), notice()).reason == "no_rule"


def test_drop_rule_ends_as_dropped_rule_drop() -> None:
    drop = rule("quiet", m(source="agent-*"), drop=True, notify=(), level=None, message=None)
    later = rule("later", m())
    d = decide((drop, later), hook())
    assert d == Decision(rule=drop, index=0, outcome="dropped", reason="rule_drop", level=None)


def test_drop_rule_that_does_not_match_lets_later_rules_decide() -> None:
    drop = rule("quiet", m(source="zzz"), drop=True, notify=(), level=None, message=None)
    later = rule("later", m())
    assert decide((drop, later), hook()).rule is later


def test_a_drop_rule_after_a_matching_notify_rule_is_not_consulted() -> None:
    notify = rule("n", m())
    drop = rule("d", m(), drop=True, notify=(), level=None, message=None)
    assert decide((notify, drop), notice()).outcome == "route"


def test_decide_is_repeatable() -> None:
    rules = (rule("a", m(source="agent-*")),)
    n = hook()
    assert decide(rules, n) == decide(rules, n)


def test_decide_scales_to_the_rule_limit() -> None:
    rules = tuple(rule(f"r{i}", m(source=f"s{i}")) for i in range(256))
    assert decide(rules, notice(source="s255")).index == 255
    assert decide(rules, notice(source="nope")).reason == "no_rule"


# --- final level -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule_level", "notice_level", "expected"),
    [
        ("warn", None, "warn"),
        ("warn", "error", "warn"),  # the rule's level wins, even lower than the notice's
        ("error", "info", "error"),
        (None, "action", "action"),  # else the notice's own level
        (None, None, "info"),  # else info
        (None, "info", "info"),
    ],
)
def test_final_level(rule_level: Any, notice_level: Any, expected: str) -> None:
    r = rule("p", m(level_min="info"), level=rule_level, message=None)
    assert final_level(r, notice(level=notice_level)) == expected
    assert decide((r,), notice(level=notice_level)).level == expected


def test_a_hook_payload_cannot_choose_its_level() -> None:
    r = rule("h", m(hook="post-fail"), level="info")
    assert decide((r,), hook({"level": "error"}, level="error")).level == "info"


def test_a_rule_without_level_on_a_hook_payload_falls_back_to_info() -> None:
    # validate refuses this config; the matcher still never invents a higher level.
    r = rule("h", m(hook="post-fail"), level=None)
    assert final_level(r, hook({"level": "error"})) == "info"


def test_unknown_notice_level_never_becomes_a_level() -> None:
    r = rule("p", m(), level=None, message=None)
    assert final_level(r, notice(level="bogus")) == "info"


# --- pass-through ------------------------------------------------------------------------------


def test_pass_through_decision_is_marked() -> None:
    r = rule("p", m(level_min="info"), level=None, message=None)
    d = decide((r,), notice(level="warn"))
    assert d.pass_through is True
    assert d.level == "warn"


def test_pass_through_rule_never_matches_a_hook_payload() -> None:
    r = rule("p", m(level_min="info"), level=None, message=None)
    assert decide((r,), hook()).reason == "no_rule"


def test_templated_rule_is_not_pass_through() -> None:
    assert decide((rule("t", m()),), notice()).pass_through is False


def test_drop_rule_is_not_pass_through() -> None:
    r = rule("d", m(), drop=True, notify=(), level=None, message=None)
    assert decide((r,), notice()).pass_through is False


# --- provenance flags --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"reads_third_party": True}, True),
        ({"reads_third_party": False}, False),
        ({}, True),  # absent fails closed
        ({"reads_third_party": 0}, True),  # not a boolean fails closed
        ({"reads_third_party": "false"}, True),
        ({"reads_third_party": None}, True),
    ],
)
def test_reads_third_party(payload: dict[str, Any], expected: bool) -> None:
    assert reads_third_party(hook(payload)) is expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"tainted": True}, True),
        ({"tainted": False}, False),
        ({}, True),
        ({"tainted": 0}, True),
        ({"tainted": "false"}, True),
    ],
)
def test_tainted(payload: dict[str, Any], expected: bool) -> None:
    assert tainted(hook(payload)) is expected


def test_notice_flags_are_read_from_the_payload_too() -> None:
    # A notice carries no payload mapping; its text is the sender's own and is never trusted
    # through these flags.
    assert reads_third_party(notice()) is True
    assert tainted(notice()) is True


# --- unreachable rules (used by validate) ------------------------------------------------------


@pytest.mark.parametrize(
    ("earlier", "later", "expected"),
    [
        (m(), m(), True),
        (m(), m(source="a", labels=("x=y",)), True),  # an empty match covers everything
        (m(source="a"), m(), False),
        (m(source="a"), m(source="a"), True),
        (m(source="a"), m(source="b"), False),
        (m(source="a*"), m(source="ab"), True),
        (m(source="a*"), m(source="ab*"), True),
        (m(source="ab*"), m(source="a*"), False),
        (m(source="a"), m(source="a*"), False),
        (m(source="*"), m(source="zzz"), True),
        (m(source="*"), m(), False),  # an absent condition is wider than a star
        (m(hook="h*"), m(hook="hk"), True),
        (m(repo="o/*"), m(repo="o/r"), True),
        (m(source="a"), m(source="a", hook="h"), True),
        (m(labels=("a=b",)), m(labels=("a=b", "c=d")), True),
        (m(labels=("a=b", "c=d")), m(labels=("a=b",)), False),
        (m(labels=("a=b",)), m(), False),
        (m(where=(("k", 1),)), m(where=(("k", 1), ("j", "x"))), True),
        (m(where=(("k", 1),)), m(where=(("k", True),)), False),  # bool is not int
        (m(where=(("k", True),)), m(where=(("k", 1),)), False),
        (m(where=(("k", "1"),)), m(where=(("k", 1),)), False),
        (m(where=(("k", 1), ("j", 2))), m(where=(("j", 2), ("k", 1))), True),  # order is free
        (m(level_min="warn"), m(level_min="warn"), True),
        (m(level_min="warn"), m(level_min="error"), False),  # same value only
        (m(level_min="info"), m(), False),
        (m(level_min="info"), m(level_min="info", source="a"), True),
    ],
)
def test_rule_covers(earlier: Match, later: Match, expected: bool) -> None:
    assert rule_covers(earlier, later) is expected


def test_rule_covers_implies_the_later_rule_never_decides() -> None:
    # Cross-check the static rule against the matcher on a sample of notifications.
    earlier = m(source="agent-*", labels=("a=b",), where=(("k", 1),))
    later = m(source="agent-spawn", labels=("a=b", "c=d"), where=(("k", 1), ("j", "x")))
    assert rule_covers(earlier, later)
    samples = (
        hook({"k": 1, "j": "x"}, labels=(("a", "b"), ("c", "d"))),
        hook({"k": 1, "j": "x"}, labels=(("a", "b"),)),
        hook({"k": 2, "j": "x"}, labels=(("a", "b"), ("c", "d"))),
    )
    exercised = 0
    for n in samples:
        if matches(later, n):
            exercised += 1
            assert matches(earlier, n)
    assert exercised >= 1
