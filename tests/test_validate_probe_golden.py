"""``validate --probe --json`` documents against golden files (#29), each validated against
``schemas/cli/validate.json``. Rewrite the files after a deliberate change with
``NOTICE_ROUTE_UPDATE_GOLDEN=1`` and review the diff."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.send_support import Rig
from tests.test_cli_rules_validate import schema_errors

pytestmark = pytest.mark.usefixtures("send_environment")

GOLDEN = Path(__file__).resolve().parent / "golden" / "validate_probe"


def check(rig: Rig, name: str, code: int, env: dict[str, str] | None = None) -> None:
    got, out, _ = rig.run("validate", "--probe", "--json", env=env)
    doc: dict[str, Any] = json.loads(out)
    assert got == code
    assert schema_errors(doc, "validate.json") == []
    text = json.dumps(doc, indent=2) + "\n"
    path = GOLDEN / f"{name}.json"
    if os.environ.get("NOTICE_ROUTE_UPDATE_GOLDEN"):
        path.write_text(text)
    assert path.read_text() == text


def test_golden_ok_disabled_and_skipped(rig: Rig) -> None:
    rig.configure(
        {
            "chat": {"notifier": rig.notifier("chat")},
            "phone": {"notifier": rig.notifier("phone", tool="ha-notice"), "kind": "ha-notice"},
            "off": {"notifier": rig.notifier("off", disabled=True)},
            "plain": {"notifier": rig.notifier("plain"), "kind": "generic"},
            "nocred": {"notifier": rig.notifier("nocred"), "env": ["CRED_ONE"]},
        }
    )
    check(rig, "ok", 0)


def test_golden_failed(rig: Rig) -> None:
    bad = rig.notifier("bad", error={"code": "credential_invalid"})
    rig.configure({"good": {"notifier": rig.notifier("good")}, "bad": {"notifier": bad}})
    check(rig, "failed", 12)


def test_golden_no_config(rig: Rig) -> None:
    check(rig, "no_config", 22)
