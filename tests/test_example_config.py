"""The example config and the drop-in, checked as CI does (#30): they validate as one config
offline, and every shipped rule renders against the payloads it must match with no missing or
excluded token. The negative tests prove the check can fail."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from notice_route.gateway import SYSTEM_TZ_DIRS
from tests import sibling_schemas as ss
from tests.example_check import ROOT, SHIPPED_CONFIG, SHIPPED_DROP_IN, check
from tests.example_payloads import Example, examples

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
if not _HAVE_ZONES and __import__("os").environ.get("CI"):
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


def test_the_drop_in_alone_validates_against_the_config_schema() -> None:
    import jsonschema

    from tests.test_cli_rules_validate import SCHEMAS

    yaml = pytest.importorskip("yaml")
    schema = json.loads((SCHEMAS / "config.json").read_text())
    for path in (SHIPPED_CONFIG, SHIPPED_DROP_IN):
        jsonschema.Draft202012Validator(schema).validate(yaml.safe_load(path.read_text()))


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
    assert problems and problems[0].startswith(("validate", "config"))
