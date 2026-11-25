"""Validation of the merged config into typed objects (#7): one case per rule, each with its
``details.key``; table-driven. The example config and drop-in must validate and also satisfy
``schemas/config.json``, which is kept in step with the validator."""

from __future__ import annotations

import copy
import json
import os
import zoneinfo
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from notice_route import config as config_module
from notice_route.config import ConfigError
from notice_route.gateway import SYSTEM_TZ_DIRS, Gateway
from notice_route.validate import Config, load_valid_config, validate_config

ROOT = Path(__file__).resolve().parent.parent
CANARY = "leak-canary-value"

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database")


@pytest.fixture(autouse=True)
def private_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may read the real user's home or XDG directories."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))


def good() -> dict[str, Any]:
    return {
        "enabled": True,
        "destinations": {
            "phone": {
                "kind": "ha-notice",
                "cmd": ["/usr/bin/ha-notice", "--config", "/etc/ha-notice.yaml"],
                "timeout": "10s",
                "floor": "warn",
                "quiet": "night",
                "max_per_hour": 20,
                "env": ["HA_CRED"],
                "enabled": True,
            },
            "chat": {"kind": "slack-notice", "cmd": ["/usr/bin/slack-notice"]},
        },
        "quiet_hours": {
            "night": {
                "timezone": "America/New_York",
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "start": "22:00",
                "end": "07:00",
            }
        },
        "links": {
            "hosts": {"github.example.com": "github", "gitlab.example.com:8443": "gitlab"},
            "tickets": {"site-a": "https://tickets.example.com/browse/"},
        },
        "defaults": {"dedup_window": "10m"},
        "lock_wait": "2s",
        "retention": {"history": "30d", "event_ids": "14d"},
        "log": True,
        "cron": {"env_file": "/home/example/cron.env"},
        "hooks": {"post-fail": [{"cmd": "/bin/true", "timeout": "30s", "async": False}]},
        "rules": [
            {
                "name": "alert",
                "match": {
                    "source": "notice-route",
                    "hook": "post-*",
                    "repo": "*",
                    "labels": ["team=core"],
                    "where": {"state": "failed", "n": 3, "flag": True},
                },
                "notify": ["chat", "phone"],
                "level": "warn",
                "title": "T {{object_id}}",
                "message": "M {{labels.team}} {{a.b.c.d}} }} ok",
                "link": "{{pr_url}}",
                "dedup_window": "5m",
                "include_text": ["labels.team", "note"],
            },
            {"name": "quiet-drop", "match": {"source": "agent-spawn"}, "drop": True},
            {
                "name": "errors",
                "match": {"level_min": "error"},
                "notify": ["phone"],
                "link": "https://example.com/x",
            },
        ],
    }


DEL = object()


def edit(cfg: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node: Any = cfg
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    last = parts[-1]
    if isinstance(node, list):
        index = int(last)
        if value is DEL:
            del node[index]
        else:
            node[index] = value
    elif value is DEL:
        del node[last]
    else:
        node[last] = value


def build(edits: dict[str, Any]) -> dict[str, Any]:
    cfg = good()
    for path, value in edits.items():
        edit(cfg, path, value)
    return cfg


def check(cfg: dict[str, Any]) -> Config:
    return validate_config(cfg, Gateway())


def rejected(cfg: dict[str, Any]) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        check(cfg)
    assert info.value.code == "config_invalid"
    assert info.value.exit_code == 2
    return info.value


# --- the good config ------------------------------------------------------------------------


def test_the_good_config_validates_into_typed_objects() -> None:
    config = check(good())
    assert config.enabled is True
    phone = config.destinations["phone"]
    assert (phone.kind, phone.timeout, phone.floor, phone.quiet) == (
        "ha-notice",
        10.0,
        "warn",
        "night",
    )
    assert (phone.max_per_hour, phone.env, phone.enabled) == (20, ("HA_CRED",), True)
    assert phone.cmd == ("/usr/bin/ha-notice", "--config", "/etc/ha-notice.yaml")
    window = config.quiet_hours["night"]
    assert (window.start, window.end, window.days) == (22 * 60, 7 * 60, (0, 1, 2, 3, 4, 5, 6))
    assert window.zone.key == "America/New_York"
    assert config.links.hosts == {
        "github.example.com": "github",
        "gitlab.example.com:8443": "gitlab",
    }
    assert config.links.tickets == {"site-a": "https://tickets.example.com/browse/"}
    assert (config.lock_wait, config.retention_history, config.retention_event_ids) == (
        2.0,
        30 * 86400.0,
        14 * 86400.0,
    )
    assert config.cron_env_file == "/home/example/cron.env"
    assert [(h.cmd, h.timeout, h.is_async) for h in config.hooks_post_fail] == [
        ("/bin/true", 30.0, False)
    ]
    alert, drop, passthrough = config.rules
    assert alert.match.source == "notice-route" and alert.match.hook == "post-*"
    assert alert.match.labels == ("team=core",)
    assert dict(alert.match.where) == {"state": "failed", "n": 3, "flag": True}
    assert (alert.level, alert.dedup_window, alert.include_text) == (
        "warn",
        300.0,
        ("labels.team", "note"),
    )
    assert alert.notify == ("chat", "phone") and not alert.drop
    assert drop.drop and drop.notify == () and drop.level is None
    assert passthrough.message is None and passthrough.match.level_min == "error"
    assert passthrough.link == "https://example.com/x"


def test_an_empty_config_gets_every_default() -> None:
    config = check({})
    assert config.enabled is True and config.log is True
    assert (config.lock_wait, config.retention_history, config.retention_event_ids) == (
        2.0,
        30 * 86400.0,
        14 * 86400.0,
    )
    assert config.destinations == {} and config.quiet_hours == {} and config.rules == ()
    assert config.links.hosts == {} and config.links.tickets == {}
    assert config.hooks_post_fail == () and config.cron_env_file is None
    assert config.default_dedup_window == 600.0


def test_a_minimal_destination_and_window_get_their_defaults() -> None:
    cfg = {
        "destinations": {"d": {"kind": "generic", "cmd": ["/bin/x"]}},
        "quiet_hours": {"w": {"timezone": "UTC", "start": "01:00", "end": "02:00"}},
    }
    dest = check(cfg).destinations["d"]
    assert (dest.timeout, dest.floor, dest.quiet, dest.max_per_hour, dest.env, dest.enabled) == (
        10.0,
        "info",
        None,
        30,
        (),
        True,
    )
    assert check(cfg).quiet_hours["w"].days == (0, 1, 2, 3, 4, 5, 6)


def test_rule_dedup_window_defaults_to_the_defaults_key() -> None:
    cfg = build({"rules.0.dedup_window": DEL, "defaults.dedup_window": "5m"})
    assert check(cfg).rules[0].dedup_window == 300.0
    assert (
        check(build({"rules.0.dedup_window": DEL, "defaults": DEL})).rules[0].dedup_window == 600.0
    )


def test_a_cmd_element_that_only_starts_with_send_is_accepted() -> None:
    cfg = build({"destinations.chat.cmd": ["/bin/x", "send=1", "sender"]})
    assert check(cfg).destinations["chat"].cmd == ("/bin/x", "send=1", "sender")


def test_the_input_is_not_modified() -> None:
    cfg = good()
    before = copy.deepcopy(cfg)
    check(cfg)
    assert cfg == before


# --- one negative case per rule -------------------------------------------------------------

BAD: list[tuple[str, dict[str, Any], str, bool]] = []


def bad(name: str, edits: dict[str, Any], key: str, schema: bool = True) -> None:
    BAD.append((name, edits, key, schema))


def bad_each(prefix: str, path: str, values: list[Any], key: str, schema: bool = True) -> None:
    for index, value in enumerate(values):
        bad(f"{prefix}-{index}", {path: value}, key, schema)


# top level and scalars
bad("unknown-top-level-key", {"bogus": 1}, "?")
bad_each("enabled", "enabled", ["yes", 1, None, "true"], "enabled")
bad_each("log", "log", ["true", 0, []], "log")
bad_each(
    "lock-wait-type", "lock_wait", [True, [], {}, "abc", "", "1e3", "-1s", "1h", "2d"], "lock_wait"
)
bad_each(
    "lock-wait-range",
    "lock_wait",
    [0, "0s", 0.5, "999ms", 61, "61s", "2m", "99999999999999999999"],
    "lock_wait",
    False,
)
bad_each("defaults-type", "defaults", [1, [], "x"], "defaults")
bad("defaults-unknown", {"defaults": {"other": 1}}, "defaults.?")
bad_each(
    "dedup-default",
    "defaults.dedup_window",
    [0, "999ms", "25h", "1441m", "2d", True, "x"],
    "defaults.dedup_window",
    False,
)
bad_each("retention-type", "retention", [1, [], "x"], "retention")
bad("retention-unknown", {"retention": {"other": 1}}, "retention.?")
bad_each(
    "retention-history",
    "retention.history",
    ["12h", "86399s", 3600, 0, True, "x", "1w"],
    "retention.history",
    False,
)
bad_each(
    "retention-events",
    "retention.event_ids",
    ["23h", "1m", 0, True, "x"],
    "retention.event_ids",
    False,
)
bad_each("cron-type", "cron", [1, [], "x"], "cron")
bad("cron-unknown", {"cron": {"other": "/x"}}, "cron.?")
bad_each(
    "cron-env-file",
    "cron.env_file",
    ["relative/path", "", "~/x", 5, True, [], "/" + "a" * 4096],
    "cron.env_file",
)

# hooks
bad_each("hooks-type", "hooks", [1, [], "x"], "hooks")
bad("hooks-unknown-hook", {"hooks": {"pre-send": []}}, "hooks.?")
bad_each("hooks-post-fail-type", "hooks.post-fail", [{}, "x", 1], "hooks.post-fail")
bad_each("hooks-entry-type", "hooks.post-fail.0", ["x", 1, []], "hooks.post-fail.0")
bad("hooks-entry-unknown", {"hooks.post-fail.0.other": 1}, "hooks.post-fail.0.?")
bad("hooks-cmd-missing", {"hooks.post-fail.0.cmd": DEL}, "hooks.post-fail.0.cmd")
bad_each(
    "hooks-cmd",
    "hooks.post-fail.0.cmd",
    ["", 1, ["/bin/true"], True, "a" * 4097],
    "hooks.post-fail.0.cmd",
)
bad_each(
    "hooks-timeout",
    "hooks.post-fail.0.timeout",
    [0, "0s", "999ms", "2h", True, "x"],
    "hooks.post-fail.0.timeout",
    False,
)
bad_each("hooks-async", "hooks.post-fail.0.async", ["no", 1, None], "hooks.post-fail.0.async")

# destinations
bad(
    "too-many-destinations",
    {"destinations": {f"d{i}": {"kind": "generic", "cmd": ["/bin/x"]} for i in range(33)}},
    "destinations",
    False,
)
bad(
    "destination-name-uppercase",
    {"destinations": {"Phone": {"kind": "generic", "cmd": ["/bin/x"]}}},
    "destinations.Phone",
)
bad(
    "destination-name-underscore-first",
    {"destinations": {"_wsl": {"kind": "generic", "cmd": ["/bin/x"]}}},
    "destinations.?",
)
bad(
    "destination-name-long",
    {"destinations": {"a" * 33: {"kind": "generic", "cmd": ["/bin/x"]}}},
    "destinations." + "a" * 33,
)
bad(
    "destination-name-space",
    {"destinations": {"a b": {"kind": "generic", "cmd": ["/bin/x"]}}},
    "destinations.?",
)
bad_each("destination-type", "destinations.chat", ["x", 1, [], None], "destinations.chat")
bad("destination-unknown-key", {"destinations.chat.other": 1}, "destinations.chat.?")
bad("kind-missing", {"destinations.chat.kind": DEL}, "destinations.chat.kind")
bad_each(
    "kind", "destinations.chat.kind", ["email", "Generic", 1, None, ""], "destinations.chat.kind"
)
bad("cmd-missing", {"destinations.chat.cmd": DEL}, "destinations.chat.cmd")
bad_each(
    "cmd-type",
    "destinations.chat.cmd",
    ["/bin/x", [], [1], [None], ["/bin/x", 2], {}],
    "destinations.chat.cmd",
)
bad_each(
    "cmd-first-not-absolute",
    "destinations.chat.cmd",
    [["bin/x"], ["./x"], ["x"], [""], ["~/x"]],
    "destinations.chat.cmd",
)
bad(
    "cmd-too-long-list", {"destinations.chat.cmd": ["/bin/x"] + ["a"] * 32}, "destinations.chat.cmd"
)
bad(
    "cmd-element-too-long",
    {"destinations.chat.cmd": ["/bin/x", "a" * 4097]},
    "destinations.chat.cmd",
)
bad("cmd-first-too-long", {"destinations.chat.cmd": ["/" + "a" * 4096]}, "destinations.chat.cmd")
for word in [
    "send",
    "--stdin-json",
    "--json",
    "--actor",
    "--dedupe-key",
    "--actor=x",
    "--dedupe-key=ab",
    "--json=1",
    "--stdin-json=1",
]:
    bad(
        f"cmd-forbidden-{word}",
        {"destinations.chat.cmd": ["/bin/x", "--config", "/c", word]},
        "destinations.chat.cmd",
    )
bad_each(
    "timeout",
    "destinations.chat.timeout",
    [0, "0s", "999ms", "121s", "3m", 121, True, "x", "1h", None],
    "destinations.chat.timeout",
    False,
)
bad_each("floor", "destinations.chat.floor", ["debug", "INFO", 1, None], "destinations.chat.floor")
bad_each(
    "quiet-grammar",
    "destinations.chat.quiet",
    ["Night", "a b", 1, "", "_x"],
    "destinations.chat.quiet",
)
bad("quiet-unknown", {"destinations.chat.quiet": "day"}, "destinations.chat.quiet", False)
bad_each(
    "max-per-hour",
    "destinations.chat.max_per_hour",
    [-1, 3601, "5", True, 1.5, None, []],
    "destinations.chat.max_per_hour",
)
bad_each("env-type", "destinations.chat.env", ["A", {}, 1, None], "destinations.chat.env")
bad_each(
    "env-name",
    "destinations.chat.env",
    [["1A"], ["A-B"], ["A B"], ["A" * 129], [""], [1], ["é"]],
    "destinations.chat.env",
)
for fixed in ["PATH", "LANG", "LC_ALL", "WSL_INTEROP", "WSL_DISTRO_NAME"]:
    bad(f"env-fixed-{fixed}", {"destinations.chat.env": ["OTHER", fixed]}, "destinations.chat.env")
bad("env-duplicate", {"destinations.chat.env": ["A", "A"]}, "destinations.chat.env")
bad_each(
    "destination-enabled",
    "destinations.chat.enabled",
    ["false", 0, None],
    "destinations.chat.enabled",
)

# quiet hours
bad(
    "quiet-hours-name-uppercase",
    {"quiet_hours": {"Night": {"timezone": "UTC", "start": "01:00", "end": "02:00"}}},
    "quiet_hours.Night",
)
bad(
    "quiet-hours-name-space",
    {"quiet_hours": {"a b": {"timezone": "UTC", "start": "01:00", "end": "02:00"}}},
    "quiet_hours.?",
)
bad_each("window-type", "quiet_hours.night", ["x", 1, [], None], "quiet_hours.night")
bad("window-unknown-key", {"quiet_hours.night.other": 1}, "quiet_hours.night.?")
bad("timezone-missing", {"quiet_hours.night.timezone": DEL}, "quiet_hours.night.timezone")
bad_each(
    "timezone",
    "quiet_hours.night.timezone",
    [
        "../x",
        "a/../b",
        "/etc/passwd",
        "localtime",
        "LocalTime",
        "posixrules",
        "Factory",
        "Nowhere/Zone",
        "Zone",
        "America",
        "a/b/c/d",
        "",
        "x" * 65,
        1,
        None,
        ["UTC"],
        "UTC ",
        "America/New York",
        "tzdata.zi",
    ],
    "quiet_hours.night.timezone",
    False,
)
bad("start-missing", {"quiet_hours.night.start": DEL}, "quiet_hours.night.start")
bad("end-missing", {"quiet_hours.night.end": DEL}, "quiet_hours.night.end")
for field in ("start", "end"):
    bad_each(
        f"window-{field}",
        f"quiet_hours.night.{field}",
        [
            "24:00",
            "7:00",
            "07:60",
            "22:00:00",
            "2200",
            2200,
            22.0,
            "",
            "ab:cd",
            "07:00 ",
            None,
            "٠٧:٠٠",
        ],
        f"quiet_hours.night.{field}",
    )
bad("window-end-equals-start", {"quiet_hours.night.end": "22:00"}, "quiet_hours.night.end", False)
bad_each("days-type", "quiet_hours.night.days", ["mon", [], {}, 1, None], "quiet_hours.night.days")
bad_each(
    "days-values",
    "quiet_hours.night.days",
    [
        ["Mon"],
        ["monday"],
        [1],
        ["mon", "mon"],
        [None],
        ["mon"] * 8,
        ["mon", "tue", "wed", "thu", "fri", "sat", "sun", "mon"],
    ],
    "quiet_hours.night.days",
)

# links
bad_each("links-type", "links", [1, [], "x"], "links")
bad("links-unknown", {"links": {"other": {}}}, "links.?")
bad_each("hosts-type", "links.hosts", ["x", 1, [], None], "links.hosts")
bad_each("tickets-type", "links.tickets", ["x", 1, [], None], "links.tickets")
bad("host-uppercase", {"links.hosts": {"GitHub.com": "github"}}, "links.hosts.GitHub.com")
bad("host-port-zero", {"links.hosts": {"a.com:0": "github"}}, "links.hosts.?", False)
bad("host-port-too-big", {"links.hosts": {"a.com:65536": "github"}}, "links.hosts.?", False)
bad("host-port-too-long", {"links.hosts": {"a.com:123456": "github"}}, "links.hosts.?")
bad("host-leading-dash", {"links.hosts": {"-a.com": "github"}}, "links.hosts.?")
bad("host-empty-label", {"links.hosts": {"a..com": "github"}}, "links.hosts.a..com")
bad("host-with-path", {"links.hosts": {"a.com/x": "github"}}, "links.hosts.?")
for index, forge in enumerate(["bitbucket", "GitHub", 1, None, ["github"]]):
    bad(
        f"host-forge-{index}",
        {"links.hosts": {"github.example.com": forge}},
        "links.hosts.github.example.com",
    )
bad("site-name", {"links.tickets": {"_x": "https://t.example.com/"}}, "links.tickets.?")
bad(
    "site-name-long",
    {"links.tickets": {"a" * 65: "https://t.example.com/"}},
    "links.tickets.?",
    False,
)
for index, url in enumerate(
    [
        "http://t.example.com/",
        "https://t.example.com",
        "https://t.example.com/browse",
        "https://T.example.com/",
        "https://u@t.example.com/",
        "https://t.example.com:0/",
        "https://t.example.com:65536/",
        "https://t.example.com/a b/",
        "https://t.example.com/?q=#",
        "https://t.example.com/" + "a" * 1024 + "/",
        "https:///",
        "//t.example.com/",
        "",
        1,
        None,
    ]
):
    bad(
        f"ticket-url-{index}",
        {"links.tickets.site-a": url},
        "links.tickets.site-a",
        index not in (5, 6),
    )

# rules
bad(
    "too-many-rules",
    {"rules": [{"name": f"r{i}", "match": {}, "drop": True} for i in range(257)]},
    "rules",
    False,
)
bad_each("rules-type", "rules", [{}, "x", 1], "rules")
bad_each("rule-type", "rules.0", ["x", 1, [], None], "rules.0")
bad("rule-unknown-key", {"rules.0.other": 1}, "rules.0.?")
bad("rule-name-missing", {"rules.0.name": DEL}, "rules.0.name")
bad_each(
    "rule-name", "rules.0.name", ["Alert", "-a", "a b", "", "a" * 65, 1, None, "é"], "rules.0.name"
)
bad("rule-name-duplicate", {"rules.1.name": "alert"}, "rules.1.name", False)
bad("match-missing", {"rules.0.match": DEL}, "rules.0.match")
bad_each("match-type", "rules.0.match", ["x", 1, [], None], "rules.0.match")
bad("match-unknown-condition", {"rules.0.match.regex": "x"}, "rules.0.match.?")
for cond in ("source", "hook", "repo"):
    bad_each(
        f"match-{cond}",
        f"rules.0.match.{cond}",
        ["", "a" * 257, "a*b", "**", "a**", "*a*", "*a", 1, True, None, ["a"]],
        f"rules.0.match.{cond}",
    )
bad_each(
    "match-labels-type",
    "rules.0.match.labels",
    ["team=core", [], {}, 1, None],
    "rules.0.match.labels",
)
bad_each(
    "match-labels-entry",
    "rules.0.match.labels",
    [
        ["team"],
        ["Team=x"],
        ["=x"],
        ["a=b c"],
        [1],
        ["a=" + "b" * 257],
        [("a" * 65) + "=x"],
        ["a=b", "a=b"],
        [None],
    ],
    "rules.0.match.labels",
)
bad(
    "match-labels-33",
    {"rules.0.match.labels": [f"k{i}=v" for i in range(33)]},
    "rules.0.match.labels",
)
bad_each("match-where-type", "rules.0.match.where", ["x", [], {}, 1, None], "rules.0.match.where")
bad_each(
    "match-where-key",
    "rules.0.match.where",
    [{"1a": "x"}, {"a b": "x"}, {"a.": "x"}, {"": "x"}, {"a.b.c.d.e": "x"}, {"a-b": "x"}],
    "rules.0.match.where",
)
bad_each(
    "match-where-value",
    "rules.0.match.where",
    [{"a": 1.5}, {"a": None}, {"a": ["x"]}, {"a": {"b": 1}}, {"a": "x" * 1025}],
    "rules.0.match.where",
)
bad(
    "match-where-17",
    {"rules.0.match.where": {f"k{i}": "v" for i in range(17)}},
    "rules.0.match.where",
)
bad_each(
    "match-level-min",
    "rules.2.match.level_min",
    ["debug", "INFO", 1, None],
    "rules.2.match.level_min",
)
for other, value in (("hook", "x"), ("repo", "x"), ("where", {"a": "b"})):
    bad(
        f"level-min-with-{other}",
        {f"rules.2.match.{other}": value},
        "rules.2.match.level_min",
        False,
    )
bad_each("drop-type", "rules.1.drop", ["true", 1, None], "rules.1.drop")
for extra, value in (
    ("notify", ["chat"]),
    ("level", "info"),
    ("title", "t"),
    ("message", "m"),
    ("link", "{{pr_url}}"),
    ("dedup_window", "5m"),
    ("include_text", ["a"]),
):
    bad(f"drop-with-{extra}", {f"rules.1.{extra}": value}, f"rules.1.{extra}", False)
bad("notify-missing", {"rules.0.notify": DEL}, "rules.0.notify")
bad_each("notify-type", "rules.0.notify", ["chat", [], {}, 1, None], "rules.0.notify")
bad_each(
    "notify-entry",
    "rules.0.notify",
    [[1], [None], ["Chat"], [""], ["chat", "chat"]],
    "rules.0.notify",
)
bad("notify-9", {"rules.0.notify": ["chat"] * 9}, "rules.0.notify")
bad("notify-unknown-destination", {"rules.0.notify": ["chat", "nowhere"]}, "rules.0.notify", False)
bad_each("level", "rules.0.level", ["debug", "WARN", 1, None], "rules.0.level")
bad("level-missing-for-hook-rule", {"rules.0.level": DEL}, "rules.0.level")
bad("level-missing-empty-match", {"rules.0.level": DEL, "rules.0.match": {}}, "rules.0.level")
bad(
    "passthrough-without-level-min",
    {"rules.0.message": DEL, "rules.0.title": DEL},
    "rules.0.match.level_min",
)
bad("passthrough-with-hook-condition", {"rules.2.match": {"hook": "x"}}, "rules.2.match.level_min")
bad(
    "title-without-message",
    {"rules.0.message": DEL, "rules.0.match": {"level_min": "info"}},
    "rules.0.title",
)
bad("passthrough-token-link", {"rules.2.link": "{{pr_url}}"}, "rules.2.link", False)
bad_each(
    "dedup-window-type",
    "rules.0.dedup_window",
    [True, [], "x", "1e3", None, "-5m"],
    "rules.0.dedup_window",
)
bad_each(
    "dedup-window-range",
    "rules.0.dedup_window",
    [0, "0s", "999ms", "25h", "1441m", "2d"],
    "rules.0.dedup_window",
    False,
)
bad_each(
    "include-text-type", "rules.0.include_text", ["a", [], {}, 1, None], "rules.0.include_text"
)
bad_each(
    "include-text-entry",
    "rules.0.include_text",
    [
        ["1a"],
        ["a b"],
        ["labels."],
        ["labels.Up"],
        ["a.b.c.d.e"],
        [1],
        [None],
        ["a", "a"],
        ["{{a}}"],
    ],
    "rules.0.include_text",
)
bad(
    "include-text-17",
    {"rules.0.include_text": [f"f{i}" for i in range(17)]},
    "rules.0.include_text",
)

TEMPLATES = [
    "",
    "x" * 2001,
    "{{ x }}",
    "{{x }}",
    "{{x",
    "x {{",
    "{{}}",
    "{{x y}}",
    "{{1a}}",
    "{{a.b.c.d.e}}",
    "{{a..b}}",
    "{{labels.}}",
    "{{labels.UPPER}}",
    "{{labels}}x{{labels.}}",
    "{{a-b}}",
    "{{{x}}}",
    "{x}} {{",
    "{{x}} {{y",
    "{{a" + "b" * 64 + "}}",
    "{{labels." + "a" * 65 + "}}",
    1,
    None,
    ["x"],
]
for field in ("title", "message"):
    bad_each(f"template-{field}", f"rules.0.{field}", TEMPLATES, f"rules.0.{field}")
bad("message-over-2000-with-tokens", {"rules.0.message": "{{a}}" * 401}, "rules.0.message")
bad_each(
    "link",
    "rules.0.link",
    [
        "",
        "{{pr_url}} ",
        "{{other}}",
        "{{ pr_url }}",
        "http://example.com",
        "https://Example.com",
        "https://example.com:0",
        "https://example.com:65536",
        "https://u@example.com",
        "https://example.com/a b",
        "https://example.com#frag",
        "https://example.com/" + "a" * 1024,
        "ftp://example.com",
        "https://",
        "https:///x",
        "example.com",
        1,
        None,
        ["https://example.com"],
    ],
    "rules.0.link",
    False,
)


# R1/R6/R7/R8 (review round 1)
bad("hook-cmd-nul", {"hooks.post-fail.0.cmd": "a\x00b"}, "hooks.post-fail.0.cmd")
bad("cmd-arg-nul", {"destinations.chat.cmd": ["/bin/x", "a\x00"]}, "destinations.chat.cmd")
bad("cmd-first-nul", {"destinations.chat.cmd": ["/bin/x\x00y"]}, "destinations.chat.cmd")
bad("cron-env-file-nul", {"cron.env_file": "/a\x00b"}, "cron.env_file")
bad("where-value-nul", {"rules.0.match.where": {"a": "x\x00"}}, "rules.0.match.where")
bad("wildcard-nul", {"rules.0.match.source": "a\x00"}, "rules.0.match.source")
bad("env-4097", {"destinations.chat.env": [f"E{i}" for i in range(4097)]}, "destinations.chat.env")
bad("passthrough-link-pr-token", {"rules.2.link": "{{pr_url}}"}, "rules.2.link")
bad("passthrough-link-ticket-token", {"rules.2.link": "{{ticket_url}}"}, "rules.2.link")
bad("huge-int-duration", {"lock_wait": 10**400}, "lock_wait", False)
bad("huge-int-retention", {"retention.history": 10**400}, "retention.history", False)
bad("nan-duration", {"lock_wait": float("nan")}, "lock_wait", False)
bad("inf-duration", {"lock_wait": float("inf")}, "lock_wait", False)
bad(
    "retention-just-below-1d",
    {"retention.history": "23.99999999999999999999h"},
    "retention.history",
    False,
)
bad(
    "timeout-just-below-1s",
    {"destinations.chat.timeout": "0.9999999999999999999s"},
    "destinations.chat.timeout",
    False,
)
bad(
    "timeout-just-above-120s",
    {"destinations.chat.timeout": "120.0000000000000001s"},
    "destinations.chat.timeout",
    False,
)
bad(
    "dedup-just-above-24h",
    {"rules.0.dedup_window": "86400.0000000000000001s"},
    "rules.0.dedup_window",
    False,
)
bad(
    "hook-timeout-just-above-1h",
    {"hooks.post-fail.0.timeout": "3600.0000000000000001s"},
    "hooks.post-fail.0.timeout",
    False,
)


@pytest.mark.parametrize(("edits", "key"), [(e, k) for _, e, k, _ in BAD], ids=[n for n, *_ in BAD])
def test_each_invalid_rule_is_config_invalid_with_its_key(edits: dict[str, Any], key: str) -> None:
    assert rejected(build(edits)).key == key


@pytest.mark.parametrize(
    "edits", [e for _, e, _, schema in BAD if schema], ids=[n for n, _, _, s in BAD if s]
)
def test_what_the_schema_can_express_it_also_refuses(edits: dict[str, Any]) -> None:
    validator = jsonschema.Draft202012Validator(SCHEMA)
    assert not validator.is_valid(build(edits))


SCHEMA = json.loads((ROOT / "schemas" / "config.json").read_text("utf-8"))


def test_the_good_config_satisfies_the_schema() -> None:
    jsonschema.Draft202012Validator(SCHEMA).validate(good())


# --- boundaries that are valid --------------------------------------------------------------

OK: list[tuple[str, dict[str, Any]]] = [
    ("timeout-low", {"destinations.chat.timeout": 1}),
    ("timeout-high", {"destinations.chat.timeout": "120s"}),
    ("timeout-ms", {"destinations.chat.timeout": "1000ms"}),
    ("timeout-minutes", {"destinations.chat.timeout": "2m"}),
    ("timeout-float", {"destinations.chat.timeout": 1.5}),
    ("max-per-hour-zero", {"destinations.chat.max_per_hour": 0}),
    ("max-per-hour-top", {"destinations.chat.max_per_hour": 3600}),
    ("cmd-32", {"destinations.chat.cmd": ["/bin/x"] + ["a"] * 31}),
    ("cmd-4096", {"destinations.chat.cmd": ["/bin/x", "a" * 4096]}),
    (
        "cmd-similar-words",
        {"destinations.chat.cmd": ["/bin/x", "--actors", "sender", "--config=/c", "--jsonx"]},
    ),
    ("cmd-send-path", {"destinations.chat.cmd": ["/usr/bin/send"]}),
    ("env-128", {"destinations.chat.env": ["A" * 128, "_b1"]}),
    ("env-empty", {"destinations.chat.env": []}),
    ("destination-disabled", {"destinations.chat.enabled": False}),
    (
        "generic-and-wsl",
        {"destinations.chat.kind": "generic", "destinations.phone.kind": "wsl-notice"},
    ),
    (
        "name-32",
        {
            "destinations": {"a" * 32: {"kind": "generic", "cmd": ["/bin/x"]}},
            "rules": [],
            "quiet_hours": {},
        },
    ),
    ("window-forward", {"quiet_hours.night.start": "01:00", "quiet_hours.night.end": "02:00"}),
    ("window-wide", {"quiet_hours.night.start": "00:00", "quiet_hours.night.end": "23:59"}),
    ("window-days", {"quiet_hours.night.days": ["sat"]}),
    ("zone-utc", {"quiet_hours.night.timezone": "UTC"}),
    ("zone-offset-name", {"quiet_hours.night.timezone": "Etc/GMT+5"}),
    ("host-port-max", {"links.hosts": {"a.example.com:65535": "github"}}),
    (
        "ticket-url-port-and-path",
        {"links.tickets.site-a": "https://t.example.com:8443/a/b?x=1&y=2/"},
    ),
    ("ticket-site-dots", {"links.tickets": {"Site.A_1-b": "https://t.example.com/"}}),
    ("lock-wait-max", {"lock_wait": 60}),
    ("lock-wait-min", {"lock_wait": "1s"}),
    ("dedup-24h", {"rules.0.dedup_window": "24h"}),
    ("dedup-1d", {"rules.0.dedup_window": "1d"}),
    ("dedup-1s", {"rules.0.dedup_window": 1}),
    ("default-dedup-24h", {"defaults.dedup_window": "24h"}),
    ("retention-1d", {"retention.history": "1d", "retention.event_ids": "24h"}),
    ("retention-big", {"retention.history": "3650d"}),
    ("timeout-long-digits", {"destinations.chat.timeout": "1." + "0" * 40 + "s"}),
    ("timeout-huge-digit-string", {"destinations.chat.timeout": "0" * 6000 + "5s"}),
    ("dedup-exact-24h", {"rules.0.dedup_window": "86400.0000000000000000s"}),
    ("retention-exact-1d", {"retention.history": "24.0000000000000000000000000000000000000h"}),
    ("hook-bounds", {"hooks.post-fail.0.timeout": "1s"}),
    ("hook-minimal", {"hooks.post-fail": [{"cmd": "x"}]}),
    ("no-hooks", {"hooks": {}}),
    ("match-empty", {"rules.0.match": {}}),
    ("match-wildcards", {"rules.0.match": {"source": "*", "hook": "a*", "repo": "x/y"}}),
    ("match-256", {"rules.0.match.source": "a" * 256}),
    ("match-labels-32", {"rules.0.match.labels": [f"k{i}=v" for i in range(32)]}),
    ("match-label-empty-value", {"rules.0.match.labels": ["k="]}),
    ("match-label-max", {"rules.0.match.labels": ["a" * 64 + "=" + "b" * 256]}),
    ("match-where-16", {"rules.0.match.where": {f"k{i}": "v" for i in range(16)}}),
    ("match-where-long-string", {"rules.0.match.where": {"a.b.c.d": "x" * 1024}}),
    ("match-where-empty-string", {"rules.0.match.where": {"a": ""}}),
    ("match-where-label-path", {"rules.0.match.where": {"labels.x": "y"}}),
    ("level-min-only", {"rules.2.match": {"level_min": "info", "source": "x", "labels": ["a=b"]}}),
    ("level-and-level-min", {"rules.2.level": "error"}),
    (
        "level-missing-with-level-min",
        {"rules.0.level": DEL, "rules.0.match": {"level_min": "warn"}},
    ),
    ("drop-false-explicit", {"rules.0.drop": False}),
    ("drop-without-level", {"rules.1.match": {"level_min": "info"}}),
    (
        "notify-8",
        {
            "destinations": {f"d{i}": {"kind": "generic", "cmd": ["/bin/x"]} for i in range(8)},
            "rules.0.notify": [f"d{i}" for i in range(8)],
            "rules.2.notify": ["d0"],
        },
    ),
    ("template-literals", {"rules.0.message": "a }} b {x} { {y}} }}{{z}}{{labels.a.b-c}}"}),
    ("template-2000", {"rules.0.message": "x" * 2000}),
    ("template-unicode", {"rules.0.message": "héllo 👩‍💻 {{x}}"}),
    ("template-depth", {"rules.0.title": "{{a.b.c.d}}{{_x}}{{labels.a.b}}{{labels}}"}),
    ("link-ticket", {"rules.0.link": "{{ticket_url}}"}),
    ("link-literal-port-path", {"rules.0.link": "https://example.com:8443/a/b?c=d&e=%20~+-.="}),
    ("link-1024", {"rules.0.link": "https://example.com/" + "a" * (1024 - 20)}),
    ("link-no-path", {"rules.0.link": "https://example.com"}),
    ("include-16", {"rules.0.include_text": [f"f{i}" for i in range(16)]}),
    ("rules-256", {"rules": [{"name": f"r{i}", "match": {}, "drop": True} for i in range(256)]}),
    ("no-rules", {"rules": []}),
]


@pytest.mark.parametrize(("edits",), [(e,) for _, e in OK], ids=[n for n, _ in OK])
def test_boundary_values_are_valid(edits: dict[str, Any]) -> None:
    check(build(edits))


# --- time zones ------------------------------------------------------------------------------


def test_bad_zone_keys_are_refused_even_with_pythontzpath_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zones = tmp_path / "zones"
    (zones / "Fake").mkdir(parents=True)
    source = next(Path(d) / "UTC" for d in SYSTEM_TZ_DIRS if (Path(d) / "UTC").is_file())
    for name in ("Fake/Zone", "localtime", "posixrules", "Factory"):
        (zones / name).write_bytes(source.read_bytes())
    monkeypatch.setenv("PYTHONTZPATH", str(zones))
    monkeypatch.setattr(zoneinfo, "TZPATH", (str(zones),))
    for key in ("../x", "localtime", "posixrules", "Factory", "Fake/Zone", "Nowhere/Zone"):
        cfg = build({"quiet_hours.night.timezone": key})
        assert rejected(cfg).key == "quiet_hours.night.timezone"


def test_the_zone_comes_from_the_gateway_database(tmp_path: Path) -> None:
    zones = tmp_path / "zones"
    (zones / "Fake").mkdir(parents=True)
    source = next(Path(d) / "UTC" for d in SYSTEM_TZ_DIRS if (Path(d) / "UTC").is_file())
    (zones / "Fake" / "Zone").write_bytes(source.read_bytes())
    cfg = build({"quiet_hours.night.timezone": "Fake/Zone"})
    config = validate_config(cfg, Gateway(tz_dirs=(str(zones),)))
    assert config.quiet_hours["night"].zone.key == "Fake/Zone"
    with pytest.raises(ConfigError):
        validate_config(build({}), Gateway(tz_dirs=(str(zones),)))


# --- errors never carry values --------------------------------------------------------------


def with_key(cfg: dict[str, Any], parent: str, key: str, value: Any) -> dict[str, Any]:
    node: Any = cfg
    for part in parent.split(".") if parent else []:
        node = node[int(part)] if isinstance(node, list) else node[part]
    node[key] = value
    return cfg


@pytest.mark.parametrize(
    "edits",
    [
        {"enabled": CANARY},
        {"lock_wait": CANARY},
        {"destinations.chat.kind": CANARY},
        {"destinations.chat.quiet": CANARY},
        {"destinations.chat.env": [CANARY + "!"]},
        {"quiet_hours.night.timezone": CANARY},
        {"quiet_hours.night.start": CANARY},
        {"rules.0.message": "{{" + CANARY},
        {"rules.0.match.where": {CANARY + " ": "x"}},
        {"rules.0.match.labels": [CANARY]},
        {"rules.0.link": CANARY},
        {"rules.0.notify": [CANARY]},
        {"rules.0.dedup_window": CANARY},
        {"links.hosts": {CANARY + " x": "github"}},
        {"links.tickets": {"site": CANARY}},
    ],
)
def test_the_error_never_holds_a_value(edits: dict[str, Any]) -> None:
    error = rejected(build(edits))
    assert CANARY not in error.key
    assert CANARY not in str(error)


@pytest.mark.parametrize(
    ("parent", "expected"),
    [
        ("", "?"),
        ("destinations.chat", "destinations.chat.?"),
        ("quiet_hours.night", "quiet_hours.night.?"),
        ("rules.0", "rules.0.?"),
        ("rules.0.match", "rules.0.match.?"),
        ("links", "links.?"),
        ("defaults", "defaults.?"),
        ("hooks.post-fail.0", "hooks.post-fail.0.?"),
    ],
)
def test_an_unknown_key_is_never_echoed(parent: str, expected: str) -> None:
    error = rejected(with_key(good(), parent, CANARY, 1))
    assert error.key == expected


def test_validation_failure_context_holds_no_values() -> None:
    error = rejected(build({"destinations.chat.cmd": ["/bin/x", CANARY, "send"]}))
    assert error.__context__ is None or CANARY not in repr(error.__context__)


# --- the whole pipeline ----------------------------------------------------------------------


def test_the_example_config_and_the_drop_in_validate_as_one_config(
    home: Path,
) -> None:
    cfg_dir = home / ".config" / "notice-route"
    (cfg_dir / "config.d").mkdir(parents=True)
    cfg_dir.chmod(0o700)
    (cfg_dir / "config.d").chmod(0o700)
    for source, dest in (
        (ROOT / "config.example.yaml", cfg_dir / "config.yaml"),
        (ROOT / "examples" / "awkit-wiring.yaml", cfg_dir / "config.d" / "awkit-wiring.yaml"),
    ):
        dest.write_bytes(source.read_bytes())
        dest.chmod(0o600)
    gateway = Gateway(passwd_home=lambda: str(home))
    loaded = config_module.load_config(gateway)
    assert loaded is not None
    jsonschema.Draft202012Validator(SCHEMA).validate(loaded.data)
    config = load_valid_config(gateway)
    assert config is not None
    assert set(config.destinations) == {"phone", "team-chat", "desktop"}
    assert len(config.rules) == len(loaded.data["rules"]) > 2


def test_load_valid_config_is_none_when_unconfigured(home: Path) -> None:
    assert load_valid_config(Gateway(passwd_home=lambda: str(home))) is None


def test_load_valid_config_reports_a_validation_failure(home: Path) -> None:
    cfg_dir = home / ".config" / "notice-route"
    cfg_dir.mkdir(parents=True)
    cfg_dir.chmod(0o700)
    path = cfg_dir / "config.yaml"
    path.write_text("lock_wait: 0s\n")
    path.chmod(0o600)
    with pytest.raises(ConfigError) as info:
        load_valid_config(Gateway(passwd_home=lambda: str(home)))
    assert (info.value.code, info.value.key) == ("config_invalid", "lock_wait")


def test_an_unquoted_clock_time_is_a_string_for_the_strict_loader(home: Path) -> None:
    cfg_dir = home / ".config" / "notice-route"
    cfg_dir.mkdir(parents=True)
    cfg_dir.chmod(0o700)
    path = cfg_dir / "config.yaml"
    path.write_text("quiet_hours:\n  n:\n    timezone: UTC\n    start: 22:00\n    end: '07:00'\n")
    path.chmod(0o600)
    config = load_valid_config(Gateway(passwd_home=lambda: str(home)))
    assert config is not None and config.quiet_hours["n"].start == 22 * 60


# --- review round 1 ----------------------------------------------------------------------------


def _utc_source() -> bytes:
    return next(Path(d) / "UTC" for d in SYSTEM_TZ_DIRS if (Path(d) / "UTC").is_file()).read_bytes()


@pytest.mark.parametrize(
    "key", ["posix/Factory", "right/Factory", "posix/localtime", "right/posixrules"]
)
def test_a_refused_zone_name_is_refused_under_a_prefix_too(tmp_path: Path, key: str) -> None:
    zones = tmp_path / "zones"
    for prefix in ("posix", "right"):
        (zones / prefix).mkdir(parents=True)
        for name in ("Factory", "localtime", "posixrules"):
            (zones / prefix / name).write_bytes(_utc_source())
    cfg = build({"quiet_hours.night.timezone": key})
    with pytest.raises(ConfigError) as info:
        validate_config(cfg, Gateway(tz_dirs=(str(zones),)))
    assert info.value.key == "quiet_hours.night.timezone"


def _non_str_keys() -> list[tuple[dict[str, Any], str]]:
    dest = {"kind": "generic", "cmd": ["/bin/x"]}
    return [
        ({"destinations": {1: dest}}, "destinations.?"),
        (
            {"quiet_hours": {1: {"timezone": "UTC", "start": "01:00", "end": "02:00"}}},
            "quiet_hours.?",
        ),
        ({"links": {"hosts": {1: "github"}}}, "links.hosts.?"),
        ({"links": {"tickets": {1: "https://t.example.com/"}}}, "links.tickets.?"),
        ({"rules.0.match.where": {1: "x"}}, "rules.0.match.where"),
        ({"destinations.chat": {1: 1}}, "destinations.chat.?"),
        ({"rules.0": {1: 1}}, "rules.0.?"),
        ({"retention": {None: 1}}, "retention.?"),
    ]


@pytest.mark.parametrize(("edits", "key"), _non_str_keys())
def test_a_mapping_key_that_is_not_a_string_is_config_invalid(
    edits: dict[str, Any], key: str
) -> None:
    assert rejected(build(edits)).key == key
