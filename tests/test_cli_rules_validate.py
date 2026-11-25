"""The ``rules`` command and offline ``validate`` (#9): output against schemas/cli/*.json, text
and ``--json``, exit codes, ``--config``, warnings (each with a positive and a negative case),
and the promises that nothing runs, nothing is read from the env file and nothing is written."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from notice_route.cli import main
from notice_route.gateway import SYSTEM_TZ_DIRS, FakeClock, Gateway

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
NOW = 1_700_000_000.0
CANARY = "leak-canary-value"

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database")


def schema_errors(instance: Any, name: str) -> list[str]:
    registry: Registry[Any] = Registry()
    for path in SCHEMAS.rglob("*.json"):
        doc = json.loads(path.read_text())
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
    schema = json.loads((SCHEMAS / "cli" / name).read_text())
    validator = Draft202012Validator(schema, registry=registry)
    return [e.message for e in validator.iter_errors(instance)]


@pytest.fixture(autouse=True)
def private_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.delenv("CRED_ONE", raising=False)
    monkeypatch.delenv("CRED_TWO", raising=False)


@pytest.fixture
def gw(home: Path, tmp_path: Path) -> Gateway:
    """A private home, a fixed clock, and trust in everything above ``tmp_path``."""
    return Gateway(
        passwd_home=lambda: str(home),
        clock=FakeClock(wall=NOW),
        exec_check_from=str(tmp_path),
    )


def make_exe(tmp_path: Path, name: str = "notifier", mode: int = 0o700) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    bin_dir.chmod(0o700)
    path = bin_dir / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(mode)
    return str(path)


def base_config(tmp_path: Path) -> dict[str, Any]:
    exe = make_exe(tmp_path)
    return {
        "destinations": {
            "phone": {
                "kind": "ha-notice",
                "cmd": [exe, "--config", "/etc/x.yaml"],
                "floor": "warn",
                "quiet": "night",
                "env": ["CRED_ONE", "CRED_TWO"],
            },
            "chat": {"kind": "slack-notice", "cmd": [exe]},
        },
        "quiet_hours": {
            "night": {
                "timezone": "America/New_York",
                "days": ["mon", "fri"],
                "start": "22:00",
                "end": "07:00",
            }
        },
        "hooks": {"post-fail": [{"cmd": "/bin/true"}]},
        "rules": [
            {
                "name": "drop-launch",
                "match": {"source": "agent-spawn", "hook": "post-launch"},
                "drop": True,
            },
            {
                "name": "done",
                "match": {
                    "source": "agent-spawn",
                    "labels": ["k=v"],
                    "where": {"state": "reported", "n": 3},
                },
                "notify": ["phone", "chat"],
                "level": "info",
                "message": "done {{object_id}}",
                "dedup_window": "2h",
            },
            {
                "name": "direct",
                "match": {"level_min": "error"},
                "notify": ["chat"],
                "dedup_window": "90s",
            },
        ],
    }


def write_config(home: Path, config: dict[str, Any], *, name: str = "config.yaml") -> Path:
    directory = home / ".config" / "notice-route"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / name
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return path


def run(capsys: pytest.CaptureFixture[str], gw: Gateway, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv), gw)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(
    capsys: pytest.CaptureFixture[str], gw: Gateway, *argv: str
) -> tuple[int, dict[str, Any]]:
    code, out, _ = run(capsys, gw, *argv, "--json")
    assert out.endswith("\n") and out.count("\n") == 1
    return code, json.loads(out)


# --- rules -----------------------------------------------------------------------------------


def test_rules_json_golden_and_schema(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, base_config(tmp_path))
    code, doc = run_json(capsys, gw, "rules")
    assert code == 0
    assert schema_errors(doc, "rules.json") == []
    assert doc == {
        "rules": [
            {
                "name": "drop-launch",
                "match": {"source": "agent-spawn", "hook": "post-launch"},
                "destinations": [],
                "level": None,
                "drop": True,
                "pass_through": False,
                "dedup_window_s": None,
            },
            {
                "name": "done",
                "match": {
                    "source": "agent-spawn",
                    "labels": ["k=v"],
                    "where": {"state": "reported", "n": 3},
                },
                "destinations": ["phone", "chat"],
                "level": "info",
                "drop": False,
                "pass_through": False,
                "dedup_window_s": 7200,
            },
            {
                "name": "direct",
                "match": {"level_min": "error"},
                "destinations": ["chat"],
                "level": None,
                "drop": False,
                "pass_through": True,
                "dedup_window_s": 90,
            },
        ],
        "destinations": [
            {
                "name": "phone",
                "kind": "ha-notice",
                "floor": "warn",
                "quiet": "night",
                "max_per_hour": 30,
                "enabled": True,
            },
            {
                "name": "chat",
                "kind": "slack-notice",
                "floor": "info",
                "quiet": None,
                "max_per_hour": 30,
                "enabled": True,
            },
        ],
        "quiet_hours": [
            {
                "name": "night",
                "timezone": "America/New_York",
                "days": ["mon", "fri"],
                "start": "22:00",
                "end": "07:00",
            }
        ],
    }


def test_rules_lists_only_referenced_destinations_and_windows(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    config = base_config(tmp_path)
    config["rules"] = [r for r in config["rules"] if r["name"] == "direct"]
    write_config(home, config)
    _, doc = run_json(capsys, gw, "rules")
    assert [d["name"] for d in doc["destinations"]] == ["chat"]
    assert doc["quiet_hours"] == []
    assert schema_errors(doc, "rules.json") == []


def test_rules_text_output(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, base_config(tmp_path))
    code, out, err = run(capsys, gw, "rules")
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0].startswith("1. drop-launch:") and lines[0].endswith("drop")
    assert "2. done:" in lines[1] and "-> phone,chat level=info dedup=7200s" in lines[1]
    assert "pass-through" in lines[2] and "level=from notice" in lines[2]
    assert any(
        line.startswith("destination phone: ha-notice floor=warn quiet=night") for line in lines
    )
    assert any(
        line.startswith("quiet night: 22:00-07:00 America/New_York mon,fri") for line in lines
    )
    assert not json.loads(json.dumps(out)).startswith("{")


def test_rules_without_config_prints_empty_lists_and_exits_22(
    gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc = run_json(capsys, gw, "rules")
    assert code == 22
    assert doc == {"rules": [], "destinations": [], "quiet_hours": []}
    assert schema_errors(doc, "rules.json") == []


def test_rules_without_config_text_is_empty_with_one_stderr_line(
    gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, gw, "rules")
    assert (code, out) == (22, "")
    assert err.count("\n") == 1
    code, out, err = run(capsys, gw, "rules", "--quiet")
    assert (code, out, err) == (22, "", "")


def test_global_flags_work_before_and_after_the_command(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, base_config(tmp_path))
    before = run(capsys, gw, "--json", "rules")
    after = run(capsys, gw, "rules", "--json")
    assert before == after and before[0] == 0
    assert json.loads(before[1])["rules"]


def test_verbose_with_quiet_is_a_usage_error(gw: Gateway) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["rules", "--verbose", "--quiet"], gw)
    assert exc.value.code == 2


def test_example_config_and_drop_in_pass_both_commands(
    home: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = home / ".config" / "notice-route"
    (directory / "config.d").mkdir(parents=True)
    directory.chmod(0o700)
    (directory / "config.d").chmod(0o700)
    for source, dest in (
        (ROOT / "config.example.yaml", directory / "config.yaml"),
        (ROOT / "examples" / "awkit-wiring.yaml", directory / "config.d" / "awkit-wiring.yaml"),
    ):
        dest.write_bytes(source.read_bytes())
        dest.chmod(0o600)
    code, doc = run_json(capsys, gw, "rules")
    assert code == 0 and schema_errors(doc, "rules.json") == []
    assert {d["name"] for d in doc["destinations"]} <= {"phone", "team-chat", "desktop"}
    code, doc = run_json(capsys, gw, "validate")
    assert code in (0, 12)  # the example's notifier paths do not exist here
    assert schema_errors(doc, "validate.json") == []


# --- config errors and --config --------------------------------------------------------------


def test_config_error_prints_the_envelope_with_the_key(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    config = base_config(tmp_path)
    config["destinations"]["phone"]["timeout"] = CANARY
    write_config(home, config)
    for command in ("rules", "validate"):
        code, out, err = run(capsys, gw, command, "--json")
        doc = json.loads(out)
        assert code == 2
        assert schema_errors(doc, "error.json") == []
        assert doc["error"]["code"] == "config_invalid"
        assert doc["error"]["reason"] is None
        assert doc["error"]["details"] == {"key": "destinations.phone.timeout"}
        assert CANARY not in out + err
        assert "destinations.phone.timeout" in err


def test_config_error_without_json_has_empty_stdout(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    config = base_config(tmp_path)
    config["unknown_key_" + CANARY] = 1
    write_config(home, config)
    code, out, err = run(capsys, gw, "rules")
    assert (code, out) == (2, "")
    assert CANARY not in err
    assert "?" in err  # an unknown key is shown as ?


def test_unsafe_config_file_is_config_unsafe(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(home, base_config(tmp_path))
    path.chmod(0o666)
    code, doc = run_json(capsys, gw, "validate")
    assert code == 2
    assert doc["error"]["code"] == "config_unsafe"
    assert doc["error"]["details"] == {"key": "config.yaml"}
    assert schema_errors(doc, "error.json") == []


def test_unusable_home_is_config_invalid_home(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = Gateway(passwd_home=lambda: "relative/home")
    code, doc = run_json(capsys, gateway, "rules")
    assert code == 2
    assert doc["error"]["details"] == {"key": "home"}


def test_config_flag_replaces_config_yaml_and_home_is_not_consulted(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (elsewhere / "other.yaml").write_text(json.dumps(base_config(tmp_path)))
    (elsewhere / "other.yaml").chmod(0o600)
    gateway = Gateway(passwd_home=lambda: "")  # no usable home at all
    for command in ("rules",):
        code, doc = run_json(capsys, gateway, command, "--config", str(elsewhere / "other.yaml"))
        assert code == 0 and len(doc["rules"]) == 3


def test_config_flag_with_a_missing_file_is_config_invalid_key_config_flag(
    tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    for command in ("rules", "validate"):
        code, doc = run_json(capsys, gw, command, "--config", str(tmp_path / "nope.yaml"))
        assert code == 2
        assert doc["error"]["details"] == {"key": "--config"}


def test_config_flag_is_refused_before_the_command_and_by_unknown_commands(
    gw: Gateway,
) -> None:
    for argv in (["--config", "/x", "rules"], ["send", "--config", "/x"]):
        with pytest.raises(SystemExit) as exc:
            main(argv, gw)
        assert exc.value.code == 2


# --- validate --------------------------------------------------------------------------------


def test_validate_golden_and_schema(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, base_config(tmp_path))
    code, doc = run_json(capsys, gw, "validate")
    assert code == 0
    assert schema_errors(doc, "validate.json") == []
    assert doc["outcome"] == "ok" and doc["probe"] is False
    assert doc["destinations"] == [
        {
            "name": "phone",
            "cmd_ok": True,
            "env_unset": ["CRED_ONE", "CRED_TWO"],
            "probe": None,
            "surfaced_today": [],
        },
        {"name": "chat", "cmd_ok": True, "env_unset": [], "probe": None, "surfaced_today": []},
    ]
    assert doc["send_worst_case_s"] == 29
    assert doc["flush_age_s"] is None
    assert [w["code"] for w in doc["warnings"]] == ["flush_stale"]


def test_validate_text_output(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, base_config(tmp_path))
    code, out, err = run(capsys, gw, "validate")
    assert code == 0 and err == ""
    assert out.startswith("outcome: ok\n")
    assert "destination phone: cmd_ok=true env_unset=CRED_ONE,CRED_TWO" in out
    assert "warning flush_stale: " in out
    assert "send_worst_case_s: 29" in out and "last flush: never" in out


def test_env_names_set_in_the_environment_are_not_reported(
    home: Path,
    tmp_path: Path,
    gw: Gateway,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(home, base_config(tmp_path))
    environment = {"CRED_ONE": "x", "CRED_TWO": ""}
    gateway = Gateway(
        passwd_home=lambda: str(home),
        clock=FakeClock(wall=NOW),
        exec_check_from=str(tmp_path),
        environ=lambda: environment,
    )
    code, doc = run_json(capsys, gateway, "validate")
    assert doc["destinations"][0]["env_unset"] == ["CRED_TWO"]  # an empty value counts as unset


def test_a_failed_cmd_ok_check_is_exit_12_with_the_full_report(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    config = base_config(tmp_path)
    config["destinations"]["chat"]["cmd"] = [make_exe(tmp_path, "wide", 0o777)]
    write_config(home, config)
    code, doc = run_json(capsys, gw, "validate")
    assert code == 12
    assert doc["outcome"] == "failed"
    assert [d["cmd_ok"] for d in doc["destinations"]] == [True, False]
    assert schema_errors(doc, "validate.json") == []
    code, out, _ = run(capsys, gw, "validate")
    assert code == 12 and "cmd_ok=false" in out


def test_validate_without_config_is_no_op_exit_22(
    gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc = run_json(capsys, gw, "validate")
    assert code == 22
    assert doc["outcome"] == "no_op" and doc["destinations"] == []
    assert schema_errors(doc, "validate.json") == []


def test_validate_with_a_config_without_destinations_is_ok(
    home: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, {"enabled": False})
    code, doc = run_json(capsys, gw, "validate")
    assert code == 0 and doc["destinations"] == [] and doc["outcome"] == "ok"
    assert doc["send_worst_case_s"] == 19  # lock_wait 2 + 0 + 17
    assert schema_errors(doc, "validate.json") == []


def test_validate_runs_no_command_and_writes_nothing(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "ran"
    exe = Path(make_exe(tmp_path, "spy"))
    exe.write_text(f"#!/bin/sh\ntouch {marker}\n")
    env_file = tmp_path / "cron.env"
    env_file.write_text("NAME=value\n")
    env_file.chmod(0o600)
    config = base_config(tmp_path)
    config["destinations"]["chat"]["cmd"] = [str(exe)]
    config["cron"] = {"env_file": str(env_file)}
    write_config(home, config)
    _make_state(home)

    def snapshot() -> dict[str, tuple[int, int, bytes]]:
        out: dict[str, tuple[int, int, bytes]] = {}
        for p in sorted(tmp_path.rglob("*")):
            if p.is_file() and not p.is_symlink():
                st = p.stat()
                out[str(p)] = (st.st_mtime_ns, st.st_mode, p.read_bytes())
        return out

    before = snapshot()  # reads every file, so the access time is taken after it
    env_atime = env_file.stat().st_atime_ns
    code, _ = run_json(capsys, gw, "validate")
    assert code == 0
    assert env_file.stat().st_atime_ns == env_atime
    assert snapshot() == before
    assert not marker.exists()


# --- state -----------------------------------------------------------------------------------


def _make_state(
    home: Path,
    *,
    flush_last: float | None = None,
    env_missing: float | None = None,
    surfaced: list[tuple[str, str, str]] = (),  # type: ignore[assignment]
    queued: list[str] = (),  # type: ignore[assignment]
) -> None:
    directory = home / ".local" / "state" / "notice-route"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    directory.joinpath("state.db").unlink(missing_ok=True)
    conn = sqlite3.connect(directory / "state.db")
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value REAL);
        CREATE TABLE deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, destination TEXT);
        CREATE TABLE queue (id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id INTEGER);
        CREATE TABLE surfaced (
            id INTEGER PRIMARY KEY AUTOINCREMENT, destination TEXT, code TEXT, period TEXT
        );
        """
    )
    conn.execute("INSERT INTO meta VALUES ('flush_last', ?)", (flush_last,))
    conn.execute("INSERT INTO meta VALUES ('flush_env_missing', ?)", (env_missing,))
    conn.executemany("INSERT INTO surfaced (destination, code, period) VALUES (?, ?, ?)", surfaced)
    for destination in queued:
        cur = conn.execute("INSERT INTO deliveries (destination) VALUES (?)", (destination,))
        conn.execute("INSERT INTO queue (delivery_id) VALUES (?)", (cur.lastrowid,))
    conn.commit()
    conn.close()
    directory.joinpath("state.db").chmod(0o600)


def test_flush_age_and_surfaced_today_come_from_state(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, base_config(tmp_path))
    _make_state(
        home,
        flush_last=NOW - 61.7,
        surfaced=[
            ("phone", "killed", "2023-11-14"),
            ("phone", "rate_capped", "2023-11-14T21"),
            ("phone", "overflow", "2023-11-13"),
            ("chat", "expired", "2023-11-14T01"),
        ],
    )
    code, doc = run_json(capsys, gw, "validate")
    assert code == 0 and schema_errors(doc, "validate.json") == []
    assert doc["flush_age_s"] == 61
    assert doc["destinations"][0]["surfaced_today"] == ["killed", "rate_capped"]
    assert doc["destinations"][1]["surfaced_today"] == ["expired"]
    assert doc["warnings"] == []


def test_a_corrupt_state_database_is_exit_1_internal(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, base_config(tmp_path))
    directory = home / ".local" / "state" / "notice-route"
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    (directory / "state.db").write_bytes(b"garbage" * 500)
    code, out, err = run(capsys, gw, "validate", "--json")
    doc = json.loads(out)
    assert code == 1
    assert doc["error"]["code"] == "internal" and doc["error"]["details"] is None
    assert schema_errors(doc, "error.json") == []
    assert "garbage" not in out + err


def test_rules_never_reads_state(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, base_config(tmp_path))
    directory = home / ".local" / "state" / "notice-route"
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    (directory / "state.db").write_bytes(b"garbage" * 500)
    assert run(capsys, gw, "rules")[0] == 0


# --- warnings --------------------------------------------------------------------------------


def codes(doc: dict[str, Any]) -> list[str]:
    return [w["code"] for w in doc["warnings"]]


def validate_with(
    home: Path,
    tmp_path: Path,
    gw: Gateway,
    capsys: pytest.CaptureFixture[str],
    edit: Any = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = base_config(tmp_path)
    if edit is not None:
        edit(config)
    write_config(home, config)
    options: dict[str, Any] = {"flush_last": NOW - 10} | (state or {})
    _make_state(home, **options)
    code, doc = run_json(capsys, gw, "validate")
    assert code in (0, 12)
    assert schema_errors(doc, "validate.json") == []
    return doc


def test_a_clean_config_has_no_warnings(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    assert validate_with(home, tmp_path, gw, capsys)["warnings"] == []


def test_destination_unused(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    def add(config: dict[str, Any]) -> None:
        config["destinations"]["spare"] = copy.deepcopy(config["destinations"]["chat"])

    doc = validate_with(home, tmp_path, gw, capsys, add)
    assert doc["warnings"] == [
        {
            "code": "destination_unused",
            "rule": None,
            "destination": "spare",
            "message": "no rule notifies this destination",
        }
    ]
    assert "destination_unused" not in codes(validate_with(home, tmp_path, gw, capsys))


def test_level_from_notice(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    def low(config: dict[str, Any]) -> None:
        config["rules"][2]["match"]["level_min"] = "warn"

    doc = validate_with(home, tmp_path, gw, capsys, low)
    assert [(w["code"], w["rule"]) for w in doc["warnings"]] == [("level_from_notice", "direct")]
    # negative: level_min error (the base config), or a rule that sets a level
    assert "level_from_notice" not in codes(validate_with(home, tmp_path, gw, capsys))

    def set_level(config: dict[str, Any]) -> None:
        low(config)
        config["rules"][2]["level"] = "warn"

    assert "level_from_notice" not in codes(validate_with(home, tmp_path, gw, capsys, set_level))


def test_rule_unreachable(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    def shadow(config: dict[str, Any]) -> None:
        config["rules"].insert(
            1,
            {
                "name": "wide",
                "match": {"source": "agent-*", "labels": ["k=v"]},
                "notify": ["chat"],
                "level": "info",
                "message": "m",
            },
        )

    doc = validate_with(home, tmp_path, gw, capsys, shadow)
    assert [(w["code"], w["rule"]) for w in doc["warnings"]] == [("rule_unreachable", "done")]
    assert "rule_unreachable" not in codes(validate_with(home, tmp_path, gw, capsys))


@pytest.mark.parametrize(
    ("earlier", "later", "unreachable"),
    [
        ({}, {"source": "a"}, True),
        ({"source": "a"}, {"source": "a"}, True),
        ({"source": "a*"}, {"source": "abc"}, True),
        ({"source": "a*"}, {"source": "abc*"}, True),
        ({"source": "*"}, {"source": "x", "hook": "h"}, True),
        ({"source": "abc"}, {"source": "a*"}, False),
        ({"source": "a"}, {"hook": "a"}, False),
        ({"source": "a"}, {}, False),
        ({"labels": ["a=1"]}, {"labels": ["a=1", "b=2"]}, True),
        ({"labels": ["a=1", "b=2"]}, {"labels": ["a=1"]}, False),
        ({"where": {"n": 1}}, {"where": {"n": 1, "m": "x"}}, True),
        ({"where": {"n": 1}}, {"where": {"n": True}}, False),
        ({"where": {"n": 1}}, {"where": {"n": "1"}}, False),
        ({"level_min": "warn"}, {"level_min": "warn"}, True),
        ({"level_min": "warn"}, {"level_min": "error"}, False),
    ],
)
def test_rule_unreachable_table(
    home: Path,
    tmp_path: Path,
    gw: Gateway,
    capsys: pytest.CaptureFixture[str],
    earlier: dict[str, Any],
    later: dict[str, Any],
    unreachable: bool,
) -> None:
    def rules(config: dict[str, Any]) -> None:
        extra = {"level": "error", "notify": ["chat"]}
        first = {"name": "first", "match": earlier, **extra}
        second = {"name": "second", "match": later, **extra}
        for rule in (first, second):
            if "level_min" not in rule["match"]:
                rule["message"] = "m"
            else:
                rule.pop("level")
                rule["level"] = "warn"
        config["rules"] = [first, second]

    doc = validate_with(home, tmp_path, gw, capsys, rules)
    assert ("rule_unreachable" in codes(doc)) is unreachable


def test_worst_case_over_hook_timeout(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    def slow(config: dict[str, Any]) -> None:
        config["destinations"]["chat"]["timeout"] = "20s"

    doc = validate_with(home, tmp_path, gw, capsys, slow)
    assert doc["send_worst_case_s"] == 39
    assert codes(doc) == ["worst_case_over_hook_timeout"]

    def edge(config: dict[str, Any]) -> None:
        config["destinations"]["chat"]["timeout"] = "11s"  # 2 + 11 + 17 = 30: not above

    doc = validate_with(home, tmp_path, gw, capsys, edge)
    assert doc["send_worst_case_s"] == 30 and codes(doc) == []

    def lock(config: dict[str, Any]) -> None:
        config["lock_wait"] = "1.5s"
        config["destinations"]["chat"]["timeout"] = "11.6s"

    doc = validate_with(home, tmp_path, gw, capsys, lock)
    assert doc["send_worst_case_s"] == pytest.approx(30.1)
    assert codes(doc) == ["worst_case_over_hook_timeout"]


@pytest.mark.parametrize(
    ("age", "stale"), [(None, True), (10, False), (300, False), (301, True), (-50, False)]
)
def test_flush_stale(
    home: Path,
    tmp_path: Path,
    gw: Gateway,
    capsys: pytest.CaptureFixture[str],
    age: int | None,
    stale: bool,
) -> None:
    flush_last = None if age is None else NOW - age
    doc = validate_with(home, tmp_path, gw, capsys, state={"flush_last": flush_last})
    assert ("flush_stale" in codes(doc)) is stale
    assert doc["flush_age_s"] == (None if age is None else max(0, age))


def test_flush_env_missing(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = validate_with(home, tmp_path, gw, capsys, state={"env_missing": NOW - 30})
    assert codes(doc) == ["flush_env_missing"]
    assert codes(validate_with(home, tmp_path, gw, capsys)) == []


def test_env_file_unsafe(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "cron.env"
    env_file.write_text("NAME=value\n")
    env_file.chmod(0o600)

    def point(config: dict[str, Any]) -> None:
        config["cron"] = {"env_file": str(env_file)}

    assert codes(validate_with(home, tmp_path, gw, capsys, point)) == []
    env_file.chmod(0o640)
    doc = validate_with(home, tmp_path, gw, capsys, point)
    assert [(w["code"], w["message"]) for w in doc["warnings"]] == [
        ("env_file_unsafe", "cron.env_file is missing or fails the safety check")
    ]
    assert str(env_file) not in json.dumps(doc)
    env_file.unlink()
    assert codes(validate_with(home, tmp_path, gw, capsys, point)) == ["env_file_unsafe"]
    # no cron.env_file: nothing to check
    assert "env_file_unsafe" not in codes(validate_with(home, tmp_path, gw, capsys))


def test_alert_unrouted(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_hook(config: dict[str, Any]) -> None:
        del config["hooks"]

    doc = validate_with(home, tmp_path, gw, capsys, no_hook)
    assert codes(doc) == ["alert_unrouted"]  # no rule can route a post-fail payload

    def with_catch_all(config: dict[str, Any]) -> None:
        no_hook(config)
        config["rules"].append(
            {
                "name": "alerts",
                "match": {"source": "notice-route"},
                "notify": ["chat"],
                "level": "error",
                "message": "m",
            }
        )

    assert "alert_unrouted" not in codes(validate_with(home, tmp_path, gw, capsys, with_catch_all))

    def drop_only(config: dict[str, Any]) -> None:
        no_hook(config)
        config["rules"].append(
            {"name": "alerts", "match": {"source": "notice-route"}, "drop": True}
        )

    assert "alert_unrouted" in codes(validate_with(home, tmp_path, gw, capsys, drop_only))

    def wrong_hook(config: dict[str, Any]) -> None:
        no_hook(config)
        config["rules"].append(
            {
                "name": "alerts",
                "match": {"hook": "post-merge"},
                "notify": ["chat"],
                "level": "error",
                "message": "m",
            }
        )

    assert "alert_unrouted" in codes(validate_with(home, tmp_path, gw, capsys, wrong_hook))

    def prefix_hook(config: dict[str, Any]) -> None:
        no_hook(config)
        config["rules"].append(
            {
                "name": "alerts",
                "match": {"hook": "post-*"},
                "notify": ["chat"],
                "level": "error",
                "message": "m",
            }
        )

    assert "alert_unrouted" not in codes(validate_with(home, tmp_path, gw, capsys, prefix_hook))
    # a post-fail hook alone satisfies it
    assert "alert_unrouted" not in codes(validate_with(home, tmp_path, gw, capsys))


def test_orphaned_queue(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = validate_with(home, tmp_path, gw, capsys, state={"queued": ["gone", "gone", "chat"]})
    assert doc["warnings"] == [
        {
            "code": "orphaned_queue",
            "rule": None,
            "destination": "gone",
            "message": "2 queued rows belong to a destination that is not configured",
        }
    ]
    assert "orphaned_queue" not in codes(
        validate_with(home, tmp_path, gw, capsys, state={"queued": ["chat", "phone"]})
    )


def test_orphaned_queue_with_an_odd_destination_name_is_reported_without_it(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = validate_with(home, tmp_path, gw, capsys, state={"queued": ["Not A Name\x1b[31m"]})
    (warning,) = doc["warnings"]
    assert warning["destination"] is None
    assert "\x1b" not in json.dumps(doc)


def test_warning_text_in_the_terminal_is_escaped(
    home: Path, tmp_path: Path, gw: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    config = base_config(tmp_path)
    config["rules"][0]["match"]["source"] = "café\x1b[31m"
    write_config(home, config)
    code, out, _ = run(capsys, gw, "rules")
    assert code == 0
    assert "\x1b" not in out and "\\xe9" in out


def test_every_warning_code_is_covered() -> None:
    schema = json.loads((SCHEMAS / "cli" / "validate.json").read_text())
    enum = set(schema["properties"]["warnings"]["items"]["properties"]["code"]["enum"])
    source = Path(__file__).read_text()
    for code in enum:
        assert f'"{code}"' in source or f"'{code}'" in source or code in source
