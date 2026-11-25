"""``send`` end to end through the real CLI entry with fake notifiers (#22): every outcome,
exit code and output rule of docs/interface.md (send, Output, Exit codes, Log)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from notice_route.gateway import SYSTEM_TZ_DIRS
from tests.fakes import Notifier
from tests.send_support import MORNING, NOW, Rig

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database")


@pytest.fixture(autouse=True)
def private_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.delenv("NOTICE_ROUTE_HOOK", raising=False)


@pytest.fixture
def rig(
    home: Path,
    tmp_path: Path,
    make_notifier: Callable[..., Notifier],
    capsys: pytest.CaptureFixture[str],
) -> Iterator[Rig]:
    r = Rig(home, tmp_path, make_notifier, capsys)
    yield r
    r.finish()


def only(doc: dict[str, Any]) -> dict[str, Any]:
    assert len(doc["destinations"]) == 1
    return doc["destinations"][0]


def test_sent(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}})
    code, doc, err = rig.send("hello", level="warn")
    assert code == 0 and err == ""
    d = only(doc)
    assert (d["name"], d["outcome"], d["attempts"], d["code"]) == ("chat", "sent", 1, None)
    assert doc["outcome"] == "ok" and doc["reason"] is None and doc["level"] == "warn"
    assert d["notifier_outcome"] == "sent"
    invoke = n.events("invoke")
    assert len(invoke) == 1
    assert invoke[0]["argv"][-6:] == [
        "send", "--stdin-json", "--json", "--actor", "auto:notice-route",
        f"--dedupe-key={doc['dedupe']}",
    ]  # fmt: skip
    assert json.loads(n.events("stdin")[0]["text"]) == {"message": "hello", "level": "warn"}
    assert rig.query("SELECT outcome, attempts, code FROM deliveries") == [("sent", 1, None)]
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
    assert rig.query("SELECT count FROM rate") == [(1,)]
