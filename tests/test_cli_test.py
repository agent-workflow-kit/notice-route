"""The ``test`` command and ``send --dry-run`` (#13): output against schemas/cli/test.json, text and
``--json``, exit codes, ``--config``, the order of the input checks, and the promise that nothing
is written, locked, spawned or sent."""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from notice_route import cli
from notice_route.cli import main
from notice_route.gateway import SYSTEM_TZ_DIRS, FakeClock, Gateway
from tests.sibling_schemas import hook_examples
from tests.test_cli_rules_validate import (
    CANARY,
    NOW,
    make_exe,
    schema_errors,
    write_config,
)

ROOT = Path(__file__).resolve().parent.parent

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database")

HOOK = {
    "tool": "agent-spawn",
    "hook": "post-state-change",
    "event_id": 17,
    "object_id": 7,
    "state": "reported",
}


@pytest.fixture(autouse=True)
def private_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.delenv("NOTICE_ROUTE_HOOK", raising=False)


class Invoker:
    def __init__(self, home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.home, self.tmp_path, self.capsys = home, tmp_path, capsys
        self.fds: list[int] = []

    def gateway(self, stdin: bytes | None, env: dict[str, str] | None = None) -> Gateway:
        read, write = os.pipe()
        self.fds.append(read)
        if stdin is not None:
            os.write(write, stdin)
        os.close(write)
        return Gateway(
            passwd_home=lambda: str(self.home),
            clock=FakeClock(wall=NOW),
            exec_check_from=str(self.tmp_path),
            stdin_fd=read,
            environ=lambda: dict(env or {}),
        )

    def run(
        self, *argv: str, stdin: bytes | None = None, env: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        code = main(list(argv), self.gateway(stdin, env))
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err

    def run_json(
        self, *argv: str, stdin: bytes | None = None, env: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]:
        code, out, _ = self.run(*argv, "--json", stdin=stdin, env=env)
        assert out.endswith("\n") and out.count("\n") == 1
        return code, json.loads(out)


@pytest.fixture
def inv(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Iterator[Invoker]:
    invoker = Invoker(home, tmp_path, capsys)
    yield invoker
    for fd in invoker.fds:
        os.close(fd)


def config_data(tmp_path: Path) -> dict[str, Any]:
    exe = make_exe(tmp_path)
    return {
        "destinations": {
            "phone": {"kind": "ha-notice", "cmd": [exe], "floor": "warn"},
            "chat": {"kind": "slack-notice", "cmd": [exe]},
            "off": {"kind": "generic", "cmd": [exe], "enabled": False},
        },
        "links": {"hosts": {"git.example.com": "github"}, "tickets": {}},
        "rules": [
            {
                "name": "drop-launch",
                "match": {"source": "agent-spawn", "hook": "post-launch"},
                "drop": True,
            },
            {
                "name": "done",
                "match": {"source": "agent-spawn", "hook": "post-state-change"},
                "notify": ["phone", "chat"],
                "level": "info",
                "title": "Done {{object_id}}",
                "message": "Agent {{object_id}} is {{state}} {{nothing}} {{note}}",
                "link": "{{pr_url}}",
            },
            {
                "name": "only-off",
                "match": {"source": "offsrc"},
                "notify": ["off"],
                "level": "error",
                "message": "off {{object_id}}",
            },
            {"name": "direct", "match": {"level_min": "info"}, "notify": ["chat", "phone"]},
        ],
    }


@pytest.fixture
def configured(home: Path, tmp_path: Path) -> dict[str, Any]:
    data = config_data(tmp_path)
    write_config(home, data)
    return data


def hook(**extra: Any) -> bytes:
    return json.dumps({**HOOK, **extra}).encode()


# --- golden outputs against the schema ---------------------------------------------------------


def test_hook_payload_json_golden(inv: Invoker, configured: dict[str, Any]) -> None:
    code, doc = inv.run_json("test", stdin=hook())
    assert code == 0
    assert schema_errors(doc, "test.json") == []
    title, message = "Done 7", "Agent 7 is reported"
    key = "\0".join(["agent-spawn", "post-state-change", "info", title, message, ""])
    assert doc == {
        "shape": "hook",
        "source": "agent-spawn",
        "hook": "post-state-change",
        "rule": "done",
        "reason": None,
        "title": title,
        "message": message,
        "link": None,
        "missing": ["nothing", "note"],
        "excluded": [],
        "text_excluded": False,
        "labels_ignored": 0,
        "level": "info",
        "dedupe": hashlib.sha256(key.encode()).hexdigest(),
        "destinations": [
            {"name": "phone", "decision": "drop", "reason": "below_floor", "deliver_after": None},
            {"name": "chat", "decision": "send", "reason": None, "deliver_after": None},
        ],
    }


def test_pr_link_is_shown(inv: Invoker, configured: dict[str, Any]) -> None:
    code, doc = inv.run_json("test", stdin=hook(pr="git.example.com/o/r#5"))
    assert code == 0
    assert schema_errors(doc, "test.json") == []
    assert doc["link"] == "https://git.example.com/o/r/pull/5"


def test_excluded_text_is_listed(inv: Invoker, configured: dict[str, Any]) -> None:
    code, doc = inv.run_json("test", stdin=hook(note="two words"))
    assert code == 0
    assert schema_errors(doc, "test.json") == []
    assert doc["missing"] == ["nothing"]
    assert doc["excluded"] == ["note"] and doc["text_excluded"] is True


def test_notice_pass_through_shows_its_own_title_and_message(
    inv: Invoker, configured: dict[str, Any]
) -> None:
    stdin = b'{"title": "Own title", "message": "own message", "level": "warn"}'
    code, doc = inv.run_json("test", stdin=stdin)
    assert code == 0
    assert schema_errors(doc, "test.json") == []
    assert (doc["shape"], doc["source"], doc["hook"], doc["rule"]) == (
        "notice",
        "direct",
        None,
        "direct",
    )
    assert (doc["title"], doc["message"], doc["level"]) == ("Own title", "own message", "warn")
    assert [d["decision"] for d in doc["destinations"]] == ["send", "send"]


def test_notice_own_link_is_never_the_link(inv: Invoker, configured: dict[str, Any]) -> None:
    stdin = b'{"message": "m", "link": "https://example.com/x"}'
    code, doc = inv.run_json("test", stdin=stdin)
    assert code == 0 and doc["link"] is None


def test_drop_rule_golden(inv: Invoker, configured: dict[str, Any]) -> None:
    code, doc = inv.run_json("test", stdin=hook(hook="post-launch"))
    assert code == 0
    assert schema_errors(doc, "test.json") == []
    assert doc == {
        "shape": "hook",
        "source": "agent-spawn",
        "hook": "post-launch",
        "rule": "drop-launch",
        "reason": "rule_drop",
        "title": None,
        "message": None,
        "link": None,
        "missing": [],
        "excluded": [],
        "text_excluded": False,
        "labels_ignored": 0,
        "level": None,
        "dedupe": None,
        "destinations": [],
    }


def test_no_rule_exits_22(inv: Invoker, configured: dict[str, Any]) -> None:
    code, doc = inv.run_json("test", stdin=hook(tool="other"))
    assert code == 22
    assert schema_errors(doc, "test.json") == []
    assert (doc["reason"], doc["rule"], doc["level"], doc["destinations"]) == (
        "no_rule",
        None,
        None,
        [],
    )
    assert doc["title"] is None and doc["message"] is None and doc["dedupe"] is None


def test_all_disabled_exits_22_and_lists_the_destinations(
    inv: Invoker, configured: dict[str, Any]
) -> None:
    code, doc = inv.run_json("test", stdin=hook(tool="offsrc"))
    assert code == 22
    assert schema_errors(doc, "test.json") == []
    assert doc["reason"] == "all_disabled" and doc["rule"] == "only-off"
    assert doc["destinations"] == [
        {"name": "off", "decision": "disabled", "reason": None, "deliver_after": None}
    ]


def test_disabled_router_exits_22(inv: Invoker, home: Path, tmp_path: Path) -> None:
    write_config(home, {**config_data(tmp_path), "enabled": False})
    code, doc = inv.run_json("test", stdin=hook())
    assert code == 22
    assert schema_errors(doc, "test.json") == []
    assert doc["reason"] == "disabled" and doc["rule"] is None and doc["destinations"] == []
    assert (doc["shape"], doc["source"]) == ("hook", "agent-spawn")


def test_no_config_is_unconfigured_exit_22(inv: Invoker) -> None:
    code, out, err = inv.run("test", "--json", stdin=hook())
    assert code == 22
    doc = json.loads(out)
    assert schema_errors(doc, "test.json") == []
    assert doc["reason"] == "unconfigured" and doc["rule"] is None
    assert err == "notice-route: no config\n"


def test_unconfigured_quiet_drops_the_stderr_line(inv: Invoker) -> None:
    code, out, err = inv.run("test", "--quiet", stdin=hook())
    assert (code, err) == (22, "")
    assert "unconfigured" in out


def test_no_destinations_is_unconfigured(inv: Invoker, home: Path) -> None:
    write_config(home, {"rules": []})
    code, doc = inv.run_json("test", stdin=hook())
    assert code == 22 and doc["reason"] == "unconfigured"
    assert schema_errors(doc, "test.json") == []


def test_no_destinations_prints_the_advisory_line_unless_quiet(inv: Invoker, home: Path) -> None:
    write_config(home, {"rules": []})
    code, _, err = inv.run("test", stdin=hook())
    assert (code, err) == (22, "notice-route: no config\n")
    code, _, err = inv.run("test", "--quiet", stdin=hook())
    assert (code, err) == (22, "")


@pytest.mark.parametrize(
    ("stdin", "level", "decisions"),
    [
        (b'{"message": "m", "level": "info"}', "info", ["drop", "send"]),
        (b'{"message": "m", "level": "warn"}', "warn", ["send", "send"]),
        (b'{"message": "m"}', "info", ["drop", "send"]),
    ],
)
def test_floor_decides_per_destination(
    inv: Invoker,
    configured: dict[str, Any],
    stdin: bytes,
    level: str,
    decisions: list[str],
) -> None:
    code, doc = inv.run_json("test", stdin=stdin)
    assert code == 0 and doc["level"] == level
    assert schema_errors(doc, "test.json") == []
    by_name = {d["name"]: d["decision"] for d in doc["destinations"]}
    assert by_name == {"chat": decisions[1], "phone": decisions[0]}


def test_disabled_destination_among_enabled(inv: Invoker, home: Path, tmp_path: Path) -> None:
    data = config_data(tmp_path)
    data["rules"] = [
        {
            "name": "r",
            "match": {"source": "s"},
            "notify": ["off", "chat"],
            "level": "info",
            "message": "x",
        }
    ]
    write_config(home, data)
    code, doc = inv.run_json("test", stdin=hook(tool="s"))
    assert code == 0
    assert schema_errors(doc, "test.json") == []
    assert [(d["name"], d["decision"]) for d in doc["destinations"]] == [
        ("off", "disabled"),
        ("chat", "send"),
    ]


def test_disabled_destination_above_the_level_is_disabled_not_dropped(
    inv: Invoker, home: Path, tmp_path: Path
) -> None:
    data = config_data(tmp_path)
    data["destinations"]["off"]["floor"] = "warn"
    data["rules"] = [
        {
            "name": "r",
            "match": {"source": "s"},
            "notify": ["off", "chat"],
            "level": "info",
            "message": "x",
        }
    ]
    write_config(home, data)
    code, doc = inv.run_json("test", stdin=hook(tool="s"))
    assert code == 0
    assert schema_errors(doc, "test.json") == []
    assert doc["destinations"][0] == {
        "name": "off",
        "decision": "disabled",
        "reason": None,
        "deliver_after": None,
    }


def independent_digest(*parts: str) -> str:
    """The repeat key of docs/interface.md (Dedup), recomputed without the code under test."""
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("stdin", "source", "hook_name", "level", "link"),
    [
        (hook(pr="git.example.com/o/r#5"), "agent-spawn", "post-state-change", "info", "pr"),
        (hook(), "agent-spawn", "post-state-change", "info", ""),
        (
            json.dumps({"title": "Tïtle ✓", "message": "héllo 👾", "level": "warn"}).encode(),
            "direct",
            "",
            "warn",
            "",
        ),
        (b'{"title": "T", "message": "m", "level": "info"}', "direct", "", "info", ""),
        (b'{"title": "T", "message": "m", "level": "action"}', "direct", "", "action", ""),
        (b'{"title": "T", "message": "m", "level": "error"}', "direct", "", "error", ""),
    ],
)
def test_dedupe_digest_matches_an_independent_recomputation(
    inv: Invoker,
    configured: dict[str, Any],
    stdin: bytes,
    source: str,
    hook_name: str,
    level: str,
    link: str,
) -> None:
    code, doc = inv.run_json("test", stdin=stdin)
    assert code == 0
    assert schema_errors(doc, "test.json") == []
    expected_link = "https://git.example.com/o/r/pull/5" if link == "pr" else ""
    assert (doc["level"], doc["link"] or "") == (level, expected_link)
    assert doc["dedupe"] == independent_digest(
        source, hook_name, level, doc["title"], doc["message"], expected_link
    )


# --- input -------------------------------------------------------------------------------------


def test_default_input_is_stdin(inv: Invoker, configured: dict[str, Any]) -> None:
    assert inv.run_json("test", stdin=hook())[0] == 0
    assert inv.run_json("test", "--stdin-json", stdin=hook())[0] == 0
    assert inv.run_json("test", "--file", "-", stdin=hook())[0] == 0


@pytest.mark.parametrize("flags", [("--source", "tool-x"), ("--label", "k=v")])
def test_a_source_or_label_flag_alone_still_reads_stdin(
    inv: Invoker, configured: dict[str, Any], flags: tuple[str, ...]
) -> None:
    code, doc = inv.run_json("test", *flags, stdin=b'{"message": "from stdin"}')
    assert code == 0 and doc["message"] == "from stdin"
    assert doc["source"] == ("tool-x" if flags[0] == "--source" else "direct")


@pytest.mark.parametrize(
    "flags", [("--file", "-", "--message", "m"), ("--stdin-json", "--message", "m")]
)
def test_stdin_input_cannot_be_combined_with_message_flags(
    inv: Invoker, configured: dict[str, Any], flags: tuple[str, ...]
) -> None:
    code, doc = inv.run_json("test", *flags, stdin=hook())
    assert code == 2 and doc["error"]["code"] == "usage"
    assert schema_errors(doc, "error.json") == []


def test_flag_input(inv: Invoker, configured: dict[str, Any]) -> None:
    code, doc = inv.run_json(
        "test", "--title", "T", "--message", "M", "--source", "tool-x", "--label", "k=v"
    )
    assert code == 0
    assert (doc["source"], doc["title"], doc["message"]) == ("tool-x", "T", "M")


def test_file_input(inv: Invoker, configured: dict[str, Any], tmp_path: Path) -> None:
    path = tmp_path / "in.json"
    path.write_bytes(hook())
    code, doc = inv.run_json("test", "--file", str(path))
    assert code == 0 and doc["rule"] == "done"


def test_stdin_terminal_is_usage(
    inv: Invoker, configured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Gateway, "stdin_is_tty", lambda self: True)
    code, doc = inv.run_json("test")
    assert code == 2 and doc["error"]["code"] == "usage"
    assert schema_errors(doc, "error.json") == []


def test_title_without_message_is_usage(inv: Invoker, configured: dict[str, Any]) -> None:
    code, doc = inv.run_json("test", "--title", "T")
    assert code == 2 and doc["error"]["code"] == "usage"


@pytest.mark.parametrize(
    ("stdin", "reason", "pointer"),
    [
        (b"not json", "payload_invalid", ""),
        (b'{"tool": "x", "hook": "Bad"}', "payload_invalid", "/hook"),
        (b'{"unknown": 1}', "payload_unrecognized", None),
    ],
)
def test_refusals_exit_20(
    inv: Invoker,
    configured: dict[str, Any],
    stdin: bytes,
    reason: str,
    pointer: str | None,
) -> None:
    code, out, err = inv.run("test", "--json", stdin=stdin)
    doc = json.loads(out)
    assert code == 20
    assert schema_errors(doc, "error.json") == []
    assert doc["error"]["code"] == "refused" and doc["error"]["reason"] == reason
    assert (doc["error"]["details"] or {}).get("pointer") == pointer
    assert err.startswith("notice-route: refused: ") and err.count("\n") == 1


def test_hook_recursion_is_refused_before_reading(inv: Invoker, configured: dict[str, Any]) -> None:
    code, doc = inv.run_json("test", stdin=hook(), env={"NOTICE_ROUTE_HOOK": "1"})
    assert code == 20 and doc["error"]["reason"] == "hook_recursion"


def test_input_is_checked_before_the_config(inv: Invoker, home: Path) -> None:
    write_config(home, {"bogus_key": 1})
    code, doc = inv.run_json("test", stdin=b"not json")
    assert code == 20 and doc["error"]["code"] == "refused"
    code, doc = inv.run_json("test", stdin=hook())
    assert code == 2 and doc["error"]["code"] == "config_invalid"


def test_refusal_text_never_carries_input(inv: Invoker, configured: dict[str, Any]) -> None:
    code, out, err = inv.run("test", "--json", stdin=json.dumps({"x": CANARY}).encode())
    assert code == 20 and CANARY not in out + err
    code, out, err = inv.run("test", "--json", "--source", CANARY, stdin=hook())
    assert code == 20 and CANARY not in out + err


# --- config ------------------------------------------------------------------------------------


def test_config_flag_replaces_the_config(
    inv: Invoker, home: Path, tmp_path: Path, configured: dict[str, Any]
) -> None:
    other = config_data(tmp_path)
    other["rules"] = [{"name": "alt", "match": {"source": "agent-spawn"}, "drop": True}]
    path = write_config(home, other, name="other.yaml")
    code, doc = inv.run_json("test", "--config", str(path), stdin=hook())
    assert code == 0 and doc["rule"] == "alt"


def test_missing_config_file_is_config_invalid(inv: Invoker, tmp_path: Path) -> None:
    code, doc = inv.run_json("test", "--config", str(tmp_path / "nope.yaml"), stdin=hook())
    assert code == 2
    assert doc["error"]["code"] == "config_invalid"
    assert doc["error"]["details"] == {"key": "--config"}
    assert schema_errors(doc, "error.json") == []


def test_send_dry_run_refuses_config(inv: Invoker, configured: dict[str, Any]) -> None:
    code, out, err = inv.run(
        "send", "--dry-run", "--stdin-json", "--config", "/x/y.yaml", "--json", stdin=hook()
    )
    doc = json.loads(out)
    assert code == 2 and doc["error"]["code"] == "usage"
    assert schema_errors(doc, "error.json") == []
    assert "/x/y.yaml" not in out + err


def test_send_refuses_config_with_its_own_fixed_message(
    inv: Invoker, configured: dict[str, Any]
) -> None:
    code, out, err = inv.run("send", "--config", "/x/y.yaml", "--json")
    doc = json.loads(out)
    assert code == 2 and doc["error"]["code"] == "usage"
    assert doc["error"]["message"] == "--config is not accepted by this command"
    assert schema_errors(doc, "error.json") == []
    assert err == "notice-route: usage: --config is not accepted by this command\n"
    assert "/x/y.yaml" not in out + err


# --- usage errors of the command line ------------------------------------------------------------

USAGE_LINE = "notice-route: usage: the command line cannot be used\n"
HOSTILE = "\x1b[31mX\x07\x85\u0085\r\u202e"


@pytest.mark.parametrize(
    "argv",
    [
        ("test", "--bogus" + HOSTILE),
        ("test", "positional" + HOSTILE),
        ("bogus" + HOSTILE,),
        ("send", "--dry-run", "--" + HOSTILE),
        ("--bogus" + HOSTILE, "test"),
        ("test", "--source"),
        ("test", "--label"),
    ],
)
def test_usage_errors_print_fixed_text_only(
    capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]
) -> None:
    assert main(list(argv)) == 2
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", USAGE_LINE)


@pytest.mark.parametrize(
    "argv",
    [
        ("test", "--bogus" + HOSTILE),
        ("bogus" + HOSTILE,),
        ("test", "extra" + HOSTILE),
        ("--json", "--bogus" + HOSTILE),
    ],
)
def test_usage_errors_under_json_print_the_envelope(
    capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]
) -> None:
    assert main([*argv, "--json"]) == 2
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert captured.out.count("\n") == 1 and captured.out.isascii()
    assert schema_errors(doc, "error.json") == []
    assert doc["error"]["code"] == "usage"
    assert doc["error"]["message"] == "the command line cannot be used"
    assert captured.err == USAGE_LINE


def test_verbose_with_quiet_is_a_fixed_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rules", "--verbose", "--quiet"]) == 2
    assert (
        capsys.readouterr().err == "notice-route: usage: --verbose and --quiet cannot be combined\n"
    )


def test_config_error_carries_no_config_value(inv: Invoker, home: Path, tmp_path: Path) -> None:
    data = config_data(tmp_path)
    data["destinations"]["chat"]["kind"] = CANARY
    write_config(home, data)
    code, out, err = inv.run("test", "--json", stdin=hook())
    assert code == 2 and CANARY not in out + err
    assert json.loads(out)["error"]["code"] == "config_invalid"


# --- send --dry-run ----------------------------------------------------------------------------


def test_send_dry_run_equals_test(inv: Invoker, configured: dict[str, Any]) -> None:
    a = inv.run_json("test", stdin=hook())
    b = inv.run_json("send", "--dry-run", "--stdin-json", stdin=hook())
    assert a == b and b[0] == 0
    assert (
        inv.run("test", stdin=hook())[1]
        == inv.run("send", "--dry-run", "--stdin-json", stdin=hook())[1]
    )


def test_send_dry_run_exit_22_and_input_flags(inv: Invoker, configured: dict[str, Any]) -> None:
    code, doc = inv.run_json("send", "--dry-run", "--message", "m", "--source", "zzz")
    assert code == 0 and doc["rule"] == "direct"
    code, doc = inv.run_json("send", "--dry-run", "--stdin-json", stdin=hook(tool="other"))
    assert code == 22 and doc["reason"] == "no_rule"


def test_send_dry_run_has_no_default_input(inv: Invoker, configured: dict[str, Any]) -> None:
    code, doc = inv.run_json("send", "--dry-run")
    assert code == 2 and doc["error"]["code"] == "usage"


# --- text output -------------------------------------------------------------------------------


def test_text_output_golden(inv: Invoker, configured: dict[str, Any]) -> None:
    pr = hook(pr="git.example.com/o/r#5")
    key = "\0".join(
        [
            "agent-spawn",
            "post-state-change",
            "info",
            "Done 7",
            "Agent 7 is reported",
            "https://git.example.com/o/r/pull/5",
        ]
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    code, out, err = inv.run("test", stdin=pr)
    assert code == 0 and err == ""
    assert out.splitlines() == [
        "shape: hook",
        "source: agent-spawn",
        "hook: post-state-change",
        "rule: done",
        "reason: none",
        "level: info",
        "title: Done 7",
        "message: Agent 7 is reported",
        "link: https://git.example.com/o/r/pull/5",
        "missing: nothing,note",
        "excluded: none",
        "text_excluded: false",
        "labels_ignored: 0",
        "dedupe: " + digest,
        "destination phone: drop (below_floor)",
        "destination chat: send",
    ]


def test_text_output_no_rule(inv: Invoker, configured: dict[str, Any]) -> None:
    code, out, _ = inv.run("test", stdin=hook(tool="other"))
    assert code == 22
    assert "rule: none" in out and "reason: no_rule" in out and "hook: post-state-change" in out


def test_hostile_text_never_reaches_the_terminal_raw(
    inv: Invoker, configured: dict[str, Any]
) -> None:
    hostile = "a\x1b[31mred\x07‮\u0085\r\nsecond 👾 ü"
    stdin = json.dumps({"title": hostile, "message": hostile, "level": "warn"}).encode()
    code, out, err = inv.run("test", stdin=stdin)
    assert code == 0
    for text in (out, err):
        assert text.isascii()
        assert not any(c in text for c in "\x1b\x07\r\x85")
        assert all(c.isprintable() or c == "\n" for c in text)
    code, out, _ = inv.run("test", "--json", stdin=stdin)
    assert "\x1b" not in out and out.isascii()


def test_hostile_flags_never_reach_the_terminal_raw(
    inv: Invoker, configured: dict[str, Any]
) -> None:
    code, out, err = inv.run("test", "--message", "x\x1b]0;pwn\x07y")
    assert code == 0
    assert "\x1b" not in out + err and "\x07" not in out + err


def test_escaping_is_unambiguous_a_backslash_is_doubled(
    inv: Invoker, configured: dict[str, Any]
) -> None:
    assert cli._safe("a\x01b") == "a\\x01b"  # pyright: ignore[reportPrivateUsage]
    assert cli._safe("a\\x01b") == "a\\\\x01b"  # pyright: ignore[reportPrivateUsage]
    assert cli._safe("\\\u00e9") == "\\\\\\xe9"  # pyright: ignore[reportPrivateUsage]
    code, out, _ = inv.run("test", "--message", "a\\b")
    assert code == 0 and "message: a\\\\b\n" in out


# --- the example config ------------------------------------------------------------------------


def test_example_config_renders_every_hook_example_without_gaps(inv: Invoker, home: Path) -> None:
    directory = home / ".config" / "notice-route"
    (directory / "config.d").mkdir(parents=True)
    directory.chmod(0o700)
    (directory / "config.d").chmod(0o700)
    for source, target in (
        (ROOT / "config.example.yaml", directory / "config.yaml"),
        (ROOT / "examples" / "awkit-wiring.yaml", directory / "config.d" / "wiring.yaml"),
    ):
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    examples = hook_examples("agent-spawn")
    expected_rules = {
        "post-escalate": "agent-escalated",
        "post-state-change-agent": "agent-done",
        "post-state-change-session": "agent-done",
    }
    assert set(examples) == set(expected_rules)
    for name, payload in examples.items():
        hook_name = name.removesuffix("-agent").removesuffix("-session")
        merged = {**payload, "hook": hook_name}
        if hook_name == "post-state-change":
            merged["state"] = "reported"  # the rule agent-done matches on state: reported
        code, doc = inv.run_json("test", stdin=json.dumps(merged).encode())
        assert code == 0, name
        assert schema_errors(doc, "test.json") == [], name
        assert doc["rule"] == expected_rules[name], name
        assert doc["missing"] == [] and doc["excluded"] == [], name


# --- the schema's own rules -----------------------------------------------------------------------


def test_schema_rejects_a_rule_drop_document_with_a_level(
    inv: Invoker, configured: dict[str, Any]
) -> None:
    _, doc = inv.run_json("test", stdin=hook(hook="post-launch"))
    assert schema_errors(doc, "test.json") == []
    assert schema_errors({**doc, "level": "info"}, "test.json") != []


def test_schema_rejects_an_all_disabled_document_without_a_level(
    inv: Invoker, configured: dict[str, Any]
) -> None:
    _, doc = inv.run_json("test", stdin=hook(tool="offsrc"))
    assert schema_errors(doc, "test.json") == []
    assert schema_errors({**doc, "level": None}, "test.json") != []


# --- no side effects ---------------------------------------------------------------------------


def snapshot(root: Path) -> list[tuple[str, int, int, int]]:
    out: list[tuple[str, int, int, int]] = []
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        out.append((str(path.relative_to(root)), st.st_mode, st.st_size, st.st_mtime_ns))
    return out


def make_state(home: Path, kind: str) -> Path:
    """The state directory with a garbage, a valid or no ``state.db``."""
    state = home / ".local" / "state" / "notice-route"
    state.mkdir(parents=True)
    state.chmod(0o700)
    db = state / "state.db"
    if kind == "garbage":
        db.write_bytes(b"not a database")
    elif kind == "valid":
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE sends (id INTEGER PRIMARY KEY, digest TEXT, at REAL)")
        conn.executemany("INSERT INTO sends (digest, at) VALUES (?, ?)", [("a", 1.0), ("b", 2.0)])
        conn.commit()
        conn.close()
    return db


PAYLOADS = (
    (hook(), 0),
    (hook(hook="post-launch"), 0),
    (hook(tool="other"), 22),
    (b"nope", 20),
)
SIDECARS = ("-wal", "-shm", "-journal")


def forbidden(*args: object, **kwargs: object) -> Any:
    raise AssertionError("a forbidden call was made")


def is_write_open(mode: object) -> bool:
    return isinstance(mode, str) and any(c in mode for c in "wax+")


def guarded_builtins_open(original: Any) -> Any:
    def inner(target: Any, mode: Any = "r", *args: Any, **kwargs: Any) -> Any:
        if is_write_open(mode):
            raise AssertionError("a file was opened for writing")
        return original(target, mode, *args, **kwargs)

    return inner


def guarded_os_open(original: Any) -> Any:
    bad = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def inner(target: Any, flags: int, *args: Any, **kwargs: Any) -> Any:
        if flags & bad:
            raise AssertionError("a file was opened for writing")
        return original(target, flags, *args, **kwargs)

    return inner


def guarded_connect(original: Any) -> Any:
    def inner(database: Any, *args: Any, **kwargs: Any) -> Any:
        text = os.fsdecode(database)
        if not (kwargs.get("uri") and ("mode=ro" in text or "immutable=1" in text)):
            raise AssertionError("state.db opened other than read-only")
        return original(database, *args, **kwargs)

    return inner


MODES = [
    (("test",), "stdin"),
    (("send", "--dry-run", "--stdin-json"), "stdin"),
    (("test", "--file"), "file"),
    (("send", "--dry-run", "--file"), "file"),
    (("test",), "flags"),
    (("send", "--dry-run"), "flags"),
    (("test", "--config"), "config"),
]


@pytest.mark.parametrize("state_kind", ["garbage", "valid", "missing"])
@pytest.mark.parametrize(("prefix", "mode"), MODES)
def test_nothing_is_written_locked_spawned_or_opened(
    inv: Invoker,
    configured: dict[str, Any],
    home: Path,
    tmp_path: Path,
    opened_names: list[str],
    monkeypatch: pytest.MonkeyPatch,
    prefix: tuple[str, ...],
    mode: str,
    state_kind: str,
) -> None:
    db = make_state(home, state_kind)
    state = db.parent
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    cases: list[tuple[tuple[str, ...], bytes | None, int]]
    if mode == "stdin":
        cases = [((), stdin, code) for stdin, code in PAYLOADS]
    elif mode == "file":
        cases = []
        for index, (stdin, code) in enumerate(PAYLOADS):
            path = tmp_path / f"in{index}.json"
            path.write_bytes(stdin)
            cases.append(((str(path),), None, code))
    elif mode == "flags":
        cases = [
            (("--message", "m"), None, 0),
            (("--title", "T", "--message", "m", "--source", "offsrc"), None, 22),
        ]
    else:
        alt = write_config(home, config_data(tmp_path), name="alt.yaml")
        cases = [((str(alt),), stdin, code) for stdin, code in PAYLOADS]
    before = snapshot(tmp_path)
    content = db.read_bytes() if db.exists() else None
    mark = len(opened_names)
    with monkeypatch.context() as m:
        for module, name in (
            (subprocess, "Popen"),
            (Gateway, "run"),
            (Gateway, "try_lock"),
            (Gateway, "open_private_dir"),
            (Gateway, "open_private_subdir"),
            (os, "system"),
            (os, "posix_spawn"),
            (os, "posix_spawnp"),
            (os, "fork"),
            (os, "execv"),
            (os, "execve"),
            (os, "execvp"),
            (os, "execvpe"),
        ):
            m.setattr(module, name, forbidden)
        m.setattr(sqlite3, "connect", guarded_connect(sqlite3.connect))
        m.setattr(builtins, "open", guarded_builtins_open(builtins.open))
        m.setattr(io, "open", guarded_builtins_open(io.open))
        m.setattr(os, "open", guarded_os_open(os.open))
        for extra, stdin, expected in cases:
            argv = [*prefix, *extra]
            # a database that cannot be opened is exit 1 for every notification that gets past
            # the input checks (20 and 2) and the enabled/configured checks, a no_rule, a drop
            # rule and an all-disabled result included
            want = 1 if expected in (0, 22) and state_kind in ("garbage", "valid") else expected
            for as_json in (False, True):
                code, _, _ = inv.run(*argv, *(["--json"] if as_json else []), stdin=stdin)
                assert code == want, argv
    names = [os.path.basename(n) for n in opened_names[mark:]]
    assert snapshot(tmp_path) == before
    # state.db is opened read-only (the guards above refuse any write open); never a lock
    assert not [n for n in names if n.endswith(".lock")]
    assert "log.jsonl" not in names
    if content is None:
        assert not db.exists()
    else:
        assert db.read_bytes() == content
    for suffix in SIDECARS:
        assert not (state / f"state.db{suffix}").exists()


def test_missing_state_directory_is_not_created(
    inv: Invoker, configured: dict[str, Any], home: Path
) -> None:
    inv.run("test", stdin=hook())
    assert not (home / ".local").exists()


@pytest.mark.parametrize("name", ["cli.py", "dryrun.py"])
def test_modules_have_no_process_or_database_imports(name: str) -> None:
    text = (ROOT / "src" / "notice_route" / name).read_text()
    assert "subprocess" not in text and "import sqlite3" not in text
    assert cli.PROG == "notice-route"


# --- standard output that cannot be written ----------------------------------------------------


def test_closed_standard_output_is_internal_not_a_traceback(
    inv: Invoker, configured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    code, _, err = inv.run("test", stdin=hook())
    assert code == 1
    assert err == "notice-route: internal: cannot write to standard output\n"


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="no /dev/full")
def test_a_full_standard_output_keeps_the_intended_exit_code(home: Path, tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "from notice_route.cli import main\n"
        "from notice_route.gateway import Gateway\n"
        "home = sys.argv[1]\n"
        "sys.exit(main(['test', '--message', 'm', '--json'], Gateway(passwd_home=lambda: home)))\n"
    )
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": os.defpath}
    with open("/dev/full", "wb") as full:
        result = subprocess.run(
            [sys.executable, "-c", script, str(home)],
            stdin=subprocess.DEVNULL,
            stdout=full,
            stderr=subprocess.PIPE,
            env=env,
            cwd=tmp_path,
            timeout=60,
            check=False,
        )
    assert result.returncode == 1
    assert result.stderr == b"notice-route: internal: cannot write to standard output\n"


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="no /dev/full")
@pytest.mark.parametrize("flag", ["--version", "--help"])
def test_help_and_version_on_a_full_standard_output_exit_one(
    flag: str, home: Path, tmp_path: Path
) -> None:
    script = f"import sys\nfrom notice_route.cli import main\nsys.exit(main([{flag!r}]))\n"
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": os.defpath}
    with open("/dev/full", "wb") as full:
        result = subprocess.run(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=full,
            stderr=subprocess.PIPE,
            env=env,
            cwd=tmp_path,
            timeout=60,
            check=False,
        )
    assert result.returncode == 1
    assert result.stderr == b"notice-route: internal: cannot write to standard output\n"


# --- a database that cannot be used is exit 1 in every case (R3) -------------------------------

CASES_STATE: dict[str, tuple[Any, ...]] = {
    "no_rule": (hook(tool="other"),),
    "drop_rule": (hook(hook="post-launch"),),
    "all_destinations_disabled": (None, "--title", "T", "--message", "m", "--source", "offsrc"),
    "all_floor_dropped": (None, "--title", "T", "--message", "m", "--source", "direct"),
}


@pytest.mark.parametrize("case", sorted(CASES_STATE))
@pytest.mark.parametrize("argv", [("test",), ("send", "--dry-run")])
def test_a_corrupt_state_db_is_exit_1_whatever_the_notification_decides(
    inv: Invoker, configured: dict[str, Any], home: Path, case: str, argv: tuple[str, ...]
) -> None:
    configured["destinations"]["chat"]["floor"] = "warn"
    write_config(home, configured)
    stdin, *extra = CASES_STATE[case]
    if stdin is not None and argv[0] == "send":
        argv = (*argv, "--stdin-json")
    argv = (*argv, *extra)
    ok_code, out, err = inv.run(*argv, stdin=stdin)
    assert ok_code in (0, 22), err  # no database: the notification own result
    db = make_state(home, "garbage")
    code, _, _ = inv.run(*argv, stdin=stdin)
    assert code == 1
    code, doc = inv.run_json(*argv, stdin=stdin)
    assert code == 1 and doc["error"]["code"] == "internal"
    assert db.read_bytes() == b"not a database"


@pytest.mark.parametrize("argv", [("test",), ("send", "--dry-run", "--stdin-json")])
def test_a_disabled_or_unconfigured_router_is_22_before_any_state_read(
    inv: Invoker, configured: dict[str, Any], home: Path, argv: tuple[str, ...]
) -> None:
    make_state(home, "garbage")
    configured["enabled"] = False
    write_config(home, configured)
    assert inv.run(*argv, stdin=hook())[0] == 22
    write_config(home, {})
    assert inv.run(*argv, stdin=hook())[0] == 22


@pytest.mark.parametrize("argv", [("test",), ("send", "--dry-run", "--stdin-json")])
def test_an_older_schema_is_exit_1_internal_for_a_read_only_open(
    inv: Invoker,
    configured: dict[str, Any],
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
) -> None:
    from notice_route.schema import MIGRATIONS
    from notice_route.store import open_readonly, open_store

    open_store(Gateway(passwd_home=lambda: str(home))).close()  # a database at version 1
    state = home / ".local" / "state" / "notice-route"
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    newer = (*MIGRATIONS, ("ALTER TABLE deliveries ADD COLUMN extra INTEGER",))
    monkeypatch.setattr(cli, "open_readonly", lambda gw: open_readonly(gw, migrations=newer))
    code, doc = inv.run_json(*argv, stdin=hook())
    assert code == 1 and doc["error"]["code"] == "internal"
    assert {p.name: p.read_bytes() for p in state.iterdir()} == before
