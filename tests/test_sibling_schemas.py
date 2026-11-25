"""The pinned sibling schemas carry a recorded version, and an edit without a re-pin fails."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests import sibling_schemas as ss

ERROR_ENVELOPE = {
    "error": {
        "code": "rate_limited",
        "message": "rate limited",
        "reason": None,
        "details": {"http_status": 429},
        "retryable": True,
        "retry_after_s": 4,
        "unknown_outcome": False,
    }
}


def test_every_pinned_tool_records_its_version() -> None:
    for tool, version in ss.PINNED.items():
        pin = ss.read_pin(tool)
        assert pin["tool"] == tool
        assert pin["version"] == version
        assert ss.pin_dir(tool).name == version
        assert isinstance(pin["source_commit"], str) and len(pin["source_commit"]) == 40


def test_pins_match_the_files_on_disk() -> None:
    for tool in ss.PINNED:
        assert ss.pin_problems(tool) == []


def test_pinned_tools_are_exactly_the_directories_present() -> None:
    present = sorted(p.name for p in ss.ROOT.iterdir() if p.is_dir())
    assert present == sorted(ss.PINNED)
    for tool in ss.PINNED:
        assert [p.name for p in (ss.ROOT / tool).iterdir()] == [ss.PINNED[tool]]


def _copy_pin(tool: str, tmp_path: Path) -> Path:
    dest = tmp_path / tool / ss.PINNED[tool]
    shutil.copytree(ss.pin_dir(tool), dest)
    return dest


def test_editing_a_copied_file_without_updating_the_record_fails(tmp_path: Path) -> None:
    dest = _copy_pin("ha-notice", tmp_path)
    target = dest / "cli" / "send.json"
    target.write_text(target.read_text("utf-8") + "\n", "utf-8")
    problems = ss.pin_problems("ha-notice", root=tmp_path)
    assert any("cli/send.json" in p and "changed" in p for p in problems)


def test_adding_an_unrecorded_file_fails(tmp_path: Path) -> None:
    dest = _copy_pin("slack-notice", tmp_path)
    (dest / "cli" / "extra.json").write_text("{}", "utf-8")
    assert any(
        "cli/extra.json" in p and "not recorded" in p
        for p in ss.pin_problems("slack-notice", root=tmp_path)
    )


def test_deleting_a_recorded_file_fails(tmp_path: Path) -> None:
    dest = _copy_pin("slack-notice", tmp_path)
    (dest / "notification.json").unlink()
    assert any(
        "notification.json" in p and "missing" in p
        for p in ss.pin_problems("slack-notice", root=tmp_path)
    )


def test_editing_the_recorded_version_without_moving_the_directory_fails(tmp_path: Path) -> None:
    dest = _copy_pin("ha-notice", tmp_path)
    pin_path = dest / "PIN.json"
    pin = json.loads(pin_path.read_text("utf-8"))
    pin["version"] = "9.9.9"
    pin_path.write_text(json.dumps(pin), "utf-8")
    assert any("version" in p for p in ss.pin_problems("ha-notice", root=tmp_path))


def test_a_changed_authored_example_fails_too(tmp_path: Path) -> None:
    dest = _copy_pin("agent-spawn", tmp_path)
    example = next((dest / "examples").glob("*.json"))
    example.write_text(example.read_text("utf-8") + " ", "utf-8")
    assert any(example.name in p for p in ss.pin_problems("agent-spawn", root=tmp_path))


@pytest.mark.parametrize("tool", ["slack-notice", "ha-notice"])
def test_error_envelope_schema_accepts_the_documented_shape(tool: str) -> None:
    assert ss.errors(ERROR_ENVELOPE, tool, "cli/error.json") == []


@pytest.mark.parametrize("tool", ["slack-notice", "ha-notice"])
def test_error_envelope_schema_rejects_an_unknown_code_and_extra_keys(tool: str) -> None:
    bad = json.loads(json.dumps(ERROR_ENVELOPE))
    bad["error"]["code"] = "made_up"
    assert ss.errors(bad, tool, "cli/error.json")
    extra = json.loads(json.dumps(ERROR_ENVELOPE))
    extra["error"]["stack"] = "x"
    assert ss.errors(extra, tool, "cli/error.json")


def test_send_result_schema_accepts_and_rejects() -> None:
    sent = {
        "outcome": "sent",
        "tool": "slack-notice",
        "level": "info",
        "truncated": False,
        "bytes": 20,
        "duration_ms": 5,
        "http_status": 200,
        "dedupe_key": None,
    }
    assert ss.errors(sent, "slack-notice", "cli/send.json") == []
    assert ss.errors({**sent, "http_status": None}, "slack-notice", "cli/send.json")
    assert ss.errors({"outcome": "disabled"}, "slack-notice", "cli/send.json") == []
    assert ss.errors({"outcome": "sent"}, "slack-notice", "cli/send.json")


def test_notification_schema_matches_what_the_router_sends() -> None:
    body = {"title": "t", "message": "m", "level": "warn", "link": "https://example.com/x"}
    for tool in ("slack-notice", "ha-notice"):
        assert ss.errors(body, tool, "notification.json") == []
        assert ss.errors({**body, "level": "loud"}, tool, "notification.json")
        assert ss.errors({"title": "t"}, tool, "notification.json")


def test_authored_hook_examples_validate_against_the_pinned_producer_schemas() -> None:
    examples = ss.hook_examples("agent-spawn")
    assert {name for name in examples} == {
        "post-escalate",
        "post-state-change-agent",
        "post-state-change-session",
    }
    schema_for = {
        "post-escalate": "hooks/post-escalate.json",
        "post-state-change-agent": "hooks/post-state-change.json",
        "post-state-change-session": "hooks/post-state-change.json",
    }
    for name, payload in examples.items():
        assert ss.errors(payload, "agent-spawn", schema_for[name]) == [], name


def test_a_broken_hook_example_is_rejected_by_the_schema() -> None:
    payload = dict(ss.hook_examples("agent-spawn")["post-escalate"])
    del payload["event_id"]
    assert ss.errors(payload, "agent-spawn", "hooks/post-escalate.json")


def test_the_tolerated_slack_schema_defect_is_still_there() -> None:
    """When this fails, the re-pinned schema is fixed: empty KNOWN_DEFECTS."""
    usage = {
        "error": {
            "code": "usage",
            "message": "m",
            "reason": None,
            "details": None,
            "retryable": None,
            "retry_after_s": None,
            "unknown_outcome": None,
        }
    }
    assert ss.raw_errors(usage, "slack-notice", "cli/error.json")
    assert ss.errors(usage, "slack-notice", "cli/error.json") == []
    assert ss.raw_errors(usage, "ha-notice", "cli/error.json") == []
    assert {t for t, _ in ss.KNOWN_DEFECTS} == {"slack-notice"}
