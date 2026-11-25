"""Links (#12): ``pr_url``, ``ticket_url`` and a rule's link from the router's own tables.

Pure logic. The payload fields are untrusted, so the tables of adversarial values below must
all render empty, and a built URL must hold only characters of the table and of the fixed
grammars."""

from __future__ import annotations

import re
import time
from dataclasses import replace
from typing import Any

import pytest

from notice_route.links import BuiltLinks, build_links, build_pr_url, build_ticket_url
from notice_route.payload import Notification
from notice_route.render import render
from notice_route.validate import Links, Match, Rule, is_literal_url

EMPTY = Match(source=None, hook=None, repo=None, labels=(), where=(), level_min=None)

LINKS = Links(
    hosts={
        "github.example.com": "github",
        "gitlab.example.com:8443": "gitlab",
        "gl.example.com": "gitlab",
        "gh.example.com:443": "github",
    },
    tickets={
        "example-site": "https://tickets.example.com/browse/",
        "other": "https://other.example.com:8080/t/",
    },
)


def rule(link: str | None = None, message: str | None = "body") -> Rule:
    return Rule(
        name="r",
        match=EMPTY,
        drop=False,
        notify=("chat",),
        level="info",
        title=None,
        message=message,
        link=link,
        dedup_window=600.0,
        include_text=(),
    )


def hook(payload: dict[str, Any], **kw: Any) -> Notification:
    return Notification(shape="hook", source="tool", hook="post-open", payload=payload, **kw)


def notice(**kw: Any) -> Notification:
    return Notification(shape="notice", source="direct", message="m", **kw)


# --- pr_url -----------------------------------------------------------------------------------

PR_GOOD = [
    ("github.example.com/o/r#1", "https://github.example.com/o/r/pull/1"),
    ("github.example.com/o/r#1234567890", "https://github.example.com/o/r/pull/1234567890"),
    ("github.example.com/a.b/c_d-e#7", "https://github.example.com/a.b/c_d-e/pull/7"),
    ("github.example.com/a/b/c/d#7", "https://github.example.com/a/b/c/d/pull/7"),
    ("github.example.com/...#7", "https://github.example.com/.../pull/7"),
    ("github.example.com/-/-#7", "https://github.example.com/-/-/pull/7"),
    ("gh.example.com:443/o/r#2", "https://gh.example.com:443/o/r/pull/2"),
    ("gl.example.com/g/s/p#12", "https://gl.example.com/g/s/p/-/merge_requests/12"),
    (
        "gitlab.example.com:8443/g/p#3",
        "https://gitlab.example.com:8443/g/p/-/merge_requests/3",
    ),
]


@pytest.mark.parametrize(("pr", "want"), PR_GOOD)
def test_pr_url_builds_the_documented_url(pr: str, want: str) -> None:
    assert build_pr_url(pr, LINKS) == want


PR_BAD = [
    # not in the tables (host, port, case)
    "other.example.com/o/r#1",
    "github.example.com:443/o/r#1",
    "gh.example.com/o/r#1",
    "gitlab.example.com/g/p#1",
    "gitlab.example.com:8444/g/p#1",
    "GITHUB.example.com/o/r#1",
    "github.example.com./o/r#1",
    "example.com/o/r#1",
    "x.github.example.com/o/r#1",
    # grammar: shape
    "",
    "#1",
    "github.example.com",
    "github.example.com#1",
    "github.example.com/#1",
    "github.example.com/o/r",
    "github.example.com/o/r#",
    "github.example.com/o/r/#1",
    "github.example.com//r#1",
    "github.example.com/o//r#1",
    "/o/r#1",
    "github.example.com/o/r#1#2",
    "github.example.com/o#x/r#1",
    "#github.example.com/o/r#1",
    # dot segments and encodings of them
    "github.example.com/./r#1",
    "github.example.com/o/..#1",
    "github.example.com/../r#1",
    "github.example.com/o/%2e%2e/r#1",
    "github.example.com/o/%2E/r#1",
    "github.example.com/o%2fr#1",
    "github.example.com/o/r%00#1",
    "github.example.com/o/r%#1",
    # separators and delimiters a URL parser would act on
    "github.example.com/o/r?x=1#1",
    "github.example.com/o/r;x#1",
    "github.example.com/o\\r#1",
    "github.example.com\\o/r#1",
    "github.example.com/o/r@evil.example#1",
    "user@github.example.com/o/r#1",
    "user:pw@github.example.com/o/r#1",
    "github.example.com@evil.example/o/r#1",
    "evil.example#@github.example.com/o/r#1",
    "evil.example/@github.example.com/o/r#1",
    "https://github.example.com/o/r#1",
    "//github.example.com/o/r#1",
    "javascript:alert(1)//github.example.com/o/r#1",
    "data:text/html,x#1",
    "github.example.com/o/r\n#1",
    "github.example.com/o/r#1\n",
    "\ngithub.example.com/o/r#1",
    "github.example.com/o/r#1\r\nHost: x",
    # whitespace and control characters
    " github.example.com/o/r#1",
    "github.example.com/o/r#1 ",
    "github.example.com/o r#1",
    "github.example.com/o\tr#1",
    "github.example.com/o\x00r#1",
    "github.example.com/o\x1br#1",
    "github.example.com/o\x7fr#1",
    "github.example.com/o r#1",
    "github.example.com/o r#1",
    "github.example.com/o​r#1",
    "github.example.com/o‮r#1",
    "github.example.com/o﻿r#1",
    # unicode, IDN and lookalikes
    "github.example.com/ö/r#1",
    "github.example.com/o/а#1",
    "gıthub.example.com/o/r#1",
    "github.exаmple.com/o/r#1",
    "github.example.com/o/r#１",
    "github.example.com/o/r#١",
    "github.example.com:８０/o/r#1",
    "github。example。com/o/r#1",
    "github.example.com／o/r#1",
    "xn--github.example.com/o/r#1",
    "github.example.com/o/\U0001f600#1",
    "github.example.com/\ud800#1",
    # numbers
    "github.example.com/o/r#0",
    "github.example.com/o/r#01",
    "github.example.com/o/r#12345678901",
    "github.example.com/o/r#-1",
    "github.example.com/o/r#+1",
    "github.example.com/o/r#1.0",
    "github.example.com/o/r#0x1",
    "github.example.com/o/r#1e3",
    # ports
    "github.example.com:/o/r#1",
    "github.example.com:0/o/r#1",
    "github.example.com:080/o/r#1",
    "github.example.com:123456/o/r#1",
    "github.example.com:443:443/o/r#1",
    "gh.example.com:0443/o/r#1",
    "gh.example.com:+443/o/r#1",
    # host labels
    "-github.example.com/o/r#1",
    "github..example.com/o/r#1",
    ".github.example.com/o/r#1",
    "github.example.com:/#1",
    # a payload that is not a pr
    "github.example.com/o/r/pull/1",
    "https://github.example.com/o/r/pull/1",
]


@pytest.mark.parametrize("pr", PR_BAD)
def test_pr_url_refuses_what_the_grammar_or_the_tables_refuse(pr: str) -> None:
    assert build_pr_url(pr, LINKS) == ""


@pytest.mark.parametrize(
    "value",
    [None, True, False, 1, 1.5, ["github.example.com/o/r#1"], {"pr": "x"}, b"github", ("a",)],
)
def test_pr_url_refuses_a_value_that_is_not_a_string(value: object) -> None:
    assert build_pr_url(value, LINKS) == ""


def test_pr_url_refuses_a_string_subclass_with_a_hostile_str() -> None:
    class Sneaky(str):
        def __str__(self) -> str:
            return "evil"

    assert build_pr_url(Sneaky("github.example.com/o/r#1"), LINKS) == ""


def test_pr_url_with_no_hosts_is_empty() -> None:
    assert build_pr_url("github.example.com/o/r#1", Links({}, {})) == ""


def test_pr_url_length_is_bounded() -> None:
    big = "a" * 600
    assert build_pr_url(f"github.example.com/{big}#1", LINKS) == ""
    assert build_pr_url("github.example.com/" + "a/" * 5000 + "a#1", LINKS) == ""
    ok = "a" * 200
    got = build_pr_url(f"github.example.com/{ok}#1", LINKS)
    assert got == f"https://github.example.com/{ok}/pull/1"
    assert len(got) <= 1024


# --- ticket_url -------------------------------------------------------------------------------

TICKET_GOOD = [
    ("example-site", "ABC-1", "https://tickets.example.com/browse/ABC-1"),
    ("example-site", "abc-123456789", "https://tickets.example.com/browse/abc-123456789"),
    ("example-site", "A1_b-0007", "https://tickets.example.com/browse/A1_b-0007"),
    ("other", "Q-9", "https://other.example.com:8080/t/Q-9"),
]


@pytest.mark.parametrize(("site", "key", "want"), TICKET_GOOD)
def test_ticket_url_builds_base_plus_key(site: str, key: str, want: str) -> None:
    assert build_ticket_url(site, key, LINKS) == want


KEY_BAD = [
    "",
    "ABC",
    "ABC-",
    "-1",
    "1ABC-1",
    "_A-1",
    "ABC--1",
    "A-B-1",
    "ABC-1-2",
    "ABC-1234567890",
    "ABC-1/../x",
    "ABC-1/x",
    "ABC-1?x=1",
    "ABC-1#x",
    "ABC-1&x",
    "ABC-1%2f",
    "ABC%2d1",
    "ABC-1\n",
    "\nABC-1",
    "ABC-1\r",
    "ABC-1 ",
    " ABC-1",
    "AB C-1",
    "ABC-1\x00",
    "ABC-1\t",
    "ABC-１",
    "ABC-١",
    "АBC-1",
    "ABC‐" + "1",
    "ABC-1@evil.example",
    "ABC-1\\",
    "../ABC-1",
    "ABC-1​",
    "ABC-1‮",
    "ABC-1\ud800",
    "@evil.example/ABC-1",
    "ABC-1:",
    "ABC-1.0",
]


@pytest.mark.parametrize("key", KEY_BAD)
def test_ticket_url_refuses_a_key_outside_the_grammar(key: str) -> None:
    assert build_ticket_url("example-site", key, LINKS) == ""


SITE_BAD = [
    "unknown",
    "",
    "Example-Site",
    "example-site ",
    " example-site",
    "example-site\n",
    "example-site/",
    "example-site/../other",
    "example-site\x00",
    "example‐site",
    "exаmple-site",
    "other​",
    "tickets.example.com",
    "https://tickets.example.com/browse/",
]


@pytest.mark.parametrize("site", SITE_BAD)
def test_ticket_url_refuses_a_site_that_is_not_a_table_key(site: str) -> None:
    assert build_ticket_url(site, "ABC-1", LINKS) == ""


@pytest.mark.parametrize("value", [None, True, 1, 1.5, ["example-site"], {"a": 1}, b"x", ("a",)])
def test_ticket_url_refuses_values_that_are_not_strings(value: object) -> None:
    assert build_ticket_url(value, "ABC-1", LINKS) == ""
    assert build_ticket_url("example-site", value, LINKS) == ""


def test_ticket_url_with_no_tickets_is_empty() -> None:
    assert build_ticket_url("example-site", "ABC-1", Links({}, {})) == ""


def test_ticket_url_length_is_bounded() -> None:
    assert build_ticket_url("example-site", "A" * 5000 + "-1", LINKS) == ""
    got = build_ticket_url("example-site", "A" * 100 + "-1", LINKS)
    assert got == "https://tickets.example.com/browse/" + "A" * 100 + "-1"


def test_ticket_url_result_over_1024_is_empty() -> None:
    long_base = "https://t.example.com/" + "a" * 1000 + "/"
    assert len(long_base) <= 1024
    links = Links({}, {"big": long_base})
    assert build_ticket_url("big", "ABC-1", links) == ""


# --- derived fields from a notification -------------------------------------------------------


def test_links_come_from_the_fixed_payload_ids_only() -> None:
    n = hook({"pr": "github.example.com/o/r#5", "site": "example-site", "key": "ABC-9"})
    got = build_links(rule(), n, LINKS)
    assert got.pr_url == "https://github.example.com/o/r/pull/5"
    assert got.ticket_url == "https://tickets.example.com/browse/ABC-9"
    assert got.link == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"link": "https://evil.example/x", "url": "https://evil.example/y"},
        {"pr_url": "https://evil.example/p", "ticket_url": "https://evil.example/t"},
        {"URL": "https://evil.example/", "Link": "https://evil.example/", "href": "https://x/"},
        {"pr": "github.example.com/o/r#1", "link": "https://evil.example/x"},
    ],
)
@pytest.mark.parametrize("token", ["{{pr_url}}", "{{ticket_url}}", None])
def test_a_payload_field_named_link_or_url_never_becomes_a_link(
    payload: dict[str, Any], token: str | None
) -> None:
    got = build_links(rule(token), hook(payload), LINKS)
    for value in (got.pr_url, got.ticket_url, got.link):
        assert "evil" not in value
        assert "x/" not in value.replace("example.com/o/r/pull/1", "")
    assert got.ticket_url == ""


def test_labels_and_repo_never_build_a_link() -> None:
    n = hook(
        {"tool": "tool"},
        repo="github.example.com/o/r",
        labels=(("pr", "github.example.com/o/r#1"), ("site", "example-site"), ("key", "ABC-1")),
    )
    assert build_links(rule("{{pr_url}}"), n, LINKS) == BuiltLinks("", "", "")


def test_site_and_key_must_both_be_present() -> None:
    assert build_links(rule(), hook({"site": "example-site"}), LINKS).ticket_url == ""
    assert build_links(rule(), hook({"key": "ABC-1"}), LINKS).ticket_url == ""
    assert build_links(rule(), hook({}), LINKS) == BuiltLinks("", "", "")


def test_a_notice_has_no_derived_links() -> None:
    n = notice(link="https://evil.example/x")
    assert build_links(rule("{{pr_url}}"), n, LINKS) == BuiltLinks("", "", "")
    assert build_links(rule("{{ticket_url}}"), n, LINKS) == BuiltLinks("", "", "")


# --- a rule's link ----------------------------------------------------------------------------


def test_rule_link_pr_token() -> None:
    n = hook({"pr": "gl.example.com/g/p#4"})
    got = build_links(rule("{{pr_url}}"), n, LINKS)
    assert got.link == "https://gl.example.com/g/p/-/merge_requests/4"


def test_rule_link_ticket_token() -> None:
    n = hook({"site": "other", "key": "Z-1"})
    assert (
        build_links(rule("{{ticket_url}}"), n, LINKS).link == "https://other.example.com:8080/t/Z-1"
    )


def test_rule_link_that_renders_empty_is_no_link() -> None:
    n = hook({"pr": "nope.example.com/o/r#1"})
    assert build_links(rule("{{pr_url}}"), n, LINKS).link == ""
    assert build_links(rule("{{ticket_url}}"), n, LINKS).link == ""


def test_rule_literal_link_is_used_as_is() -> None:
    lit = "https://status.example.com:8443/path/a.b?x=1&y=2"
    assert build_links(rule(lit), hook({}), LINKS).link == lit
    assert build_links(rule(lit), notice(), LINKS).link == lit


def test_no_rule_link_is_no_link() -> None:
    n = hook({"pr": "github.example.com/o/r#1"})
    assert build_links(rule(None), n, LINKS).link == ""


def test_notice_own_link_never_reaches_the_rendered_link() -> None:
    own = "https://evil.example/phish"
    n = notice(link=own)
    assert build_links(rule(None, message=None), n, LINKS).link == ""
    lit = "https://status.example.com/"
    assert build_links(rule(lit, message=None), n, LINKS).link == lit
    assert build_links(rule("{{pr_url}}", message=None), n, LINKS).link == ""


@pytest.mark.parametrize(
    "bad",
    [
        "http://status.example.com/",
        "HTTPS://status.example.com/",
        "https://",
        "https://u@status.example.com/",
        "https://status.example.com:0/",
        "https://status.example.com:65536/",
        "https://status.example.com/a b",
        "https://status.example.com/a\n",
        "https://status.example.com/ö",
        "https://status.example.com/{{pr_url}}",
        "javascript:alert(1)",
        "{{pr_url}} ",
        " {{pr_url}}",
        "{{pr_url}}{{ticket_url}}",
        "{{source}}",
        "{{payload.link}}",
        "",
        "https://" + "a" * 1100,
    ],
)
def test_a_hand_built_rule_with_a_bad_link_renders_no_link(bad: str) -> None:
    n = hook({"pr": "github.example.com/o/r#1", "site": "example-site", "key": "ABC-1"})
    assert build_links(rule(bad), n, LINKS).link == ""


def test_is_literal_url_matches_the_config_grammar() -> None:
    assert is_literal_url("https://a.example.com")
    assert is_literal_url("https://a.example.com:65535/x/y?z=1")
    assert not is_literal_url("https://a.example.com:65536")
    assert not is_literal_url(None)
    assert not is_literal_url("https://a.example.com/\n")
    assert not is_literal_url("https://a.example.com/" + "a" * 1024)


# --- properties -------------------------------------------------------------------------------

_SAFE = re.compile(r"https://[a-z0-9.:-]+(/[A-Za-z0-9._/-]*)?")


def test_a_built_url_holds_only_safe_characters() -> None:
    for pr, want in PR_GOOD:
        got = build_pr_url(pr, LINKS)
        assert got == want
        assert _SAFE.fullmatch(got)
    for site, key, _ in TICKET_GOOD:
        assert _SAFE.fullmatch(build_ticket_url(site, key, LINKS))


def test_building_is_deterministic_and_does_not_mutate_the_tables() -> None:
    hosts = dict(LINKS.hosts)
    tickets = dict(LINKS.tickets)
    n = hook({"pr": "github.example.com/o/r#5", "site": "example-site", "key": "ABC-9"})
    first = build_links(rule("{{pr_url}}"), n, LINKS)
    for _ in range(3):
        assert build_links(rule("{{pr_url}}"), n, LINKS) == first
    assert LINKS.hosts == hosts
    assert LINKS.tickets == tickets


def test_a_payload_is_not_mutated() -> None:
    payload: dict[str, Any] = {"pr": "github.example.com/o/r#5"}
    n = hook(payload)
    build_links(rule("{{pr_url}}"), n, LINKS)
    assert payload == {"pr": "github.example.com/o/r#5"}


@pytest.mark.parametrize(
    "hostile",
    [
        "a" * 200000 + "!",
        "github.example.com/" + "a" * 200000,
        "a." * 100000 + "#",
        "github.example.com/" + "a-" * 100000 + "#",
        "github.example.com/" + "a/" * 100000 + "#x",
        "-" * 200000,
        ":" * 200000,
        "#" * 200000,
        "github.example.com/o/r#" + "9" * 200000,
    ],
)
def test_hostile_long_input_is_linear(hostile: str) -> None:
    start = time.monotonic()
    assert build_pr_url(hostile, LINKS) == ""
    assert build_ticket_url("example-site", hostile, LINKS) == ""
    assert build_ticket_url(hostile, "ABC-1", LINKS) == ""
    assert time.monotonic() - start < 1.0


# --- render integration -----------------------------------------------------------------------


def test_the_built_urls_render_as_derived_values_and_text_stays_out() -> None:
    n = hook(
        {
            "pr": "github.example.com/o/r#5",
            "site": "example-site",
            "key": "ABC-9",
            "link": "https://evil.example/x",
        }
    )
    r = replace(rule(), message="{{pr_url}} {{ticket_url}} {{link}}")
    built = build_links(r, n, LINKS)
    out = render(r, n, level="info", pr_url=built.pr_url, ticket_url=built.ticket_url)
    assert out.message == (
        "https://github.example.com/o/r/pull/5 https://tickets.example.com/browse/ABC-9"
    )
    assert "evil" not in out.message
