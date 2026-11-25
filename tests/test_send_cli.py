"""``send``: stdout and stderr discipline, exit codes, usage errors, hostile input and output,
the log (#22)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from notice_route import log as logmod
from notice_route.gateway import SYSTEM_TZ_DIRS
from tests.fakes import scrubbed_env
from tests.send_support import CANARY, SCRIPT, Rig, log_errors
from tests.test_cli_rules_validate import schema_errors

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = [
    pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database"),
    pytest.mark.usefixtures("send_environment"),
]

HOSTILE_MESSAGE = "HOSTILEMSG \x1b[31m‮\x07 {{title}} \u0085 \ud800 `rm -rf` $(x) ' OR 1=1 --"


def error_doc(out: str) -> dict[str, Any]:
    doc = json.loads(out)
    assert schema_errors(doc, "error.json") == [], doc
    return doc["error"]


# --- stdout and stderr -------------------------------------------------------------------------


def test_json_mode_prints_exactly_one_document_on_stdout(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    code, out, err = rig.run(
        "send", "--stdin-json", "--json", "--source", "s", stdin=b'{"message": "hi"}'
    )
    assert code == 0 and out.count("\n") == 1 and err == ""
    assert json.loads(out)["destinations"][0]["outcome"] == "sent"


def test_text_mode_has_an_empty_stdout_and_one_stderr_line_per_destination(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"a": {"notifier": n}, "b": {"notifier": n, "quiet": "night"}})
    code, out, err = rig.run("send", "--message", "hi", "--source", "s", "--label", "k=v")
    assert code == 0 and out == ""
    assert err.splitlines() == ["notice-route: a: sent", "notice-route: b: dropped (quiet)"]
    code, out, err = rig.run(
        "send", "--message", "other", "--source", "s", "--verbose",
    )  # fmt: skip
    assert out == "" and "decision a: send" in err and "notice-route: a: sent" in err


def test_a_failure_line_names_the_code(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier(error={"code": "credential_invalid"})}})
    code, out, err = rig.run("send", "--message", "hi", "--source", "s")
    assert code == 12 and out == ""
    assert err == "notice-route: chat: failed (credential_invalid)\n"


def test_unconfigured_text_run_prints_one_line(rig: Rig) -> None:
    code, out, err = rig.run("send", "--message", "hi")
    assert (code, out, err) == (22, "", "notice-route: no config\n")
    code, out, err = rig.run("send", "--message", "hi", "--quiet")
    assert (code, out, err) == (22, "", "")


# --- exit codes --------------------------------------------------------------------------------


def test_exit_20_refusals_send_nothing_and_write_no_state(rig: Rig, home: Path) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}})
    cases: list[tuple[bytes, tuple[str, ...], str, str | None]] = [
        (b'{"nope": 1}', (), "payload_unrecognized", None),
        (b'{"message": ""}', (), "payload_invalid", "/message"),
        (b'{"tool": "t", "hook": "h"}', ("--source", "other"), "source_mismatch", None),
        (b'{"message": "x"}', ("--source", "notice-route"), "source_mismatch", None),
        (b"not json", (), "payload_invalid", ""),
        (b'{"message": "x", "message": "y"}', (), "payload_invalid", ""),
        (b'{"message": "x"}', ("--label", "BAD"), "payload_invalid", "--label"),
    ]
    for stdin, extra, reason, pointer in cases:
        code, out, _ = rig.run("send", "--stdin-json", "--json", *extra, stdin=stdin)
        err = error_doc(out)
        assert code == 20 and err["code"] == "refused" and err["reason"] == reason, (stdin, extra)
        assert (err["details"] or {}).get("pointer") == pointer
    assert n.events("invoke") == [] and not (home / ".local").exists()


def test_exit_2_for_a_bad_config_names_the_key_and_sends_nothing(rig: Rig, home: Path) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}}, defaults={"dedup_window": "nonsense"})
    code, out, err = rig.run("send", "--message", "hi", "--json")
    e = error_doc(out)
    assert code == 2 and e["code"] == "config_invalid" and "nonsense" not in out + err
    assert not (home / ".local").exists()


def test_exit_1_for_a_state_database_that_cannot_be_used(rig: Rig, home: Path) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}})
    state = home / ".local" / "state" / "notice-route"
    state.mkdir(parents=True, mode=0o700)
    db = state / "state.db"
    db.write_bytes(b"this is not a database" * 100)
    db.chmod(0o600)
    code, out, err = rig.run("send", "--message", "hi", "--json", "--source", "s")
    e = error_doc(out)
    assert code == 1 and e["code"] == "internal"
    assert "not a database" not in out + err and n.events("invoke") == []


def test_exit_1_even_for_a_no_rule_result_when_the_database_is_bad(rig: Rig, home: Path) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}}, rules=[])
    state = home / ".local" / "state" / "notice-route"
    state.mkdir(parents=True, mode=0o700)
    (state / "state.db").write_bytes(b"x" * 4096)
    (state / "state.db").chmod(0o600)
    code, out, _ = rig.run("send", "--message", "hi", "--json")
    assert code == 1 and error_doc(out)["code"] == "internal"


# --- hook recursion ----------------------------------------------------------------------------


def test_hook_recursion_is_refused_before_anything_is_read(rig: Rig, home: Path) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    (home / ".config" / "notice-route" / "config.yaml").write_text("{ not: [valid")
    gw = rig.gateway(b'{"message": "hi"}', env={"NOTICE_ROUTE_HOOK": "1"})
    code, out, _ = rig.run("send", "--stdin-json", "--json", gateway=gw)
    e = error_doc(out)
    assert code == 20 and e["reason"] == "hook_recursion"
    import os as _os

    assert _os.read(gw.stdin_fd, 100) == b'{"message": "hi"}'  # stdin was never read
    assert not (home / ".local").exists()


# --- usage -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["send", "--bogus"],
        ["send", "--actor", "root"],
        ["send", "--actor", "human:", "--message", "x"],
        ["send", "--message", "x", "--decision-id", "a", "--decision-id", "b"],
        ["send", "--message", "x", "--decision-id", "bad id"],
        ["send", "--title", "only"],
        ["send", "--message", "x", "--config", "/etc/x.yaml"],
        ["--config", "/etc/x.yaml", "send", "--message", "x"],
        ["send", "--message", "x", "--verbose", "--quiet"],
        ["send", "--stdin-json", "--file", "x"],
    ],
)
def test_usage_errors_are_exit_2_with_the_envelope_under_json(rig: Rig, argv: list[str]) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    code, out, err = rig.run(*argv, "--json")
    e = error_doc(out)
    assert code == 2 and e["code"] == "usage"
    assert "/etc/x.yaml" not in out + err and "bad id" not in out + err
    code, out, err = rig.run(*argv)
    assert code == 2 and out == "" and err.startswith("notice-route: usage: ")


def test_config_before_the_command_works_only_for_the_commands_that_run_nothing(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    path = str(rig.home / ".config" / "notice-route" / "config.yaml")
    code, out, _ = rig.run("--config", path, "rules", "--json")
    assert code == 0 and json.loads(out)["rules"]
    code, out, _ = rig.run("--config", path, "flush", "--json")
    assert code == 2


# --- hostile input and hostile notifier output -------------------------------------------------


def test_hostile_notice_and_notifier_output_never_reach_stdout_stderr_or_log(rig: Rig) -> None:
    n = rig.notifier(
        stdout='{"error":{"code":"EVIL\\u001b[31m","message":"EVILTEXT","retryable":false,'
        '"unknown_outcome":false}}',
        stderr="STDERRMARK \x1b[31m",
        exit=12,
        validate_input=False,
    )
    rig.configure({"chat": {"notifier": n}})
    body = json.dumps({"message": HOSTILE_MESSAGE, "title": "TITLEMARK\x1b[0m"}).encode()
    code, out, err = rig.run("send", "--stdin-json", "--json", "--source", "s", stdin=body)
    doc = json.loads(out)
    assert code == 12
    d = doc["destinations"][0]
    assert (d["outcome"], d["code"]) == ("unknown", "envelope_invalid")
    everything = out + err + (rig.state / "log.jsonl").read_text()
    for marker in ("EVIL", "STDERRMARK", "\x1b", "‮", "HOSTILEMSG", "TITLEMARK"):
        assert marker not in everything, marker
    text_code, text_out, text_err = rig.run("send", "--message", "other", "--source", "s")
    assert text_out == "" and "\x1b" not in text_err


def test_a_notifier_that_floods_stdout_is_output_overflow_unknown(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier(stdout_bytes=70000)}})
    code, doc, _ = rig.send("hi")
    d = doc["destinations"][0]
    assert code == 12 and (d["outcome"], d["code"]) == ("unknown", "output_overflow")


def test_a_notifier_that_dies_by_signal_is_unknown_signal(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier(die_by_signal="KILL")}})
    code, doc, _ = rig.send("hi")
    d = doc["destinations"][0]
    assert code == 12 and (d["outcome"], d["code"]) == ("unknown", "signal")


def test_a_command_that_cannot_start_is_failed_exec_failed(rig: Rig, tmp_path: Path) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}})
    n.stub.chmod(0o666)  # group- and world-writable: the call-time check refuses it
    code, doc, _ = rig.send("hi")
    d = doc["destinations"][0]
    assert code == 12 and (d["outcome"], d["code"]) == ("failed", "exec_failed")


# --- the log -----------------------------------------------------------------------------------


def test_the_log_line_follows_the_schema_and_holds_no_text(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}})
    code, out, _ = rig.run(
        "send", "--stdin-json", "--json", "--source", "s", "--actor", "auto:test-policy",
        "--decision-id", "d-1:x", stdin=b'{"message": "BODYTEXT body", "title": "TITLETEXT"}',
    )  # fmt: skip
    doc = json.loads(out)
    (line,) = rig.log_lines()
    assert log_errors(line) == [] and code == 0
    assert (line["record"], line["destination"], line["outcome"], line["attempts"]) == (
        "delivery", "chat", "sent", 1,
    )  # fmt: skip
    assert (line["actor"], line["decision_id"]) == ("auto:test-policy", "d-1:x")
    assert line["message_sha256"] == logmod.sha16("BODYTEXT body")
    assert line["title_sha256"] == logmod.sha16("TITLETEXT")
    assert line["dedupe_key_sha256"] == doc["dedupe"][:16]
    assert (line["source"], line["rule"], line["level"]) == ("s", "pt", "info")
    text = (rig.state / "log.jsonl").read_text()
    assert "BODYTEXT" not in text and "TITLETEXT" not in text


def test_the_default_actor_is_auto_unknown_off_a_terminal(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    rig.send("hi")
    assert (
        rig.log_lines()[0]["actor"] == "auto:unknown" and rig.log_lines()[0]["decision_id"] is None
    )


def test_every_destination_outcome_is_logged_including_suppressed(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}, "late": {"notifier": n, "quiet": "night"}})
    rig.send("hi", level="warn")
    rig.send("hi", level="warn")
    got = [(x["destination"], x["outcome"], x["reason"]) for x in rig.log_lines()]
    assert got == [  # called destinations first (their results), then the decided ones
        ("chat", "sent", None),
        ("late", "deferred", "quiet"),
        ("chat", "suppressed", "repeat"),
        ("late", "suppressed", "repeat"),
    ]
    assert all(log_errors(x) == [] for x in rig.log_lines())


def test_log_false_writes_no_log(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}}, log=False)
    assert rig.send("hi")[0] == 0
    assert not (rig.state / "log.jsonl").exists()


def test_a_bad_log_warns_and_changes_no_result(rig: Rig, tmp_path: Path) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    rig.state.mkdir(parents=True, mode=0o700)
    target = tmp_path / "elsewhere"
    target.write_text("")
    (rig.state / "log.jsonl").symlink_to(target)
    code, doc, err = rig.send("hi")
    assert code == 0 and doc["destinations"][0]["outcome"] == "sent"
    assert "warning: the log could not be written" in err
    assert target.read_text() == ""


def test_a_run_keeps_its_text_out_of_the_log_and_a_variable_value_out_of_all(
    rig: Rig,
) -> None:
    rig.configure({"chat": {"notifier": rig.notifier(), "env": ["ROUTE_CRED"]}})
    code, out, err = rig.run(
        "send", "--stdin-json", "--json", "--source", "s",
        stdin=b'{"message": "queued body text"}',
        env={"ROUTE_CRED": CANARY},
    )  # fmt: skip
    assert CANARY not in out + err + rig.state_text()
    assert "queued body text" not in out + err + (rig.state / "log.jsonl").read_text()


# --- output that cannot be written -------------------------------------------------------------


def run_with_streams(
    rig: Rig, argv: list[str], body: bytes, *, out: str = "pipe", err: str = "pipe"
) -> tuple[int, bytes, bytes]:
    """The real ``main`` in a child whose stdout or stderr is ``/dev/full`` (``full``), closed
    (``closed``) or a pipe nobody reads (``epipe`` for stdout); returns the exit code and what
    arrived on the pipes that exist."""
    redirect = {"full": "/dev/full", "closed": "&-"}
    shell = 'exec "$@"'
    if out in redirect:
        shell += f" >{redirect[out]}"
    if err in redirect:
        shell += f" 2>{redirect[err]}"
    proc = subprocess.Popen(
        ["/bin/sh", "-c", shell, "sh", sys.executable, "-I", "-c", SCRIPT,
         str(rig.home), str(rig.tmp_path), *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=scrubbed_env(rig.home),
        cwd="/",
    )  # fmt: skip
    rig.procs.append(proc)
    assert proc.stdin is not None and proc.stdout is not None
    if out == "epipe":
        proc.stdout.close()
    proc.stdin.write(body)
    proc.stdin.close()
    proc.stdin = None
    got_out, got_err = proc.communicate(timeout=60)
    return proc.returncode, got_out, got_err


@pytest.mark.parametrize("out", ["full", "closed", "epipe"])
@pytest.mark.parametrize(("error", "expected"), [(None, 0), ({"code": "credential_invalid"}, 12)])
def test_an_unwritable_stdout_after_the_send_keeps_the_exit_code(
    rig: Rig, out: str, error: dict[str, str] | None, expected: int
) -> None:
    behavior: dict[str, Any] = {"error": error} if error else {}
    rig.configure({"chat": {"notifier": rig.notifier(**behavior)}})
    code, _, _ = run_with_streams(
        rig, ["send", "--stdin-json", "--json", "--source", "s"], b'{"message": "hi"}', out=out
    )
    assert code == expected
    assert rig.query("SELECT outcome FROM deliveries") == [("failed" if error else "sent",)]


@pytest.mark.parametrize("err", ["full", "closed"])
@pytest.mark.parametrize(("error", "expected"), [(None, 0), ({"code": "credential_invalid"}, 12)])
def test_an_unwritable_stderr_in_a_text_run_keeps_the_exit_code(
    rig: Rig, err: str, error: dict[str, str] | None, expected: int
) -> None:
    behavior: dict[str, Any] = {"error": error} if error else {}
    rig.configure({"chat": {"notifier": rig.notifier(**behavior)}})
    code, out, _ = run_with_streams(
        rig, ["send", "--stdin-json", "--source", "s"], b'{"message": "hi"}', err=err
    )
    assert (code, out) == (expected, b"")


LATE_SIGNAL_SCRIPT = (
    "import os, signal, sys\n"
    "from notice_route import cli\n"
    "from notice_route.gateway import Gateway\n"
    "real = cli._json\n"
    "def late(document):\n"
    "    os.kill(os.getpid(), signal.SIGTERM)\n"
    "    real(document)\n"
    "cli._json = late\n"
    "home, tmp = sys.argv[1], sys.argv[2]\n"
    "sys.exit(cli.main(sys.argv[3:], Gateway(passwd_home=lambda: home, exec_check_from=tmp)))\n"
)


def test_a_signal_after_the_calls_does_not_kill_the_run_before_its_document(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", LATE_SIGNAL_SCRIPT, str(rig.home), str(rig.tmp_path),
         "send", "--stdin-json", "--json", "--source", "s"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=scrubbed_env(rig.home), cwd="/",
    )  # fmt: skip
    rig.procs.append(proc)
    out, _ = proc.communicate(b'{"message": "hi"}', timeout=60)
    assert proc.returncode == 0
    assert json.loads(out)["destinations"][0]["outcome"] == "sent"


@pytest.mark.parametrize(
    "extra",
    [["--actor", "root"], ["--decision-id", "a", "--decision-id", "b"], ["--decision-id", "a b"]],
)
def test_a_dry_run_checks_the_audit_flags_like_a_send(rig: Rig, extra: list[str]) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    code, out, _ = rig.run("send", "--dry-run", "--message", "x", "--source", "s", "--json", *extra)
    assert code == 2 and error_doc(out)["code"] == "usage"


def test_the_default_actor_on_a_terminal_is_human_and_the_login_name(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notice_route.gateway import Gateway

    monkeypatch.setattr(Gateway, "stdin_is_tty", lambda self: True)
    monkeypatch.setattr(Gateway, "login_name", lambda self: "some.user")
    rig.configure({"chat": {"notifier": rig.notifier()}})
    code, _, _ = rig.run("send", "--message", "hi", "--source", "s")
    assert code == 0 and rig.log_lines()[0]["actor"] == "human:some.user"
