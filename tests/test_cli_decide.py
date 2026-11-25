"""``test`` and ``send --dry-run`` with state (#18): the quiet, redelivery, repeat, env_missing and
rate_capped decisions read ``state.db`` read-only, and the database stays byte for byte the same."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from notice_route.decide import decide_and_record
from notice_route.gateway import SYSTEM_TZ_DIRS
from notice_route.store import open_store
from tests.decide_support import dest, gateway, make_config, notice
from tests.test_cli_rules_validate import make_exe, schema_errors, write_config
from tests.test_cli_test import Invoker

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database")

NOW = 1_700_000_000  # 22:13:20 UTC, inside the window 22:00 to 06:00
MORNING = 1_700_028_000
NEXT_HOUR = 1_700_002_800


@pytest.fixture(autouse=True)
def private_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.delenv("NOTICE_ROUTE_HOOK", raising=False)


@pytest.fixture
def inv(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Iterator[Invoker]:
    invoker = Invoker(home, tmp_path, capsys)
    yield invoker
    for fd in invoker.fds:
        os.close(fd)


@pytest.fixture
def configured(home: Path, tmp_path: Path) -> None:
    exe = make_exe(tmp_path)
    write_config(
        home,
        {
            "quiet_hours": {"night": {"timezone": "UTC", "start": "22:00", "end": "06:00"}},
            "destinations": {
                "chat": {"kind": "generic", "cmd": [exe], "max_per_hour": 1},
                "late": {"kind": "generic", "cmd": [exe], "quiet": "night"},
                "needs": {"kind": "generic", "cmd": [exe], "env": ["ROUTE_VAR"]},
            },
            "rules": [
                {
                    "name": "all",
                    "match": {"level_min": "info"},
                    "notify": ["chat", "late", "needs"],
                    "level": "warn",
                    "message": "hello",
                },
                {
                    "name": "second",
                    "match": {"source": "other"},
                    "notify": ["chat"],
                    "level": "warn",
                    "message": "different",
                },
            ],
        },
    )


ARGV = ("test", "--message", "hi", "--source", "src")


def decisions(doc: dict[str, Any]) -> dict[str, tuple[str, str | None, int | None]]:
    return {
        d["name"]: (d["decision"], d["reason"], d["deliver_after"]) for d in doc["destinations"]
    }


def state_bytes(home: Path) -> dict[str, bytes]:
    root = home / ".local" / "state" / "notice-route"
    return {p.name: p.read_bytes() for p in sorted(root.glob("*"))} if root.exists() else {}


def seed(home: Path, doc: dict[str, Any], *, message: str | None = None) -> None:
    """Record a real ``send`` of the notification ``doc`` describes to ``chat`` and ``late``."""
    gw = gateway(home, NOW)
    n = notice("warn", message or doc["message"], title=doc["title"] or "", source="src", hook=None)
    if message is None:
        assert n.dedupe == doc["dedupe"]
    cfg = make_config(dest("chat", max_per_hour=1), dest("late", quiet="night"))
    with open_store(gw) as store:
        decide_and_record(store, gw, cfg, n, ["chat", "late"])


@pytest.mark.parametrize("prefix", [("test",), ("send", "--dry-run")])
def test_no_state_gives_the_decisions_of_the_config_and_the_clock(
    inv: Invoker, configured: None, prefix: tuple[str, ...]
) -> None:
    code, doc = inv.run_json(*prefix, "--message", "hi", "--source", "src")
    assert code == 0 and schema_errors(doc, "test.json") == []
    assert decisions(doc) == {
        "chat": ("send", None, None),
        "late": ("defer", "quiet", MORNING),
        "needs": ("defer", "env_missing", NOW),
    }


def test_a_set_variable_lets_the_destination_send(inv: Invoker, configured: None) -> None:
    _, doc = inv.run_json(*ARGV, env={"ROUTE_VAR": "x"})
    assert decisions(doc)["needs"] == ("send", None, None)
    _, doc = inv.run_json(*ARGV, env={"ROUTE_VAR": ""})
    assert decisions(doc)["needs"][1] == "env_missing"


def test_state_makes_a_repeat_suppressed_and_reads_the_cap(
    inv: Invoker, configured: None, home: Path
) -> None:
    _, doc = inv.run_json(*ARGV)
    seed(home, doc)
    before = state_bytes(home)
    code, again = inv.run_json(*ARGV)
    assert code == 0 and schema_errors(again, "test.json") == []
    got = decisions(again)
    assert got["chat"] == ("suppress", "repeat", None)
    assert got["late"] == ("suppress", "repeat", None)
    assert got["needs"][1] == "env_missing"
    # another text: the hourly cap of chat (1) is reached by the seeded send
    _, other = inv.run_json("test", "--message", "x", "--source", "other")
    assert decisions(other)["chat"] == ("defer", "rate_capped", NEXT_HOUR)
    assert state_bytes(home) == before


def test_text_output_shows_deliver_after(inv: Invoker, configured: None) -> None:
    _, out, _ = inv.run(*ARGV)
    assert f"destination late: defer (quiet) deliver_after={MORNING}" in out
    assert "destination chat: send\n" in out


@pytest.mark.parametrize("prefix", [("test",), ("send", "--dry-run")])
def test_the_database_is_byte_for_byte_unchanged(
    inv: Invoker, configured: None, home: Path, prefix: tuple[str, ...]
) -> None:
    _, doc = inv.run_json(*ARGV)
    seed(home, doc)
    before = state_bytes(home)
    assert "state.db" in before
    for message in ("hi", "other", "third"):
        for as_json in (False, True):
            inv.run(
                *prefix, "--message", message, "--source", "src", *(["--json"] if as_json else [])
            )
    assert state_bytes(home) == before


def test_no_database_is_not_created(inv: Invoker, configured: None, home: Path) -> None:
    inv.run(*ARGV)
    assert not (home / ".local").exists()


@pytest.mark.parametrize("prefix", [("test",), ("send", "--dry-run")])
def test_a_database_that_cannot_be_opened_is_exit_1_with_nothing_printed_but_the_error(
    inv: Invoker, configured: None, home: Path, prefix: tuple[str, ...]
) -> None:
    state = home / ".local" / "state" / "notice-route"
    state.mkdir(parents=True)
    state.chmod(0o700)
    db = state / "state.db"
    db.write_bytes(b"garbage" * 500)
    db.chmod(0o600)
    before = db.read_bytes()
    code, out, err = inv.run(*prefix, "--message", "hi", "--source", "src", "--json")
    assert code == 1
    assert json.loads(out)["error"]["code"] == "internal"
    assert "internal" in err
    assert db.read_bytes() == before


def test_a_foreign_database_is_exit_1(inv: Invoker, configured: None, home: Path) -> None:
    import sqlite3

    state = home / ".local" / "state" / "notice-route"
    state.mkdir(parents=True)
    state.chmod(0o700)
    db = state / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    db.chmod(0o600)
    code, _, _ = inv.run(*ARGV)
    assert code == 1


def test_an_empty_database_file_reads_as_no_database(
    inv: Invoker, configured: None, home: Path
) -> None:
    state = home / ".local" / "state" / "notice-route"
    state.mkdir(parents=True)
    state.chmod(0o700)
    db = state / "state.db"
    db.write_bytes(b"")
    db.chmod(0o600)
    code, doc = inv.run_json(*ARGV)
    assert code == 0 and decisions(doc)["chat"][0] == "send"
    assert db.read_bytes() == b""


def test_a_drop_rule_or_no_rule_never_opens_the_database(
    inv: Invoker, configured: None, home: Path
) -> None:
    state = home / ".local" / "state" / "notice-route"
    state.mkdir(parents=True)
    state.chmod(0o700)
    (state / "state.db").write_bytes(b"garbage")
    (state / "state.db").chmod(0o600)
    code, _, _ = inv.run("test", "--message", "x", "--source", "src", "--label", "bad label")
    assert code == 20  # refused before any state is needed
