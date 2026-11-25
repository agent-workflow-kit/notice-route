"""The fake notifier answers the documented call grammar the way the real notifiers do."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from tests import sibling_schemas as ss
from tests.fakes import CANONICAL, Notifier, last_json, run

Make = Callable[..., Notifier]
TOOLS = ["slack-notice", "ha-notice"]


@pytest.mark.parametrize("tool", TOOLS)
def test_canonical_call_sends_and_the_result_matches_the_pinned_send_schema(
    make_notifier: Make, home: Path, tool: str
) -> None:
    n = make_notifier(tool, tool=tool)
    result = run(n, home, '{"title": "T", "message": "M", "level": "warn"}')
    assert result.returncode == 0
    body = last_json(result)
    ss.validate(body, tool, "cli/send.json")
    assert (body["outcome"], body["level"], body["tool"]) == ("sent", "warn", tool)
    assert body["dedupe_key"] is None
    assert result.stderr == b""


@pytest.mark.parametrize("tool", TOOLS)
def test_dedupe_key_in_equals_form_is_echoed(make_notifier: Make, home: Path, tool: str) -> None:
    n = make_notifier(tool, tool=tool)
    result = run(n, home, args=[*CANONICAL, "--dedupe-key=abc123"])
    assert result.returncode == 0
    body = last_json(result)
    ss.validate(body, tool, "cli/send.json")
    assert body["dedupe_key"] == "abc123"
    if tool == "ha-notice":
        assert body["tag_set"] is True


def test_the_call_is_recorded_with_argv_stdin_cwd_and_the_private_environment(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier()
    run(n, home, '{"message": "héllo"}')
    invoke = n.events("invoke")[0]
    assert invoke["argv"] == CANONICAL
    assert invoke["cwd"] == "/"
    assert invoke["env"]["HOME"] == str(home)
    assert invoke["pgid"] == invoke["pid"] == invoke["sid"]  # own session, as the router makes it
    assert sorted(set(invoke["open_fds"]) - {0, 1, 2}) == []
    assert n.events("stdin")[0]["text"] == '{"message": "héllo"}'
    assert n.events("exit")[0]["code"] == 0


def test_global_flags_may_precede_the_subcommand(make_notifier: Make, home: Path) -> None:
    n = make_notifier()
    result = run(
        n, home, args=["--json", "--actor=auto:x", "send", "--stdin-json", "--dedupe-key", "k"]
    )
    assert result.returncode == 0
    assert last_json(result)["dedupe_key"] == "k"


def test_version_and_help_exit_zero(make_notifier: Make, home: Path) -> None:
    n = make_notifier("ha-notice", tool="ha-notice")
    assert run(n, home, args=["--version"]).stdout.startswith(b"ha-notice ")
    assert run(n, home, args=["send", "-h"]).returncode == 0


USAGE_CASES: list[tuple[str, list[str]]] = [
    ("abbreviated flag", ["send", "--stdin", "--json"]),
    ("unknown flag", ["send", "--stdin-json", "--json", "--bogus"]),
    ("unknown subcommand", ["frobnicate", "--json"]),
    ("no subcommand", ["--json"]),
    ("second positional", ["send", "extra", "--stdin-json", "--json"]),
    ("bare double dash", ["send", "--stdin-json", "--json", "--"]),
    ("short option", ["send", "--stdin-json", "--json", "-x"]),
    ("json with a value", ["send", "--stdin-json", "--json=1"]),
    ("boolean with a value", ["send", "--stdin-json=1", "--json"]),
    ("bad actor", ["send", "--stdin-json", "--json", "--actor", "root"]),
    ("actor over 64", ["send", "--stdin-json", "--json", "--actor", "auto:" + "a" * 65]),
    ("empty actor", ["send", "--stdin-json", "--json", "--actor="]),
    ("empty dedupe key", ["send", "--stdin-json", "--json", "--dedupe-key="]),
    ("dedupe key over 1024", ["send", "--stdin-json", "--json", "--dedupe-key=" + "k" * 1025]),
    ("missing value", ["send", "--stdin-json", "--json", "--actor"]),
    ("value starting with a dash needs the equals form", ["send", "--json", "--dedupe-key", "-x"]),
    ("value flag twice", ["send", "--stdin-json", "--json", "--actor=auto:a", "--actor=auto:b"]),
    (
        "value flag on each side of the subcommand",
        ["--actor=auto:a", "send", "--stdin-json", "--json", "--actor=auto:b"],
    ),
    ("verbose with quiet", ["send", "--stdin-json", "--json", "--verbose", "--quiet"]),
    ("stdin-json with a file path", ["send", "--stdin-json", "--file", "x", "--json"]),
    ("json source with a text flag", ["send", "--stdin-json", "--title", "t", "--json"]),
    ("text flag without a message", ["send", "--title", "t", "--json"]),
    ("probe outside validate", ["send", "--stdin-json", "--json", "--probe"]),
]


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(("label", "args"), USAGE_CASES, ids=[c[0] for c in USAGE_CASES])
def test_usage_errors_exit_2_with_the_error_envelope(
    make_notifier: Make, home: Path, tool: str, label: str, args: list[str]
) -> None:
    n = make_notifier(tool, tool=tool)
    result = run(n, home, args=args)
    assert result.returncode == 2, label
    body = last_json(result)
    ss.validate(body, tool, "cli/error.json")
    error = body["error"]
    assert error["code"] == "usage"
    assert (error["reason"], error["retryable"], error["unknown_outcome"]) == (None, None, None)
    assert result.stderr.startswith(b"usage: ") and result.stderr.count(b"\n") == 1


def test_a_usage_error_without_json_prints_one_stderr_line_and_no_stdout(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier()
    result = run(n, home, args=["send", "--bogus"])
    assert (result.returncode, result.stdout) == (2, b"")
    assert result.stderr.count(b"\n") == 1


def test_a_usage_error_is_found_before_stdin_is_read(make_notifier: Make, home: Path) -> None:
    n = make_notifier()
    run(n, home, args=["send", "--bogus", "--json"])
    assert n.events("stdin") == []


def test_other_subcommands_are_not_modeled_and_say_so(make_notifier: Make, home: Path) -> None:
    n = make_notifier("ha-notice", tool="ha-notice")
    result = run(n, home, args=["clear", "--json", "--dedupe-key=k"])
    assert result.returncode == 1
    assert last_json(result)["error"]["code"] == "fake_unsupported"


def test_disabled_prints_the_outcome_exits_22_and_reads_no_input(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(disabled=True)
    result = run(n, home)
    assert (result.returncode, last_json(result)) == (22, {"outcome": "disabled"})
    ss.validate(last_json(result), "slack-notice", "cli/send.json")
    assert n.events("stdin") == []


def refused(n: Notifier, home: Path, payload: bytes | str) -> tuple[int, dict[str, object]]:
    result = run(n, home, payload)
    return result.returncode, json.loads(result.stdout) if result.stdout else {}


PAYLOAD_CASES: list[tuple[str, bytes | str, str, str | None]] = [
    ("unknown field", '{"message": "m", "zeta": "1", "alpha": "2"}', "invalid_payload", "/alpha"),
    ("odd field name", '{"message": "m", "a b": "1"}', "invalid_payload", "/?"),
    ("wrong type", '{"message": ["x"]}', "invalid_payload", "/message"),
    ("null is a wrong type", '{"message": "m", "title": null}', "invalid_payload", "/title"),
    ("bad level", '{"message": "m", "level": "loud"}', "invalid_payload", "/level"),
    ("duplicate key", '{"message": "a", "message": "b"}', "invalid_payload", ""),
    ("NaN", '{"message": NaN}', "invalid_payload", ""),
    ("not an object", '["m"]', "invalid_payload", ""),
    ("not json", "{", "invalid_payload", ""),
    ("empty input", b"", "invalid_payload", ""),
    ("bom", b'\xef\xbb\xbf{"message": "m"}', "invalid_payload", ""),
    ("invalid utf-8", b'{"message": "\xff"}', "invalid_payload", ""),
    ("lone surrogate", '{"message": "\\ud800"}', "invalid_payload", "/message"),
    (
        "title over 100000",
        json.dumps({"message": "m", "title": "t" * 100001}),
        "invalid_payload",
        "/title",
    ),
    ("over one MiB", b'{"message": "' + b"a" * 1_048_576 + b'"}', "invalid_payload", ""),
    ("missing message", '{"title": "t"}', "refused", None),
    ("blank message", '{"message": " \\t\\n"}', "refused", None),
    ("link scheme", '{"message": "m", "link": "javascript:alert(1)"}', "refused", None),
    ("link with a space", '{"message": "m", "link": "https://a b"}', "refused", None),
    ("link without a host", '{"message": "m", "link": "https:///x"}', "refused", None),
    ("link with userinfo", '{"message": "m", "link": "https://u@h/x"}', "refused", None),
]


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    ("label", "payload", "code", "pointer"), PAYLOAD_CASES, ids=[c[0] for c in PAYLOAD_CASES]
)
def test_a_refused_payload_is_exit_20_with_a_reason_and_pointer(
    make_notifier: Make,
    home: Path,
    tool: str,
    label: str,
    payload: bytes | str,
    code: str,
    pointer: str | None,
) -> None:
    n = make_notifier(tool, tool=tool)
    status, body = refused(n, home, payload)
    assert status == 20, label
    ss.validate(body, tool, "cli/error.json")
    error = body["error"]
    assert isinstance(error, dict)
    assert error["code"] == "refused"
    assert error["reason"] in {"invalid_payload", "empty_message", "link_invalid", "link_scheme"}
    assert (error["retryable"], error["unknown_outcome"]) == (None, None)
    if pointer is not None:
        assert error["reason"] == "invalid_payload"
        assert error["details"] == {"pointer": pointer}


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ('{"title": "t"}', "empty_message"),
        ('{"message": "\\u200b"}', "empty_message"),
        ('{"message": "m", "link": "javascript:x"}', "link_scheme"),
        ('{"message": "m", "link": "https://a b"}', "link_invalid"),
    ],
)
def test_refusal_reasons(make_notifier: Make, home: Path, payload: str, reason: str) -> None:
    _, body = refused(make_notifier(), home, payload)
    error = body["error"]
    assert isinstance(error, dict)
    assert error["reason"] == reason


def test_a_valid_link_and_unicode_text_are_accepted(make_notifier: Make, home: Path) -> None:
    n = make_notifier()
    payload = json.dumps(
        {"title": "ünï", "message": "👩‍💻 ok", "link": "https://example.com:8443/a?b=c#d"},
        ensure_ascii=False,
    )
    assert run(n, home, payload).returncode == 0


def test_command_keywords_are_refused_by_the_ha_fake_only(make_notifier: Make, home: Path) -> None:
    ha = make_notifier("ha-notice", tool="ha-notice")
    for text in ("clear_notification", "TTS", "Command_Screen_On", "ｋｉｏｓｋ_x"):
        _, body = refused(ha, home, json.dumps({"message": text}))
        error = body["error"]
        assert isinstance(error, dict)
        assert (error["reason"], error["details"]) == ("command_keyword", None), text
    assert run(ha, home, json.dumps({"message": "update_deps finished"})).returncode == 0
    slack = make_notifier("slack-notice")
    assert run(slack, home, json.dumps({"message": "clear_notification"})).returncode == 0


def test_input_validation_can_be_switched_off(make_notifier: Make, home: Path) -> None:
    n = make_notifier(validate_input=False)
    assert run(n, home, b"not json at all").returncode == 0


def test_a_number_is_a_parse_failure_for_slack_and_a_type_error_for_ha(
    make_notifier: Make, home: Path
) -> None:
    slack = make_notifier("slack-notice")
    ha = make_notifier("ha-notice", tool="ha-notice")
    assert refused(slack, home, '{"message": 5}')[1]["error"]["details"] == {"pointer": ""}  # type: ignore[index]
    assert refused(ha, home, '{"message": 5}')[1]["error"]["details"] == {"pointer": "/message"}  # type: ignore[index]
    assert refused(ha, home, '{"message": 1' + "0" * 5000 + "}")[1]["error"]["details"] == {  # type: ignore[index]
        "pointer": ""
    }
