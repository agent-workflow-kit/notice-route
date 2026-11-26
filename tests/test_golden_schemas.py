"""Every ``--json`` golden file under ``tests/golden/`` validates against its schema in
``schemas/`` (#30). A new golden directory fails until it is mapped here."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_cli_rules_validate import schema_errors

GOLDEN = Path(__file__).resolve().parent / "golden"
# golden directory -> schema under schemas/cli/ (``cron`` holds crontab text, not JSON).
SCHEMA_OF = {
    "destination_disable": "destination-disable.json",
    "destination_enable": "destination-enable.json",
    "destination_list": "destination-list.json",
    "flush": "flush.json",
    "history": "history.json",
    "queue_drop": "queue-drop.json",
    "queue_list": "queue-list.json",
    "queue_show": "queue-show.json",
    "send": "send.json",
    "validate_probe": "validate.json",
}
FILES = sorted(p for p in GOLDEN.rglob("*.json"))


def test_every_golden_json_directory_is_mapped() -> None:
    assert {p.parent.name for p in FILES} <= set(SCHEMA_OF)
    assert FILES


@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_golden_file_matches_its_schema(path: Path) -> None:
    # An empty-array golden (history/none.json) proves well-formedness only: it cannot fail.
    document = json.loads(path.read_text(encoding="utf-8"))
    assert schema_errors(document, SCHEMA_OF[path.parent.name]) == []


def test_the_golden_check_can_fail() -> None:
    assert schema_errors({"not": "a send document"}, "send.json") != []
