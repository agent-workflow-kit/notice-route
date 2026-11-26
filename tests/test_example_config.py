"""The example config and the drop-in, checked as CI does (#30): they validate as one config
offline, and every shipped rule renders against the payloads it must match with no missing or
excluded token. The negative tests prove the check can fail."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from notice_route.gateway import SYSTEM_TZ_DIRS
from tests import example_check
from tests import sibling_schemas as ss
from tests.example_check import ROOT, SHIPPED_CONFIG, SHIPPED_DROP_IN, check, validate_failure
from tests.example_payloads import Example, examples
from tests.test_cli_rules_validate import SCHEMAS

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and os.environ.get("CI"):
    pytest.fail("CI needs a system time zone database", pytrace=False)
pytestmark = pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database")

CONFIG = SHIPPED_CONFIG.read_text(encoding="utf-8")
DROP_IN = SHIPPED_DROP_IN.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def private_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.delenv("NOTICE_ROUTE_HOOK", raising=False)


def private_dir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def mutate(text: str, old: str, new: str) -> str:
    assert text.count(old) >= 1, old
    return text.replace(old, new, 1)


def run_check(
    tmp_path: Path,
    config: str = CONFIG,
    drop_in: str = DROP_IN,
    given: list[Example] | None = None,
) -> list[str]:
    return check(tmp_path, config, drop_in, given if given is not None else examples())


def test_the_shipped_example_and_drop_in_pass_the_check(tmp_path: Path) -> None:
    assert run_check(tmp_path) == []


def test_pinned_producer_examples_carry_a_recorded_version() -> None:
    pin = ss.read_pin("agent-spawn")
    assert pin["version"] == ss.PINNED["agent-spawn"]
    assert all(rel.startswith("examples/") for rel in pin["authored"])
    assert ss.pin_problems("agent-spawn") == []


def test_every_pinned_example_is_used_unchanged() -> None:
    used = {json.dumps(e.payload, sort_keys=True) for e in examples()}
    for name, payload in ss.hook_examples("agent-spawn").items():
        assert json.dumps(payload, sort_keys=True) in used, name


def test_the_derived_agent_spawn_examples_satisfy_the_pinned_schemas() -> None:
    derived = [e for e in examples() if e.schema is not None]
    assert len(derived) >= 6
    for example in derived:
        assert example.schema is not None
        assert ss.errors(example.payload, *example.schema) == [], example.name


def test_each_shipped_file_validates_against_the_config_schema() -> None:
    schema = json.loads((SCHEMAS / "config.json").read_text())
    for path in (SHIPPED_CONFIG, SHIPPED_DROP_IN):
        jsonschema.Draft202012Validator(schema).validate(yaml.safe_load(path.read_text()))


def test_the_hazard_notes_ship_in_the_examples() -> None:
    # Distinctive phrases (comment marks and line breaks folded away): the notes must stay.
    drop_in = " ".join(DROP_IN.replace("#", " ").split())
    config = " ".join(CONFIG.replace("#", " ").split())
    assert (
        "wire either agent-spawn's or claude-session's state hooks to notifications, not both"
        in drop_in
    )
    assert "A post-fail hook must not call `notice-route flush` or `send`" in config
    assert "(hook_recursion)" in config


def test_the_readme_and_plan_list_what_ships() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    plan = (ROOT / "docs" / "plan.md").read_text(encoding="utf-8")
    for needed in (
        "config.example.yaml",
        "examples/awkit-wiring.yaml",
        "tests/test_example_config.py",
    ):
        assert needed in plan, needed
    assert "config.example.yaml" in readme and "examples/awkit-wiring.yaml" in readme


# --- the check can fail ----------------------------------------------------------------------


def test_a_misspelled_placeholder_fails(tmp_path: Path) -> None:
    drop_in = mutate(DROP_IN, "Agent {{object_id}} reported", "Agent {{objct_id}} reported")
    problems = run_check(tmp_path, drop_in=drop_in)
    assert any("agent-done" in p and "missing" in p and "objct_id" in p for p in problems)


def test_a_text_class_value_in_a_title_fails(tmp_path: Path) -> None:
    drop_in = mutate(DROP_IN, 'title: "Agent done"', 'title: "Agent {{report.summary}}"')
    problems = run_check(tmp_path, drop_in=drop_in)
    assert any("agent-done" in p and "excluded" in p for p in problems)


def test_an_unreachable_rule_fails(tmp_path: Path) -> None:
    copy = (
        "\n  - name: agent-done-copy\n    match:\n      source: agent-spawn\n"
        "      hook: post-state-change\n      where:\n        state: reported\n"
        "    notify: [team-chat]\n    level: info\n    message: copy\n"
    )
    problems = run_check(tmp_path, drop_in=DROP_IN + copy)
    assert any("rule_unreachable" in p for p in problems)
    assert any("agent-done-copy" in p and "no example" in p for p in problems)


def test_a_rule_that_matches_no_example_fails(tmp_path: Path) -> None:
    extra = (
        "\n  - name: nothing-sends-this\n    match:\n      source: no-such-producer\n"
        "    notify: [team-chat]\n    level: info\n    message: never\n"
    )
    problems = run_check(tmp_path, drop_in=DROP_IN + extra)
    assert problems == ["rule 'nothing-sends-this' is matched by no example (unreachable)"]


def test_an_example_that_matches_an_unexpected_rule_fails(tmp_path: Path) -> None:
    drop_in = mutate(DROP_IN, "      where:\n        state: reported\n", "")
    problems = run_check(tmp_path, drop_in=drop_in)
    assert any("expected no_rule, matched rule 'agent-done'" in p for p in problems)


def test_an_example_that_matches_no_rule_fails(tmp_path: Path) -> None:
    drop_in = mutate(DROP_IN, "hook: on-approved", "hook: on-approved-renamed")
    problems = run_check(tmp_path, drop_in=drop_in)
    assert any("pr approved" in p and "pr-approved" in p for p in problems)


def test_a_validate_warning_fails(tmp_path: Path) -> None:
    spare = "\n  spare:\n    kind: generic\n    cmd:\n      - /home/example/.local/bin/spare\n"
    config = mutate(CONFIG, "\n# --- quiet hours", spare + "\n# --- quiet hours")
    problems = run_check(tmp_path, config=config)
    assert any("destination_unused" in p for p in problems)


def test_a_pinned_schema_violation_in_an_example_fails(tmp_path: Path) -> None:
    given = examples()
    index = next(i for i, e in enumerate(given) if e.name == "session reported")
    bad = {**given[index].payload, "state": "no-such-state"}
    given[index] = Example(given[index].name, bad, given[index].rule, given[index].schema)
    problems = run_check(tmp_path, given=given)
    assert any("session reported" in p and "pinned" in p for p in problems)


def test_a_config_that_does_not_load_fails(tmp_path: Path) -> None:
    problems = run_check(
        tmp_path, drop_in=mutate(DROP_IN, "notify: [team-chat]", "notify: [nowhere]")
    )
    assert problems
    assert problems[0].startswith(("validate", "config"))
    # The router names the key of the unknown destination, never its value.
    assert "config_invalid" in problems[0]
    assert "rules." in problems[0]
    assert ".notify" in problems[0]


def test_a_quoted_stub_path_and_a_flow_style_cmd_still_install(tmp_path: Path) -> None:
    config = mutate(
        CONFIG,
        "      - /home/example/.local/bin/wsl-notice\n",
        '      - "/home/example/.local/bin/wsl-notice"\n',
    )
    assert run_check(private_dir(tmp_path, "quoted"), config=config) == []
    flow = mutate(
        CONFIG,
        "    cmd:\n      - /home/example/.local/bin/wsl-notice\n",
        "    cmd: ['/home/example/.local/bin/wsl-notice']\n",
    )
    assert run_check(private_dir(tmp_path, "flow"), config=flow) == []


def test_a_config_without_an_env_file_fails_with_a_clear_message(tmp_path: Path) -> None:
    config = mutate(
        CONFIG, "  env_file: /home/example/.config/notice-route/cron.env\n", "  log: true\n"
    )
    problems = run_check(tmp_path, config=config.replace("\nlog: true\n", "\n", 1))
    assert problems == ["install: no cron.env_file in the config or the drop-in"]


def test_a_destination_cmd_outside_the_placeholder_home_fails_clearly(tmp_path: Path) -> None:
    config = mutate(CONFIG, "/home/example/.local/bin/wsl-notice", "/usr/local/bin/wsl-notice")
    problems = run_check(tmp_path, config=config)
    assert len(problems) == 1
    assert problems[0].startswith("install: destination 'desktop' cmd[0]")
    assert "/usr/local/bin/wsl-notice" in problems[0]


def test_a_destination_without_a_cmd_list_fails_clearly(tmp_path: Path) -> None:
    config = mutate(CONFIG, "    cmd:\n      - /home/example/.local/bin/wsl-notice\n", "")
    problems = run_check(tmp_path, config=config)
    assert problems == ["install: config.yaml: destination 'desktop' has no cmd argv list"]


def test_a_config_that_is_not_yaml_fails_clearly(tmp_path: Path) -> None:
    problems = run_check(tmp_path, config="destinations: [unclosed\n")
    assert len(problems) == 1
    assert problems[0].startswith("install: config.yaml: not valid YAML")


def test_a_failed_validate_names_the_destinations_whose_cmd_failed() -> None:
    doc: dict[str, Any] = {
        "error": "config_invalid",
        "outcome": "failed",
        "destinations": [
            {"name": "phone", "cmd_ok": True},
            {"name": "desktop", "cmd_ok": False},
        ],
    }
    message = validate_failure(2, doc, {"desktop": ["/home/example/x"], "phone": ["/y"]})
    assert "exit 2, config_invalid" in message
    assert "'desktop' cmd ['/home/example/x'] is not ok" in message
    assert "phone" not in message


def test_a_missing_router_failures_rule_fails(tmp_path: Path) -> None:
    config = mutate(CONFIG, "name: router-failures", "name: router-failures-renamed")
    problems = run_check(tmp_path, config=config)
    assert any("required rule 'router-failures'" in p and "absent" in p for p in problems)


def test_a_rule_that_renders_no_link_fails(tmp_path: Path) -> None:
    drop_in = mutate(
        DROP_IN,
        'message: "{{pr}} was approved."\n    link: "{{pr_url}}"',
        'message: "{{pr}} was approved."\n    link: "{{ticket_url}}"',
    )
    problems = run_check(tmp_path, drop_in=drop_in)
    assert any("pr-approved" in p and "renders no link" in p for p in problems)


def test_a_rule_that_renders_an_empty_title_fails(tmp_path: Path) -> None:
    drop_in = mutate(DROP_IN, 'title: "Job held"', 'title: "{{ticket_url}}"')
    problems = run_check(tmp_path, drop_in=drop_in)
    assert any("job-held" in p and "empty title or message" in p for p in problems)


def test_an_alert_payload_that_breaks_the_post_fail_schema_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = example_check._own_failure_alert

    def broken(config: Any) -> Any:
        payload, note = real(config)
        return {**payload, "not_a_field": 1}, note

    monkeypatch.setattr(example_check, "_own_failure_alert", broken)
    problems = run_check(tmp_path)
    assert any(p.startswith("alert payload:") for p in problems)


def _rule_blocks(text: str) -> dict[str, str]:
    """The text of each drop-in rule, by name."""
    blocks = text.split("\n  - name: ")[1:]
    return {block.split("\n", 1)[0]: "  - name: " + block for block in blocks}


RULE_BLOCKS = _rule_blocks(DROP_IN)


@pytest.mark.parametrize("condition", ["source", "hook"])
@pytest.mark.parametrize("rule", sorted(RULE_BLOCKS))
def test_a_rule_that_loses_its_source_or_hook_condition_is_caught(
    tmp_path: Path, rule: str, condition: str
) -> None:
    """Every shipped producer rule: the negative examples (right hook with another source, right
    source with another hook) make the check fail when either condition is dropped. Verified by
    hand as well: deleting ``source:`` from any one rule of the drop-in fails the check."""
    block = RULE_BLOCKS[rule]
    line = next(
        line for line in block.splitlines(keepends=True) if line.strip().startswith(f"{condition}:")
    )
    drop_in = mutate(DROP_IN, block, block.replace(line, "", 1))
    assert run_check(tmp_path, drop_in=drop_in), (rule, condition)


def test_the_router_failure_rule_losing_its_source_is_caught(tmp_path: Path) -> None:
    config = mutate(
        CONFIG, "      source: notice-route\n      hook: post-fail\n", "      hook: post-fail\n"
    )
    assert run_check(tmp_path, config=config)
