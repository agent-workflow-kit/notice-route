"""``validate --probe`` (#29): the notifiers' own ``validate`` as a normal call, never a send.

Through the real CLI entry with the fake notifiers: which destinations are probed, the argv and
environment of the call, the send lock, how every end of a call is classified, that nothing is
written to the queue, the history or the log, that offline ``validate`` still runs nothing, and
that no notifier text is ever echoed."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from notice_route import gateway as gateway_module
from tests.fakes import Notifier
from tests.send_support import Rig, RigClock
from tests.test_cli_rules_validate import schema_errors

CANARY = "leak-canary-value"
PROBE = ("validate", "--probe", "--json")


@pytest.fixture(autouse=True)
def private_environment(send_environment: None) -> None:
    return None


def probe(rig: Rig, *extra: str, env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """Run ``validate --probe --json``; the document is checked against the schema."""
    code, out, err = rig.run(*PROBE, *extra, env=env)
    assert out.endswith("\n") and out.count("\n") == 1, (out, err)
    doc = json.loads(out)
    if "error" not in doc:
        assert schema_errors(doc, "validate.json") == [], doc
    return code, doc


def only(doc: dict[str, Any]) -> dict[str, Any]:
    assert len(doc["destinations"]) == 1
    return doc["destinations"][0]


def invokes(n: Notifier) -> list[dict[str, Any]]:
    return n.events("invoke")


def hold_lock(rig: Rig, name: str) -> Any:
    gw = rig.gateway()
    with gw.open_state_dir() as state, gw.open_private_subdir(state, "locks") as locks:
        held = gw.open_file(locks, f"{name}.lock", mode="a", create=True)
    assert gw.try_lock(held, 0)
    return held


# --- which destinations are probed, and how ------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "tool", "tail"),
    [
        ("slack-notice", "slack-notice", ["validate", "--json"]),
        ("ha-notice", "ha-notice", ["validate", "--json", "--probe"]),
        # the fake has no wsl-notice tool: ha-notice shares the ``--probe`` grammar
        ("wsl-notice", "ha-notice", ["validate", "--json", "--probe"]),
    ],
)
def test_each_known_kind_is_probed_with_its_own_argv_and_never_a_test_message(
    rig: Rig, kind: str, tool: str, tail: list[str]
) -> None:
    n = rig.notifier(kind, tool=tool)
    rig.configure({"d": {"notifier": n, "kind": kind}})
    code, doc = probe(rig)
    assert code == 0 and doc["outcome"] == "ok" and doc["probe"] is True
    assert only(doc)["probe"] == {"status": "ok", "exit": 0, "enabled": True}
    (call,) = invokes(n)
    assert call["argv"] == tail  # exactly this: no --test-message, no send, no --actor
    assert ("--probe" in call["argv"]) is (kind != "slack-notice")
    # the fake reads fd 0 on validate and logs what it got: nothing is written to a probe's stdin
    (stdin,) = n.events("stdin")
    assert stdin["size"] == 0 and stdin["text"] == ""


def test_the_cmd_extra_arguments_come_before_validate(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"d": {"cmd": [*n.cmd, "--config", "/x/y.yaml"]}})
    code, doc = probe(rig)
    assert code == 0
    assert invokes(n)[0]["argv"] == ["--config", "/x/y.yaml", "validate", "--json"]


def test_the_call_has_its_own_session_an_empty_cwd_and_exactly_the_allow_list_environment(
    rig: Rig,
) -> None:
    n = rig.notifier()
    rig.configure({"d": {"notifier": n, "env": ["CRED_ONE"]}})
    secret_env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "CRED_ONE": CANARY,
        "CRED_TWO": "other",
        "HOME": "/nonexistent",
        "NOTICE_ROUTE_HOOK": "1",
        "SLACK_TOKEN_X": "z",
    }
    code, doc = probe(rig, env=secret_env)
    assert code == 0
    (call,) = invokes(n)
    assert call["env"] == {
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CRED_ONE": CANARY,
    }
    assert call["cwd"] == "/"
    assert call["pid"] == call["pgid"] == call["sid"]
    assert call["open_fds"] == [0, 1, 2]


def test_a_wsl_destination_gets_the_wsl_names_too(rig: Rig) -> None:
    n = rig.notifier("wsl-notice", tool="ha-notice")
    rig.configure({"w": {"notifier": n, "kind": "wsl-notice"}})
    code, _ = probe(rig, env={"WSL_INTEROP": "/run/x", "WSL_DISTRO_NAME": "d", "OTHER": "1"})
    assert code == 0
    env = invokes(n)[0]["env"]
    assert env["WSL_INTEROP"] == "/run/x" and env["WSL_DISTRO_NAME"] == "d"
    assert "OTHER" not in env


def test_generic_disabled_and_env_unset_destinations_are_not_probed(rig: Rig) -> None:
    gen = rig.notifier("gen")
    off = rig.notifier("off")
    unset = rig.notifier("unset")
    empty = rig.notifier("empty")
    rig.configure(
        {
            "gen": {"notifier": gen, "kind": "generic"},
            "off": {"notifier": off, "enabled": False},
            "unset": {"notifier": unset, "env": ["CRED_ONE"]},
            "empty": {"notifier": empty, "env": ["CRED_TWO"]},
        }
    )
    code, doc = probe(rig, env={"CRED_TWO": ""})
    assert code == 0 and doc["outcome"] == "ok"
    by_name = {d["name"]: d for d in doc["destinations"]}
    assert all(d["probe"] is None for d in by_name.values())
    assert by_name["unset"]["env_unset"] == ["CRED_ONE"]
    assert by_name["empty"]["env_unset"] == ["CRED_TWO"]
    for n in (gen, off, unset, empty):
        assert invokes(n) == []


def test_a_set_variable_makes_the_destination_probed_and_is_passed(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"d": {"notifier": n, "env": ["CRED_ONE"]}})
    code, doc = probe(rig, env={"CRED_ONE": "v"})
    assert code == 0 and only(doc)["env_unset"] == [] and only(doc)["probe"]["status"] == "ok"
    assert invokes(n)[0]["env"]["CRED_ONE"] == "v"


def test_no_destinations_to_probe_is_ok_and_opens_no_state(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"g": {"notifier": n, "kind": "generic"}})
    code, doc = probe(rig)
    assert code == 0 and doc["probe"] is True and only(doc)["probe"] is None
    assert not rig.state.exists()


def test_a_cmd_that_fails_the_check_is_a_failed_probe_and_is_never_started(rig: Rig) -> None:
    n = rig.notifier()
    n.stub.chmod(0o777)
    rig.configure({"d": {"notifier": n}})
    code, doc = probe(rig)
    d = only(doc)
    assert (code, doc["outcome"], d["cmd_ok"]) == (12, "failed", False)
    assert d["probe"] == {"status": "failed", "exit": None, "enabled": None}
    assert invokes(n) == []


def test_a_missing_cmd_is_a_failed_probe(rig: Rig, tmp_path: Path) -> None:
    n = rig.notifier()
    rig.configure({"d": {"cmd": [str(tmp_path / "bin" / "absent")]}})
    code, doc = probe(rig)
    assert code == 12 and only(doc)["probe"] == {"status": "failed", "exit": None, "enabled": None}
    assert invokes(n) == []


# --- how a call is classified -------------------------------------------------------------------


def test_exit_22_is_disabled_information_and_the_run_still_passes(rig: Rig) -> None:
    n = rig.notifier(tool="ha-notice", disabled=True)  # ha-notice answers 22 with --probe
    rig.configure({"d": {"notifier": n, "kind": "ha-notice"}})
    code, doc = probe(rig)
    assert (code, doc["outcome"]) == (0, "ok")
    assert only(doc)["probe"] == {"status": "disabled", "exit": 22, "enabled": False}


def test_a_disabled_slack_notice_answers_plain_validate_with_exit_0_and_enabled_false(
    rig: Rig,
) -> None:
    n = rig.notifier(disabled=True)
    rig.configure({"d": {"notifier": n}})
    code, doc = probe(rig)
    assert code == 0 and only(doc)["probe"] == {"status": "ok", "exit": 0, "enabled": False}


def test_exit_0_with_the_disabled_outcome_and_no_enabled_key_is_enabled_null(rig: Rig) -> None:
    n = rig.notifier(stdout='{"outcome": "disabled"}')
    rig.configure({"d": {"notifier": n}})
    code, doc = probe(rig)
    assert code == 0 and only(doc)["probe"] == {"status": "ok", "exit": 0, "enabled": None}


def test_exit_22_with_an_explicit_enabled_boolean_reports_it(rig: Rig) -> None:
    n = rig.notifier(tool="ha-notice", exit=22, stdout='{"enabled": true}')
    rig.configure({"d": {"notifier": n, "kind": "ha-notice"}})
    code, doc = probe(rig)
    assert code == 0 and only(doc)["probe"] == {"status": "disabled", "exit": 22, "enabled": True}


def test_the_notifiers_own_enabled_value_is_reported_on_exit_0(rig: Rig) -> None:
    n = rig.notifier(result={"enabled": False})
    rig.configure({"d": {"notifier": n}})
    code, doc = probe(rig)
    assert code == 0 and only(doc)["probe"] == {"status": "ok", "exit": 0, "enabled": False}


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        "[]",
        '{"enabled": "yes"}',
        '{"enabled": 1}',
        '{"enabled": null}',
        '{"enabled": true, "enabled": false}',
        '{"enabled": NaN}',
        "",
    ],
)
def test_an_enabled_value_that_is_not_a_clean_boolean_is_null(rig: Rig, stdout: str) -> None:
    n = rig.notifier(stdout=stdout)
    rig.configure({"d": {"notifier": n}})
    code, doc = probe(rig)
    assert code == 0 and only(doc)["probe"] == {"status": "ok", "exit": 0, "enabled": None}


@pytest.mark.parametrize("exit_code", [1, 2, 10, 12, 20, 21, 23, 64, 129, 143, 255])
def test_any_other_exit_is_failed_with_its_code_and_run_exit_12(rig: Rig, exit_code: int) -> None:
    n = rig.notifier(exit=exit_code, stdout=json.dumps({"ok": False, "enabled": True}))
    rig.configure({"d": {"notifier": n}})
    code, doc = probe(rig)
    assert (code, doc["outcome"]) == (12, "failed")
    # a failed check still prints the document with the notifier's own boolean (spec clarification)
    assert only(doc)["probe"] == {"status": "failed", "exit": exit_code, "enabled": True}


@pytest.mark.parametrize("stdout", ["not json", "[]", '{"enabled": "no"}', '{"enabled": 1}', ""])
@pytest.mark.parametrize("exit_code", [2, 12])
def test_a_failed_check_with_a_hostile_or_unreadable_body_is_enabled_null(
    rig: Rig, exit_code: int, stdout: str
) -> None:
    n = rig.notifier(exit=exit_code, stdout=stdout)
    rig.configure({"d": {"notifier": n}})
    code, doc = probe(rig)
    assert code == 12
    assert only(doc)["probe"] == {"status": "failed", "exit": exit_code, "enabled": None}


def test_a_failed_check_with_enabled_false_reports_false(rig: Rig) -> None:
    n = rig.notifier(exit=2, stdout='{"ok": false, "enabled": false, "checks": []}')
    rig.configure({"d": {"notifier": n}})
    code, doc = probe(rig)
    assert code == 12 and only(doc)["probe"] == {"status": "failed", "exit": 2, "enabled": False}


def test_a_failed_check_envelope_is_failed_and_its_text_is_not_echoed(rig: Rig) -> None:
    n = rig.notifier(error={"code": "credential_invalid"}, stderr=f"{CANARY} on stderr\n")
    rig.configure({"d": {"notifier": n}})
    code, out, err = rig.run(*PROBE)
    assert code == 12
    assert CANARY not in out + err and "credential_invalid" not in out + err


def test_hostile_stdout_and_stderr_never_reach_the_output(rig: Rig) -> None:
    hostile = f"\x1b]0;{CANARY}\x07‮{CANARY}\x00"
    n = rig.notifier(stdout=json.dumps({"enabled": True, "x": hostile}), stderr=hostile)
    rig.configure({"d": {"notifier": n}})
    for argv in (PROBE, ("validate", "--probe")):
        code, out, err = rig.run(*argv)
        assert code == 0
        assert CANARY not in out + err
        assert "\x1b" not in out + err


def test_a_call_that_ran_past_its_deadline_is_killed_and_failed(rig: Rig) -> None:
    rig.clock = RigClock(rate=60.0)
    n = rig.notifier(hang=True, max_seconds=60)
    rig.configure({"d": {"notifier": n, "timeout": 1}})
    started = time.monotonic()
    code, doc = probe(rig)
    assert (code, doc["outcome"]) == (12, "failed")
    assert only(doc)["probe"] == {"status": "failed", "exit": None, "enabled": None}
    assert time.monotonic() - started < 30
    (call,) = invokes(n)
    assert call["pid"] and n.events("exit") == []  # the fake did not end by itself


def test_a_call_that_ignores_term_is_still_killed(rig: Rig) -> None:
    rig.clock = RigClock(rate=60.0)
    n = rig.notifier(hang=True, signals={"TERM": "ignore"}, max_seconds=60)
    rig.configure({"d": {"notifier": n, "timeout": 1}})
    code, doc = probe(rig)
    assert code == 12 and only(doc)["probe"]["exit"] is None
    assert n.events("signal")  # TERM arrived and was ignored; the kill followed


def test_output_over_the_cap_kills_the_call_and_is_failed(rig: Rig) -> None:
    n = rig.notifier(stdout_bytes=70_000)
    rig.configure({"d": {"notifier": n}})
    code, doc = probe(rig)
    assert code == 12 and only(doc)["probe"] == {"status": "failed", "exit": None, "enabled": None}


def test_stderr_over_the_cap_is_failed_too(rig: Rig) -> None:
    n = rig.notifier(stderr_bytes=20_000)
    rig.configure({"d": {"notifier": n}})
    code, doc = probe(rig)
    assert code == 12 and only(doc)["probe"] == {"status": "failed", "exit": None, "enabled": None}


def test_a_call_ended_by_a_signal_is_failed_with_no_exit(rig: Rig) -> None:
    n = rig.notifier(die_by_signal="SEGV")
    rig.configure({"d": {"notifier": n}})
    code, doc = probe(rig)
    assert code == 12 and only(doc)["probe"] == {"status": "failed", "exit": None, "enabled": None}


def test_a_descendant_that_left_the_group_cannot_hold_the_probe(rig: Rig) -> None:
    rig.clock = RigClock(rate=20.0)
    n = rig.notifier(grandchild={"setsid": True, "hold_stdout": True, "lifetime": 8})
    rig.configure({"d": {"notifier": n}})
    started = time.monotonic()
    code, doc = probe(rig)
    assert code == 0 and only(doc)["probe"]["status"] == "ok"
    assert time.monotonic() - started < 6  # the grandchild lives 8 s: not waited for


# --- the send lock ------------------------------------------------------------------------------


def test_a_busy_lock_is_a_failed_probe_with_no_call_and_no_queue_row(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}}, lock_wait="1s")
    held = hold_lock(rig, "chat")
    try:
        code, doc = probe(rig)
    finally:
        held.close()
    assert (code, doc["outcome"]) == (12, "failed")
    assert only(doc)["probe"] == {"status": "failed", "exit": None, "enabled": None}
    assert invokes(n) == []
    assert not (rig.state / "state.db").exists()  # no queue row, no count, no outcome


def test_the_lock_is_free_again_after_a_probe(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}}, lock_wait="1s")
    assert probe(rig)[0] == 0
    held = hold_lock(rig, "chat")  # takes it at once: the probe released it
    held.close()


def test_a_held_lock_of_another_destination_does_not_block_a_probe(rig: Rig) -> None:
    a, b = rig.notifier("a"), rig.notifier("b")
    rig.configure({"a": {"notifier": a}, "b": {"notifier": b}}, lock_wait="1s")
    held = hold_lock(rig, "a")
    try:
        code, doc = probe(rig)
    finally:
        held.close()
    by_name = {d["name"]: d["probe"]["status"] for d in doc["destinations"]}
    assert (code, by_name) == (12, {"a": "failed", "b": "ok"})
    assert invokes(a) == [] and len(invokes(b)) == 1


def test_wsl_destinations_probe_one_after_the_other_under_the_shared_lock(rig: Rig) -> None:
    n = rig.notifier("wsl-notice", tool="ha-notice", sleep=0.2)
    rig.configure(
        {"a": {"notifier": n, "kind": "wsl-notice"}, "b": {"notifier": n, "kind": "wsl-notice"}},
        lock_wait="10s",
    )
    code, doc = probe(rig)
    assert code == 0 and [d["probe"]["status"] for d in doc["destinations"]] == ["ok", "ok"]
    order = [r["event"] for r in n.records() if r["event"] in ("invoke", "exit")]
    assert order == ["invoke", "exit", "invoke", "exit"]


def test_the_shared_wsl_lock_blocks_every_wsl_destination_and_only_those(rig: Rig) -> None:
    w1, w2, s = (
        rig.notifier("w1", tool="ha-notice"),
        rig.notifier("w2", tool="ha-notice"),
        rig.notifier("s"),
    )
    rig.configure(
        {
            "a": {"notifier": w1, "kind": "wsl-notice"},
            "b": {"notifier": w2, "kind": "wsl-notice"},
            "c": {"notifier": s},
        },
        lock_wait="1s",
    )
    held = hold_lock(rig, "_wsl")
    try:
        code, doc = probe(rig)
    finally:
        held.close()
    by_name = {d["name"]: d["probe"]["status"] for d in doc["destinations"]}
    assert (code, by_name) == (12, {"a": "failed", "b": "failed", "c": "ok"})
    assert invokes(w1) == [] and invokes(w2) == [] and len(invokes(s)) == 1


# --- the flags -----------------------------------------------------------------------------------


def test_config_with_probe_is_refused_with_usage_and_nothing_runs(rig: Rig, tmp_path: Path) -> None:
    n = rig.notifier()
    rig.configure({"d": {"notifier": n}})
    other = tmp_path / "other.yaml"
    other.write_text("{}")
    other.chmod(0o600)
    for argv in (
        ("validate", "--probe", "--config", str(other), "--json"),
        ("--config", str(other), "validate", "--probe", "--json"),
    ):
        code, out, _ = rig.run(*argv)
        doc = json.loads(out)
        assert code == 2 and doc["error"]["code"] == "usage"
        assert str(other) not in out
    assert invokes(n) == []


def test_validate_with_config_but_without_probe_is_unchanged(rig: Rig, tmp_path: Path) -> None:
    n = rig.notifier()
    rig.configure({"d": {"notifier": n}})
    code, out, _ = rig.run(
        "validate", "--config", str(rig.home / ".config/notice-route/config.yaml"), "--json"
    )
    assert code == 0 and json.loads(out)["probe"] is False
    assert invokes(n) == []


def test_probe_is_accepted_only_by_validate_and_abbreviations_are_refused(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"d": {"notifier": n}})
    for argv in (("validate", "--prob"), ("flush", "--probe")):
        code, _, _ = rig.run(*argv, "--json")
        assert code == 2
    # send is given valid input, so the refusal can only come from --probe
    code, out, _ = rig.run(
        "send", "--stdin-json", "--probe", "--json", stdin=b'{"message": "hello"}'
    )
    doc = json.loads(out)
    assert code == 2 and doc["error"]["code"] == "usage"
    assert doc["error"]["message"] == "the command line cannot be used"
    assert invokes(n) == []
    ok, _, _ = rig.run("send", "--stdin-json", "--json", stdin=b'{"message": "hello"}')
    assert ok == 0


def test_text_output_names_the_probe_without_notifier_text(rig: Rig) -> None:
    n = rig.notifier(stdout=f'{{"enabled": true, "m": "{CANARY}"}}')
    rig.configure({"d": {"notifier": n}})
    code, out, err = rig.run("validate", "--probe")
    assert code == 0 and CANARY not in out + err
    assert "probe=ok" in out and "exit=0" in out and "enabled=true" in out
    bad = rig.notifier("bad", exit=12, stdout="not json")
    rig.configure({"d": {"notifier": bad}})
    code, out, _ = rig.run("validate", "--probe")
    assert code == 12 and "probe=failed" in out and "exit=12" in out and "enabled=unknown" in out


def test_a_probe_report_of_a_run_without_probe_is_null_and_probe_false(rig: Rig) -> None:
    rig.configure({"d": {"notifier": rig.notifier()}})
    code, out, _ = rig.run("validate", "--json")
    doc = json.loads(out)
    assert code == 0 and doc["probe"] is False and only(doc)["probe"] is None
    assert schema_errors(doc, "validate.json") == []


# --- nothing is written, nothing runs offline -----------------------------------------------------


def tree(root: Path) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for p in sorted(root.rglob("*")):
        out[str(p.relative_to(root))] = p.read_bytes() if p.is_file() else None
    return out


def test_a_probe_writes_nothing_but_its_lock_files(rig: Rig) -> None:
    n = rig.notifier(exit=12)
    rig.configure({"d": {"notifier": n}})
    rig.send("seed")  # a real state directory with a queue, history and a log
    before = tree(rig.state)
    code, _ = probe(rig)
    assert code == 12
    after = tree(rig.state)
    added = set(after) - set(before)
    assert added <= {"locks", "locks/d.lock"}
    assert {k: v for k, v in after.items() if k in before and k != "locks"} == {
        k: v for k, v in before.items() if k != "locks"
    }


def test_a_probe_on_a_fresh_home_creates_no_database_log_or_queue(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"d": {"notifier": n}})
    code, _ = probe(rig)
    assert code == 0
    names = {p.name for p in rig.state.rglob("*")}
    assert names == {"locks", "d.lock"}
    assert not any(p.name.startswith(("state.db", "log")) for p in rig.state.rglob("*"))


def test_a_probe_does_not_read_the_cron_env_file(rig: Rig, tmp_path: Path) -> None:
    n = rig.notifier()
    env_file = tmp_path / "cron.env"
    env_file.write_text(f"X={CANARY}\n")
    env_file.chmod(0o600)
    rig.configure({"d": {"notifier": n}}, cron={"env_file": str(env_file)})
    code, out, err = rig.run(*PROBE)
    assert code in (0, 12) and CANARY not in out + err
    assert CANARY not in json.dumps(invokes(n))


def test_offline_validate_runs_nothing_even_with_every_spawn_path_closed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = rig.notifier()
    rig.configure({"d": {"notifier": n}})

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("offline validate must not start a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(os, "fork", refuse)
    monkeypatch.setattr(gateway_module.Gateway, "run", refuse)
    code, out, _ = rig.run("validate", "--json")
    assert code == 0 and json.loads(out)["probe"] is False
    assert invokes(n) == [] and not rig.state.exists()


# --- hooks, signals ------------------------------------------------------------------------------


def test_a_probe_run_from_a_hook_is_not_refused_and_the_variable_is_not_passed(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"d": {"notifier": n}})
    code, doc = probe(rig, env={"NOTICE_ROUTE_HOOK": "post-fail", "NOTICE_ROUTE_ID": "1"})
    assert code == 0 and only(doc)["probe"]["status"] == "ok"
    assert set(invokes(n)[0]["env"]) == {"PATH", "LANG", "LC_ALL"}


class SignalClock(RigClock):
    """Sends SIGTERM to this process the first time ``when`` says so (from the clock reads)."""

    def __init__(self, when: Any) -> None:
        super().__init__()
        self.when = when
        self.sent = False

    def monotonic(self) -> float:
        if not self.sent and self.when():
            self.sent = True
            os.kill(os.getpid(), signal.SIGTERM)
        return super().monotonic()


def test_a_signal_before_any_call_started_exits_128_plus_n_and_prints_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = rig.notifier("a"), rig.notifier("b")
    rig.configure({"a": {"notifier": a}, "b": {"notifier": b}}, lock_wait="5s")
    held = hold_lock(rig, "a")  # the first destination waits for its lock; the signal arrives
    real = gateway_module.Gateway.try_lock

    def signaling(self: Any, *args: Any, **kwargs: Any) -> bool:
        os.kill(os.getpid(), signal.SIGTERM)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(gateway_module.Gateway, "try_lock", signaling)
    started = time.monotonic()
    try:
        code, out, _ = rig.run(*PROBE)
    finally:
        held.close()
    assert (code, out) == (128 + signal.SIGTERM, "")
    assert time.monotonic() - started < 3  # the wait ended at once, not after lock_wait
    assert invokes(a) == [] and invokes(b) == []


def test_a_signal_after_a_call_started_finishes_it_and_leaves_the_rest_unprobed(rig: Rig) -> None:
    a, b = rig.notifier("a", sleep=0.3), rig.notifier("b")
    rig.configure({"a": {"notifier": a}, "b": {"notifier": b}})
    rig.clock = SignalClock(lambda: a.log_path.exists())
    code, out, _ = rig.run(*PROBE)
    doc = json.loads(out)
    assert schema_errors(doc, "validate.json") == []
    statuses = {d["name"]: d["probe"] for d in doc["destinations"]}
    assert statuses["a"] == {"status": "ok", "exit": 0, "enabled": True}
    assert statuses["b"] == {"status": "failed", "exit": None, "enabled": None}
    assert code == 12 and invokes(b) == []


def test_a_wall_clock_step_changes_nothing(rig: Rig) -> None:
    n = rig.notifier(sleep=0.2)
    rig.configure({"d": {"notifier": n}})
    rig.clock.jump_wall(-86400 * 400)
    code, doc = probe(rig)
    assert code == 0 and only(doc)["probe"]["status"] == "ok"


def test_an_unsafe_state_directory_is_the_same_error_as_plain_validate(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"d": {"notifier": n}})
    rig.state.mkdir(parents=True)
    rig.state.chmod(0o777)
    plain_code, plain_out, _ = rig.run("validate", "--json")
    code, out, err = rig.run(*PROBE)
    assert (code, json.loads(out)["error"]) == (plain_code, json.loads(plain_out)["error"])
    assert code == 1 and json.loads(out)["error"]["message"] == "state database cannot be read"
    assert "Traceback" not in out + err and invokes(n) == []


def _signal_inside(monkeypatch: pytest.MonkeyPatch, module: Any, name: str, *, after: bool) -> None:
    real = getattr(module, name)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not after:
            os.kill(os.getpid(), signal.SIGTERM)
        result = real(*args, **kwargs)
        if after:
            os.kill(os.getpid(), signal.SIGTERM)
        return result

    monkeypatch.setattr(module, name, wrapper)


def test_a_signal_during_the_output_after_a_call_ran_still_exits_with_the_report_code(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notice_route import cli

    n = rig.notifier(exit=12)
    rig.configure({"d": {"notifier": n}})
    _signal_inside(monkeypatch, cli, "_json", after=False)
    code, out, _ = rig.run(*PROBE)
    assert code == 12  # not 128+N: a call ran
    assert only(json.loads(out))["probe"]["exit"] == 12
    assert len(invokes(n)) == 1


def test_a_signal_during_the_output_when_no_call_started_exits_128_plus_n_with_no_output(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notice_route import cli

    rig.configure({"g": {"notifier": rig.notifier(), "kind": "generic"}})
    _signal_inside(monkeypatch, cli, "_json", after=False)
    code, out, _ = rig.run(*PROBE)
    assert (code, out) == (128 + signal.SIGTERM, "")


def test_a_signal_inside_run_probes_with_every_probe_skipped_exits_128_plus_n(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = rig.notifier()
    n.stub.chmod(0o777)  # cmd[0] fails the check: never started
    rig.configure({"d": {"notifier": n}})
    # delivered once the targets exist and the loop passed its stop() check, before any call
    _signal_inside(monkeypatch, gateway_module.Gateway, "resolve_exec", after=True)
    code, out, _ = rig.run(*PROBE)
    assert (code, out) == (128 + signal.SIGTERM, "")
    assert invokes(n) == []


@pytest.mark.parametrize("case", ["no_config", "nothing_to_probe"])
def test_a_signal_after_the_config_load_and_before_any_call_exits_128_plus_n(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from notice_route import cli

    n = rig.notifier()
    if case == "nothing_to_probe":
        rig.configure({"g": {"notifier": n, "kind": "generic"}})
    _signal_inside(monkeypatch, cli, "run_probes", after=False)
    code, out, _ = rig.run(*PROBE)
    assert (code, out) == (128 + signal.SIGTERM, "")
    assert invokes(n) == []
