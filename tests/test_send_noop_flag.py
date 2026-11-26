"""``send --exit-zero-on-noop`` (#43): the no-op exit 22 becomes 0, nothing else changes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.fakes import scrubbed_env
from tests.send_support import Rig
from tests.test_cli_rules_validate import schema_errors
from tests.test_send_cli import LATE_SIGNAL_SCRIPT, error_doc

pytestmark = pytest.mark.usefixtures("send_environment")

FLAG = "--exit-zero-on-noop"
ROOT = Path(__file__).resolve().parent.parent

NO_MATCH_RULES = [
    {
        "name": "only",
        "match": {"source": "other"},
        "notify": ["chat"],
        "level": "info",
        "message": "x",
    }
]
DROP_RULES = [{"name": "drop-all", "match": {"level_min": "info"}, "drop": True}]


def _unconfigured(rig: Rig) -> None:
    return None


def _disabled_router(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}}, enabled=False)


def _no_rule(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}}, rules=NO_MATCH_RULES)


def _no_rules_at_all(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}}, rules=[])


def _all_disabled(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier(), "enabled": False}})


NOOPS: dict[str, tuple[Callable[[Rig], None], str]] = {
    "unconfigured": (_unconfigured, "unconfigured"),
    "disabled": (_disabled_router, "disabled"),
    "no_rule": (_no_rule, "no_rule"),
    "no_rules_at_all": (_no_rules_at_all, "no_rule"),
    "all_disabled": (_all_disabled, "all_disabled"),
}


def same(text: str) -> str:
    """The run's own timing is the one field that differs between two runs."""
    return re.sub(r'"duration_ms":\d+', '"duration_ms":0', text)


SEND = ("send", "--stdin-json", "--json", "--source", "s")
BODY = b'{"message": "hi"}'


@pytest.mark.parametrize("name", list(NOOPS))
def test_each_no_op_exits_22_without_the_flag_and_0_with_it(rig: Rig, name: str) -> None:
    setup, reason = NOOPS[name]
    setup(rig)
    code, out, err = rig.run(*SEND, stdin=BODY)
    assert code == 22 and json.loads(out)["reason"] == reason
    code, out2, err2 = rig.run(*SEND, FLAG, stdin=BODY)
    assert code == 0
    doc = json.loads(out2)
    assert doc["outcome"] == "no_op" and doc["reason"] == reason
    assert schema_errors(doc, "send.json") == [], doc


@pytest.mark.parametrize("name", list(NOOPS))
def test_the_flag_changes_neither_stdout_nor_stderr(rig: Rig, name: str) -> None:
    setup, _ = NOOPS[name]
    setup(rig)
    code, out, err = rig.run(*SEND, stdin=BODY)
    code2, out2, err2 = rig.run(*SEND, FLAG, stdin=BODY)
    assert (code, code2) == (22, 0)
    assert same(out2) == same(out) and err2 == err  # byte for byte
    # text mode as well
    t1 = rig.run("send", "--message", "hi", "--source", "s")
    t2 = rig.run("send", "--message", "hi", "--source", "s", FLAG)
    assert (t1[0], t2[0]) == (22, 0) and same(t1[1]) == same(t2[1]) and t1[2] == t2[2]


def test_the_flag_changes_no_history_or_log_effect(rig: Rig) -> None:
    _no_rule(rig)
    rig.run(*SEND, stdin=BODY)
    rig.run(*SEND, FLAG, stdin=BODY)
    rows = rig.query("SELECT rule, destination, outcome, reason FROM deliveries ORDER BY id")
    assert rows == [(None, None, "dropped", "no_rule")] * 2
    first, second = rig.log_lines()
    for key in ("reason", "outcome", "destination", "rule", "actor"):
        assert first[key] == second[key]


def test_a_drop_rule_is_exit_0_with_or_without_the_flag(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}}, rules=DROP_RULES)
    for extra in ((), (FLAG,)):
        code, out, _ = rig.run(*SEND, *extra, stdin=BODY)
        assert code == 0 and json.loads(out)["reason"] == "rule_drop"


def test_a_notifier_answering_disabled_is_exit_0_without_the_flag_too(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier(exit=22, stdout='{"outcome":"disabled"}')}})
    for extra in ((), (FLAG,)):
        code, out, _ = rig.run(*SEND, *extra, stdin=BODY)
        doc = json.loads(out)
        assert code == 0 and doc["outcome"] == "ok"
        assert doc["destinations"][0]["outcome"] == "disabled"


def test_a_sent_run_is_exit_0_with_the_flag(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    code, out, _ = rig.run(*SEND, FLAG, stdin=BODY)
    assert code == 0 and json.loads(out)["outcome"] == "ok"


def test_a_disabled_destination_beside_a_failed_one_is_still_12(rig: Rig) -> None:
    rig.configure(
        {
            "off": {"notifier": rig.notifier(), "enabled": False},
            "bad": {"notifier": rig.notifier(error={"code": "credential_invalid"})},
        }
    )
    for extra in ((), (FLAG,)):
        code, out, _ = rig.run(*SEND, *extra, stdin=BODY)
        assert code == 12 and json.loads(out)["outcome"] == "partial"


def test_exit_2_for_a_bad_config_stays_with_the_flag(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}}, defaults={"dedup_window": "nonsense"})
    code, out, _ = rig.run(*SEND, FLAG, stdin=BODY)
    assert code == 2 and error_doc(out)["code"] == "config_invalid"


def test_exit_1_for_a_bad_state_database_stays_with_the_flag(rig: Rig, home: Path) -> None:
    _no_rule(rig)
    state = home / ".local" / "state" / "notice-route"
    state.mkdir(parents=True, mode=0o700)
    (state / "state.db").write_bytes(b"x" * 4096)
    (state / "state.db").chmod(0o600)
    code, out, _ = rig.run(*SEND, FLAG, stdin=BODY)
    assert code == 1 and error_doc(out)["code"] == "internal"


@pytest.mark.parametrize(
    ("stdin", "reason"),
    [(b'{"nope": 1}', "payload_unrecognized"), (b"not json", "payload_invalid")],
)
def test_exit_20_refusals_stay_with_the_flag(rig: Rig, stdin: bytes, reason: str) -> None:
    _no_rule(rig)
    code, out, _ = rig.run(*SEND, FLAG, stdin=stdin)
    assert code == 20 and error_doc(out)["reason"] == reason


def test_hook_recursion_stays_exit_20_with_the_flag(rig: Rig) -> None:
    _unconfigured(rig)
    gw = rig.gateway(BODY, env={"NOTICE_ROUTE_HOOK": "1"})
    code, out, _ = rig.run(*SEND, FLAG, gateway=gw)
    assert code == 20 and error_doc(out)["reason"] == "hook_recursion"


def test_a_usage_error_stays_exit_2_with_the_flag(rig: Rig) -> None:
    code, out, _ = rig.run("send", FLAG, "--actor", "root", "--message", "x", "--json")
    assert code == 2 and error_doc(out)["code"] == "usage"


# --- send --dry-run and test --------------------------------------------------------------------


@pytest.mark.parametrize("name", ["no_rule", "all_disabled", "disabled", "unconfigured"])
def test_send_dry_run_honors_the_flag_and_prints_the_same_document(rig: Rig, name: str) -> None:
    NOOPS[name][0](rig)
    argv = ("send", "--dry-run", "--stdin-json", "--json", "--source", "s")
    code, out, err = rig.run(*argv, stdin=BODY)
    code2, out2, err2 = rig.run(*argv, FLAG, stdin=BODY)
    assert (code, code2) == (22, 0) and same(out2) == same(out) and err2 == err
    assert json.loads(out2)["reason"] == NOOPS[name][1]


def test_send_dry_run_of_a_matching_rule_is_0_either_way(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    code, _, _ = rig.run("send", "--dry-run", FLAG, "--message", "x", "--source", "s")
    assert code == 0


def test_send_dry_run_with_the_flag_still_exits_1_and_20(rig: Rig, home: Path) -> None:
    _no_rule(rig)
    code, out, _ = rig.run("send", "--dry-run", FLAG, "--json", "--stdin-json", stdin=b"nope")
    assert code == 20 and error_doc(out)["code"] == "refused"
    state = home / ".local" / "state" / "notice-route"
    state.mkdir(parents=True, mode=0o700)
    (state / "state.db").write_bytes(b"x" * 4096)
    (state / "state.db").chmod(0o600)
    code, out, _ = rig.run("send", "--dry-run", FLAG, "--json", "--message", "x", "--source", "s")
    assert code == 1 and error_doc(out)["code"] == "internal"


# --- the flag is a send flag only ---------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["rules"],
        ["validate"],
        ["validate", "--probe"],
        ["test", "--message", "x"],
        ["flush"],
        ["flush", "--dry-run"],
        ["history"],
        ["prune"],
        ["cron", "install"],
        ["cron", "uninstall", "--dry-run"],
        ["cron", "status"],
    ],
)
def test_every_other_command_refuses_the_flag_as_a_usage_error(rig: Rig, argv: list[str]) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    code, out, err = rig.run(*argv, FLAG, "--json")
    e = error_doc(out)
    assert code == 2 and e["code"] == "usage" and err.startswith("notice-route: usage: ")
    code, out, err = rig.run(*argv, FLAG)
    assert code == 2 and out == ""


def test_the_flag_before_the_command_is_refused(rig: Rig) -> None:
    _no_rule(rig)
    code, out, _ = rig.run(FLAG, "send", "--message", "x", "--json")
    assert code == 2 and error_doc(out)["code"] == "usage"


def test_an_abbreviation_of_the_flag_is_refused(rig: Rig) -> None:
    _no_rule(rig)
    for abbreviated in ("--exit-zero", "--exit-zero-on", "--exit"):
        code, out, _ = rig.run(*SEND, abbreviated, stdin=BODY)
        assert code == 2 and error_doc(out)["code"] == "usage"


def test_the_flag_takes_no_value(rig: Rig) -> None:
    _no_rule(rig)
    code, out, _ = rig.run(*SEND, f"{FLAG}=1", stdin=BODY)
    assert code == 2 and error_doc(out)["code"] == "usage"


def test_the_flag_is_in_the_send_help_only(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    from notice_route.cli import main

    with pytest.raises(SystemExit):
        main(["send", "--help"])
    assert FLAG in capsys.readouterr().out
    for command in ("rules", "test", "flush", "history", "prune"):
        with pytest.raises(SystemExit):
            main([command, "--help"])
        assert FLAG not in capsys.readouterr().out


# --- --quiet, --verbose, signals ----------------------------------------------------------------


def test_quiet_and_verbose_behave_as_without_the_flag(rig: Rig) -> None:
    _unconfigured(rig)
    assert rig.run("send", "--message", "hi", "--quiet", FLAG) == (0, "", "")
    code, _, _ = rig.run("send", "--message", "hi", "--verbose", "--quiet", FLAG)
    assert code == 2


def test_a_late_signal_leaves_the_document_and_the_mapped_exit(rig: Rig) -> None:
    _no_rule(rig)
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", LATE_SIGNAL_SCRIPT, str(rig.home), str(rig.tmp_path),
         *SEND, FLAG],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=scrubbed_env(rig.home), cwd="/",
    )  # fmt: skip
    rig.procs.append(proc)
    out, _ = proc.communicate(BODY, timeout=60)
    assert proc.returncode == 0
    assert json.loads(out)["reason"] == "no_rule"


SEND_SCHEMA_SHA256 = "31d122e097e4b3a3bcaf6ec3dfe57f5138e73205662af9eae7e4ae4c8fa30fe3"


def test_the_published_send_schema_is_unchanged() -> None:
    digest = hashlib.sha256((ROOT / "schemas" / "cli" / "send.json").read_bytes()).hexdigest()
    assert digest == SEND_SCHEMA_SHA256

    def git(*argv: str) -> int:
        return subprocess.run(
            ["git", *argv], cwd=ROOT, env=scrubbed_env(ROOT), capture_output=True, timeout=30
        ).returncode

    if git("rev-parse", "--verify", "-q", "origin/main") != 0:
        pytest.skip("no origin/main to compare with; the pinned digest was checked")
    assert git("diff", "--quiet", "origin/main", "--", "schemas/cli/send.json") == 0


@pytest.mark.parametrize("argv", [["test", "--message", "x"], ["flush"]])
def test_other_commands_still_exit_22_on_a_no_op_without_the_flag(
    rig: Rig, argv: list[str]
) -> None:
    _all_disabled(rig)
    code, _, _ = rig.run(*argv, "--json")
    assert code == 22


def test_a_signal_exit_stays_128_plus_n_with_the_flag(rig: Rig) -> None:
    from notice_route.gateway import Gateway

    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}, "late": {"notifier": n, "quiet": "night"}})

    class Signalling(Gateway):
        def new_token(self) -> str:  # called inside the decision transaction
            os.kill(os.getpid(), signal.SIGTERM)
            return super().new_token()

    base = rig.gateway(b'{"message": "hi", "level": "warn"}')
    gw = Signalling(
        passwd_home=base.passwd_home,
        clock=rig.clock,
        exec_check_from=base.exec_check_from,
        stdin_fd=base.stdin_fd,
        environ=base.environ,
    )
    code, out, err = rig.run(*SEND, FLAG, gateway=gw)
    assert (code, out, err) == (128 + signal.SIGTERM, "", "")


@pytest.mark.parametrize("name", ["no_rule", "all_disabled", "disabled", "unconfigured"])
def test_send_dry_run_text_mode_is_identical_with_the_flag(rig: Rig, name: str) -> None:
    NOOPS[name][0](rig)
    argv = ("send", "--dry-run", "--message", "hi", "--source", "s")
    code, out, err = rig.run(*argv)
    code2, out2, err2 = rig.run(*argv, FLAG)
    assert (code, code2) == (22, 0) and same(out2) == same(out) and err2 == err
