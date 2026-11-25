"""The ``test`` command and ``send --dry-run`` (#13): output against schemas/cli/test.json, text and
``--json``, exit codes, ``--config``, the order of the input checks, and the promise that nothing
is written, locked, spawned or sent."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
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


# --- input -------------------------------------------------------------------------------------


def test_default_input_is_stdin(inv: Invoker, configured: dict[str, Any]) -> None:
    assert inv.run_json("test", stdin=hook())[0] == 0
    assert inv.run_json("test", "--stdin-json", stdin=hook())[0] == 0
    assert inv.run_json("test", "--file", "-", stdin=hook())[0] == 0


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
    digest = json.loads(inv.run("test", "--json", stdin=pr)[1])["dedupe"]
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
    matched = 0
    assert examples
    for name, payload in examples.items():
        hook_name = name.removesuffix("-agent").removesuffix("-session")
        stdin = json.dumps({**payload, "hook": hook_name}).encode()
        code, doc = inv.run_json("test", stdin=stdin)
        assert code in (0, 22), name
        assert schema_errors(doc, "test.json") == [], name
        assert doc["missing"] == [] and doc["excluded"] == [], name
        matched += code == 0
    # TODO(#30): every example must match a rule once the example issue lands; today the
    # post-state-change examples match none of the wiring rules.
    assert matched >= 1


# --- no side effects ---------------------------------------------------------------------------


def snapshot(root: Path) -> list[tuple[str, int, int, int]]:
    out: list[tuple[str, int, int, int]] = []
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        out.append((str(path.relative_to(root)), st.st_mode, st.st_size, st.st_mtime_ns))
    return out


@pytest.mark.parametrize(
    "argv",
    [
        ("test",),
        ("send", "--dry-run", "--stdin-json"),
        ("test", "--json"),
        ("send", "--dry-run", "--stdin-json", "--json"),
    ],
)
def test_nothing_is_written_locked_spawned_or_opened(
    inv: Invoker,
    configured: dict[str, Any],
    home: Path,
    tmp_path: Path,
    opened_names: list[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
) -> None:
    state = home / ".local" / "state" / "notice-route"
    state.mkdir(parents=True)
    state.chmod(0o700)
    (state / "state.db").write_bytes(b"not a database")
    calls: list[str] = []

    def spawn(*args: object, **kwargs: object) -> None:
        calls.append("spawn")
        raise AssertionError("spawned")

    def lock(*args: object, **kwargs: object) -> None:
        calls.append("lock")
        raise AssertionError("locked")

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(Gateway, "run", spawn)
    monkeypatch.setattr(Gateway, "try_lock", lock)
    monkeypatch.setattr(Gateway, "open_private_dir", lock)
    monkeypatch.setattr(Gateway, "open_private_subdir", lock)
    before = snapshot(tmp_path)
    mark = len(opened_names)
    for stdin in (hook(), hook(hook="post-launch"), hook(tool="other"), b"nope"):
        inv.run(*argv, stdin=stdin)
    names = [os.path.basename(n) for n in opened_names[mark:]]
    assert calls == []
    assert snapshot(tmp_path) == before
    assert (state / "state.db").read_bytes() == b"not a database"
    assert not [n for n in names if n.startswith("state.db") or n.endswith(".lock")]
    assert not [n for n in names if n == "log.jsonl"]


def test_missing_state_db_is_not_created(
    inv: Invoker, configured: dict[str, Any], home: Path
) -> None:
    inv.run("test", stdin=hook())
    assert not (home / ".local").exists()


def test_cli_module_has_no_process_imports() -> None:
    text = (ROOT / "src" / "notice_route" / "cli.py").read_text()
    assert "subprocess" not in text and "import sqlite3" not in text
    assert cli.PROG == "notice-route"
