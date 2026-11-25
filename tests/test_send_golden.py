"""The ``send --json`` documents of the main outcomes against golden files (#22).

Each document is validated against ``schemas/cli/send.json`` (by ``Rig.send``) and compared
exactly to ``tests/golden/send/<name>.json`` after the one volatile field, ``duration_ms``, is
set to 0. To rewrite the files after a deliberate change, run the tests with
``NOTICE_ROUTE_UPDATE_GOLDEN=1`` and review the diff.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from notice_route import cli, send
from notice_route.gateway import SYSTEM_TZ_DIRS
from tests.send_support import Rig
from tests.test_send_outcomes import LINK_BODY, LINK_RULE, LINKS

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = [
    pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database"),
    pytest.mark.usefixtures("send_environment"),
]

GOLDEN = Path(__file__).resolve().parent / "golden" / "send"


def check(name: str, doc: dict[str, Any]) -> None:
    doc = {**doc, "duration_ms": 0}
    text = json.dumps(doc, indent=2) + "\n"
    path = GOLDEN / f"{name}.json"
    if os.environ.get("NOTICE_ROUTE_UPDATE_GOLDEN"):
        path.write_text(text)
    assert path.read_text() == text


def test_golden_sent(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    check("sent", rig.send("hello", level="warn")[1])


def test_golden_partial(rig: Rig) -> None:
    good, bad = rig.notifier("good"), rig.notifier("bad", error={"code": "credential_invalid"})
    rig.configure({"a": {"notifier": good}, "b": {"notifier": bad}})
    code, doc, _ = rig.send("hello")
    assert code == 12
    check("partial", doc)


def test_golden_no_op_dropped_by_no_rule(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}}, rules=[])
    code, doc, _ = rig.send("hello")
    assert code == 22
    check("no_rule", doc)


def test_golden_queued_retry(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier(error={"code": "rate_limited"})}})
    check("queued_retry", rig.send("hello")[1])


def test_golden_lost_claim(rig: Rig) -> None:
    n = rig.notifier(sleep=0.5, error={"code": "rate_limited"})
    rig.configure({"chat": {"notifier": n}})

    def steal() -> None:
        n.wait_for("invoke")
        conn = sqlite3.connect(rig.state / "state.db", isolation_level=None)
        conn.execute("UPDATE queue SET claimed_by = 'another-process'")
        conn.close()

    thread = threading.Thread(target=steal)
    thread.start()
    try:
        code, doc, _ = rig.send("hello")
    finally:
        thread.join()
    assert code == 0
    check("lost_claim", doc)


def test_golden_retries_exhausted(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = send.run_send
    seams = send.Seams(retry_schedule=lambda kind, now: lambda attempts: None)
    monkeypatch.setattr(cli, "run_send", lambda *a, **k: real(*a, **{**k, "seams": seams}))
    rig.configure({"chat": {"notifier": rig.notifier(error={"code": "rate_limited"})}})
    code, doc, _ = rig.send("hello")
    assert code == 12
    check("retries_exhausted", doc)


def test_golden_link_refused_queued(rig: Rig) -> None:
    n = rig.notifier(error={"code": "refused", "reason": "link_scheme"})
    rig.configure({"chat": {"notifier": n}}, rules=[LINK_RULE], links=LINKS)
    code, out, _ = rig.run("send", "--stdin-json", "--json", stdin=LINK_BODY)
    assert code == 0
    check("link_refused", json.loads(out))
