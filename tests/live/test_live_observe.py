"""Observation tests: the real notifiers called directly, never with a real send.

Everything here uses ``validate`` (without ``--test-message``) or ``send --dry-run``, which the
notifiers document as reading no credential and sending nothing. Each test records what the real
tool did into the report (for the person to compare with ``tests/fakebin/FIDELITY.md``) and
asserts only what the router relies on. Items: see FIDELITY.md, "Unverified".
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import pytest

from tests import sibling_schemas
from tests.live.support import DRY_SEND, Report, Result, ToolConfig, run_tool

ALL = ["slack-notice", "ha-notice", "wsl-notice"]
PAYLOAD = {"title": "notice-route live test", "message": "TEST MESSAGE (dry run, nothing is sent)"}


def dry(cfg: ToolConfig, payload: Any = None, *, before: list[str] | None = None,
        args: tuple[str, ...] = DRY_SEND) -> Result:  # fmt: skip
    body = payload if isinstance(payload, bytes) else json.dumps(payload or PAYLOAD).encode()
    return run_tool(cfg, args, os.environ, stdin=body, before=before)


def brief(r: Result, report: Report) -> dict[str, Any]:
    """What is worth recording of a run: code, stdout (parsed when it is JSON), stderr start.
    Text is redacted before it is cut. The line count and trailing newline are observations."""
    try:
        out: Any = json.loads(r.out)
    except ValueError:
        out = report.clip(r.out, 400)
    return {"code": r.code, "stdout": out, "stdout_ends_newline": r.out.endswith("\n"),
            "stdout_lines": r.out.count("\n"), "stderr": report.clip(r.err, 400)}  # fmt: skip


@pytest.fixture
def ready(make_tool: Callable[..., ToolConfig], request: pytest.FixtureRequest,
          report: Report) -> ToolConfig:  # fmt: skip
    """The notifier, after a dry run proved it has a loadable config and is enabled (else a skip,
    recorded in the report: a disabled config is a missing prerequisite)."""
    cfg = make_tool(request.param)
    base = dry(cfg)
    if base.code == 2 and '"config_invalid"' in base.out:
        report.record(f"prerequisite/{cfg.name}", skipped="config_invalid", code=base.code)
        pytest.skip(f"{cfg.name} has no loadable config (set {cfg.spec.config_var})")
    if base.code == 22 and '"disabled"' in base.out:
        report.record(f"prerequisite/{cfg.name}", skipped="disabled", code=base.code)
        pytest.skip(f"{cfg.name} is disabled in its config (enabled: false); enable it for the run")
    if base.code == 12 and cfg.name == "wsl-notice":
        report.record(f"prerequisite/{cfg.name}", skipped="cannot_run", code=base.code)
        pytest.skip("wsl-notice cannot run here (not WSL?)")
    return cfg


@pytest.mark.parametrize("ready", ALL, indirect=True)
def test_observe_dry_run_stdout_shape(ready: ToolConfig, report: Report) -> None:
    """FIDELITY: stdout is strict JSON; its line count and trailing newline are recorded (the
    router does not rely on them); the real success object against the pinned schema
    (slack-notice's pin is stale, so its drift is only recorded)."""
    r = dry(ready)
    report.record(f"dry_run_shape/{ready.name}", **brief(r, report))
    assert r.code == 0, f"dry run exit code {r.code}"
    doc = json.loads(r.out)
    assert isinstance(doc, dict)
    pin = ready.spec.pinned
    if pin is not None:
        problems = sibling_schemas.raw_errors(doc, pin, "cli/send.json")
        report.record(f"dry_run_schema/{ready.name}", pinned=sibling_schemas.PINNED[pin],
                      problems=problems)  # fmt: skip
        if pin == "ha-notice":
            assert problems == []


@pytest.mark.parametrize("ready", ALL, indirect=True)
def test_observe_usage_errors(ready: ToolConfig, report: Report) -> None:
    """FIDELITY: exit code and texts of usage errors; the error envelope is strict JSON (its
    line count is recorded, not asserted). Every case runs with a payload that cannot send:
    a dry run, or empty stdin."""
    cases = {
        "unknown_flag": (*DRY_SEND, "--no-such-flag"),
        "missing_subcommand": ("--json",),
        "bare_double_dash": ("send", "--", "--dry-run"),  # not a dry run: stdin is empty below
        "json_with_value": ("send", "--dry-run", "--stdin-json", "--json=1"),
    }
    for label, args in cases.items():
        body = b"" if label == "bare_double_dash" else json.dumps(PAYLOAD).encode()
        r = run_tool(ready, args, os.environ, stdin=body)
        report.record(f"usage/{ready.name}/{label}", **brief(r, report))
    r = run_tool(ready, (*DRY_SEND, "--no-such-flag"), os.environ, stdin=b"{}")
    assert r.code == 2
    doc = json.loads(r.out)
    assert doc["error"]["code"] == "usage"


@pytest.mark.parametrize("ready", ALL, indirect=True)
def test_observe_flag_positioning(ready: ToolConfig, report: Report) -> None:
    """FIDELITY: flags before the subcommand (the router's shape), after it, on both sides, and
    abbreviations. The router relies on: its own shape works, abbreviations are refused."""
    cfg_args = ["--config", ready.config] if ready.config else []
    shape = {
        "router_shape": (cfg_args, DRY_SEND),
        "actor_before_subcommand": ([*cfg_args, "--actor", "auto:notice-route"],
                                    ("send", "--dry-run", "--stdin-json", "--json")),
        "config_after_subcommand": ([], ("send", *cfg_args, "--dry-run", "--stdin-json", "--json")),
        "config_both_sides": (cfg_args, ("send", *cfg_args, "--dry-run", "--stdin-json", "--json")),
        "abbreviated_dry_run": (cfg_args, ("send", "--dry", "--stdin-json", "--json")),
        "abbreviated_stdin": (cfg_args, ("send", "--dry-run", "--stdin", "--json")),
    }  # fmt: skip
    seen: dict[str, Result] = {}
    for label, (before, args) in shape.items():
        seen[label] = run_tool(ready, args, os.environ, stdin=json.dumps(PAYLOAD).encode(),
                               before=before)  # fmt: skip
        report.record(f"flags/{ready.name}/{label}", **brief(seen[label], report))
    assert seen["router_shape"].code == 0
    assert seen["abbreviated_dry_run"].code == 2
    assert seen["abbreviated_stdin"].code == 2


EMPTYISH = {
    "zero_width_only": "​⁠﻿",
    "controls_only": "\x01\x02\x7f",
    "whitespace_only": " \t\r\n ",
    "combining_only": "́̂",
    "variation_selectors_only": "️︎",
}
KEPT = {
    "nul_inside": "a\x00b",
    "bidi_override": "‮abc‬",
    "zwj_emoji": "\U0001f469‍\U0001f469‍\U0001f467",
    "long_5000": "x" * 5000,
    "astral": "\U0001f600" * 300,
}
KEYWORDS = ["clear_notification", "TTS", "command_screen_on", " request_location_update "]


@pytest.mark.parametrize("ready", ALL, indirect=True)
def test_observe_sanitizer(ready: ToolConfig, report: Report) -> None:
    """FIDELITY: empty-message results beyond the tables, `bytes` and `truncated`, and for
    ha-notice the command keywords. The router relies on: an empty-after-sanitizing message is a
    refusal (exit 20), and for ha-notice a keyword refusal is exit 20 `command_keyword`."""
    empties = {label: dry(ready, {"message": text}) for label, text in EMPTYISH.items()}
    for label, r in empties.items():
        report.record(f"sanitize/{ready.name}/{label}", **brief(r, report))
    for label, text in KEPT.items():
        report.record(
            f"sanitize/{ready.name}/{label}", **brief(dry(ready, {"message": text}), report)
        )
    words = {word: dry(ready, {"message": word}) for word in KEYWORDS}
    for word, r in words.items():
        report.record(f"keyword/{ready.name}/{word.strip()}", **brief(r, report))
    # Everything is recorded; only now is anything asserted.
    for label, r in empties.items():
        assert r.code == 20, f"{label}: exit code {r.code}, expected 20"
    if ready.name == "ha-notice":
        for word, r in words.items():
            assert r.code == 20, f"keyword {word.strip()}: exit code {r.code}, expected 20"
            assert json.loads(r.out)["error"]["reason"] == "command_keyword"


@pytest.mark.parametrize("ready", ALL, indirect=True)
def test_observe_config_checked_before_input(ready: ToolConfig, report: Report,
                                              scratch: Any) -> None:  # fmt: skip
    """FIDELITY: the order of config loading and input reading. An unreadable config path with a
    broken payload: which error comes first (the router assumes `config_invalid`, exit 2)."""
    missing = str(scratch / "no-such-config.yaml")
    r = run_tool(ready, DRY_SEND, os.environ, stdin=b"{not json", before=["--config", missing])
    report.record(f"order/{ready.name}/config_vs_input", **brief(r, report))
    assert r.code == 2
    assert json.loads(r.out)["error"]["code"] == "config_invalid"


@pytest.mark.parametrize("ready", ALL, indirect=True)
def test_observe_closed_stdin_and_oversize(ready: ToolConfig, report: Report) -> None:
    """FIDELITY: empty stdin, and input over 1 MiB (the router writes up to the cap). The input
    bound is tested with a valid short message padded with JSON whitespace past 1048576 bytes
    (asserts exit 20); an unpadded oversize message (refused for its length, not the bound) is
    recorded separately."""
    report.record(f"stdin/{ready.name}/empty", **brief(dry(ready, b""), report))
    unpadded = json.dumps({"message": "x" * (1 << 20)}).encode()
    report.record(f"stdin/{ready.name}/oversize_message", **brief(dry(ready, unpadded), report))
    padded = b'{"message":"x"}' + b" " * ((1 << 20) + 1)
    assert len(padded) > 1 << 20
    r = dry(ready, padded)
    report.record(f"stdin/{ready.name}/padded_over_1MiB", **brief(r, report))
    assert r.code == 20, f"padded input over 1 MiB: exit code {r.code}, expected 20"
