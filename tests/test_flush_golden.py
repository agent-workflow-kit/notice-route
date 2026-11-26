"""The ``flush --json`` documents of the main outcomes against golden files (#23).

Each document is validated against ``schemas/cli/flush.json`` (by ``flush_support.flush``) and
compared exactly to ``tests/golden/flush/<name>.json`` with ``duration_ms`` set to 0. Rewrite the
files after a deliberate change with ``NOTICE_ROUTE_UPDATE_GOLDEN=1`` and review the diff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.flush_support import flush, seed
from tests.send_support import Rig

pytestmark = pytest.mark.usefixtures("send_environment")

GOLDEN = Path(__file__).resolve().parent / "golden" / "flush"


def check(name: str, doc: dict[str, Any]) -> None:
    text = json.dumps({**doc, "duration_ms": 0}, indent=2) + "\n"
    path = GOLDEN / f"{name}.json"
    if os.environ.get("NOTICE_ROUTE_UPDATE_GOLDEN"):
        path.write_text(text)
    assert path.read_text() == text


def test_golden_done(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    seed(rig)
    seed(rig)
    code, doc, _ = flush(rig)
    assert code == 0
    check("done", doc)


def test_golden_partial(rig: Rig) -> None:
    bad = rig.notifier("bad", error={"code": "credential_invalid"})
    rig.configure({"a": {"notifier": rig.notifier("a")}, "b": {"notifier": bad}})
    seed(rig, "a")
    seed(rig, "b")
    code, doc, _ = flush(rig)
    assert code == 12
    check("partial", doc)


def test_golden_no_op(rig: Rig) -> None:
    rig.configure(
        {
            "chat": {"notifier": rig.notifier()},
            "off": {"notifier": rig.notifier("o"), "enabled": False},
        }
    )
    code, doc, _ = flush(rig)
    assert code == 22
    check("no_op", doc)


def test_golden_skipped_states(rig: Rig) -> None:
    rig.configure(
        {
            "chat": {"notifier": rig.notifier(), "env": ["ROUTE_KEY"]},
            "off": {"notifier": rig.notifier("o"), "enabled": False},
            "quiet": {"notifier": rig.notifier("q"), "quiet": "night"},
        }
    )
    seed(rig, "quiet", level="warn")
    code, doc, _ = flush(rig)
    assert code == 0
    check("skipped_states", doc)


def test_golden_dry_run(rig: Rig) -> None:
    rig.configure({"chat": {"notifier": rig.notifier()}})
    seed(rig)
    seed(rig, kind="held")
    code, doc, _ = flush(rig, "--dry-run")
    assert code == 0
    check("dry_run", doc)
