"""The render check of the shipped example config and drop-in (#30).

``check`` installs a config and one drop-in under a private home, runs offline ``validate`` and
offline ``test`` (never ``--probe``, never ``send``, ``flush`` or ``cron``: nothing is spawned,
sent or written to the state directory), and returns every problem found as a list of strings
(empty means the build passes). The negative tests feed it mutated copies.

Offline ``validate`` checks that each destination's ``cmd[0]`` is an executable owned by the user,
so the example's placeholder home ``/home/example`` is mapped to a private directory holding empty
stub files (never run) and a mode-0600 ``cron.env``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from notice_route.cli import main
from notice_route.config import ConfigError
from notice_route.gateway import FakeClock, Gateway
from notice_route.validate import load_valid_config
from tests import sibling_schemas
from tests.example_payloads import Example
from tests.test_cli_rules_validate import schema_errors

ROOT = Path(__file__).resolve().parent.parent
SHIPPED_CONFIG = ROOT / "config.example.yaml"
SHIPPED_DROP_IN = ROOT / "examples" / "awkit-wiring.yaml"
PLACEHOLDER_HOME = "/home/example"
# The only validate warning a fresh state directory documents: no flush has ever run.
ALLOWED_WARNINGS = frozenset({"flush_stale"})
NOW = 1_700_000_000.0


class _Runner:
    def __init__(self, home: Path, trusted_above: Path) -> None:
        self.home, self.trusted_above = home, trusted_above

    def gateway(self, stdin: bytes) -> tuple[Gateway, int]:
        read, write = os.pipe()
        os.write(write, stdin)
        os.close(write)
        gateway = Gateway(
            passwd_home=lambda: str(self.home),
            clock=FakeClock(wall=NOW),
            exec_check_from=str(self.trusted_above),
            stdin_fd=read,
            environ=lambda: {},
        )
        return gateway, read

    def run(self, *argv: str, stdin: bytes = b"") -> tuple[int, dict[str, Any]]:
        gateway, read = self.gateway(stdin)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main([*argv, "--json"], gateway)
        finally:
            os.close(read)
        try:
            return code, json.loads(out.getvalue())
        except ValueError:
            raise AssertionError(
                f"{argv}: no JSON document (exit {code}): {err.getvalue()}"
            ) from None


def _private_dirs(leaf: Path, top: Path) -> None:
    """Create ``leaf`` and every directory down from ``top`` as mode 0700 (whatever the umask)."""
    leaf.mkdir(parents=True, exist_ok=True)
    for directory in (leaf, *leaf.parents):
        directory.chmod(0o700)
        if directory == top:
            break


class SetupProblem(Exception):
    """The config pair cannot be installed under the private home: a clear message, no traceback."""


def _load_yaml(name: str, text: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise SetupProblem(f"{name}: not valid YAML: {error}") from None
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise SetupProblem(f"{name}: the top level is not a mapping")
    return document


def _placeholder_path(where: str, value: object) -> Path:
    """``value`` under the placeholder home, mapped to the same path under ``example``."""
    if not isinstance(value, str) or not value.startswith(PLACEHOLDER_HOME + "/"):
        raise SetupProblem(f"{where}: {value!r} is not an absolute path under {PLACEHOLDER_HOME}")
    return Path(value)


def _destination_commands(documents: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Every destination's cmd (both files merged by name); a clear problem for a bad layout."""
    commands: dict[str, list[str]] = {}
    for name, document in documents.items():
        destinations = document.get("destinations", {})
        if not isinstance(destinations, dict):
            raise SetupProblem(f"{name}: destinations is not a mapping")
        for destination, body in destinations.items():
            cmd = body.get("cmd") if isinstance(body, dict) else None
            if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
                raise SetupProblem(f"{name}: destination {destination!r} has no cmd argv list")
            commands[str(destination)] = cmd
    return commands


def _env_file(documents: dict[str, dict[str, Any]]) -> str:
    for document in documents.values():
        cron = document.get("cron")
        if isinstance(cron, dict) and cron.get("env_file"):
            return str(cron["env_file"])
    raise SetupProblem("no cron.env_file in the config or the drop-in")


def _install(
    work: Path, config_text: str, drop_in_text: str
) -> tuple[Path, Path, dict[str, list[str]]]:
    """Write both files under a private home; return (home, the directory standing in for
    ``/home/example``, every destination's cmd). Stubs and the env file come from the parsed YAML,
    not from the text, so quoting and flow style cannot hide one; raises ``SetupProblem``."""
    documents = {
        "config.yaml": _load_yaml("config.yaml", config_text),
        "awkit-wiring.yaml": _load_yaml("awkit-wiring.yaml", drop_in_text),
    }
    commands = _destination_commands(documents)
    stubs = [
        _placeholder_path(f"destination {name!r} cmd[0]", cmd[0]) for name, cmd in commands.items()
    ]
    env_file = _placeholder_path("cron.env_file", _env_file(documents))
    home = work / "home"
    example = work / "example"
    directory = home / ".config" / "notice-route"
    (directory / "config.d").mkdir(parents=True)
    directory.chmod(0o700)
    (directory / "config.d").chmod(0o700)
    targets = (
        (directory / "config.yaml", config_text),
        (directory / "config.d" / "awkit-wiring.yaml", drop_in_text),
    )
    for path, text in targets:
        path.write_text(text.replace(PLACEHOLDER_HOME, str(example)), encoding="utf-8")
        path.chmod(0o600)
    for placeholder in stubs:
        stub = example / placeholder.relative_to(PLACEHOLDER_HOME)
        _private_dirs(stub.parent, example)
        stub.write_text("", encoding="utf-8")  # never run: offline commands spawn nothing
        stub.chmod(0o700)
    mapped_env = example / env_file.relative_to(PLACEHOLDER_HOME)
    _private_dirs(mapped_env.parent, example)
    mapped_env.write_text("", encoding="utf-8")
    mapped_env.chmod(0o600)
    return home, example, commands


def validate_failure(code: int, doc: dict[str, Any], commands: dict[str, list[str]]) -> str:
    """The problem text of a failed offline ``validate``: the error and every destination whose
    cmd failed the ownership check."""
    parts = [f"validate: exit {code}, {doc.get('error') or doc.get('outcome')}"]
    for destination in doc.get("destinations") or []:
        if destination.get("cmd_ok") is False:
            name = destination.get("name")
            parts.append(f"destination {name!r} cmd {commands.get(str(name))} is not ok")
    return "; ".join(parts)


def _own_failure_alert(config: Any) -> Any:
    from notice_route.alerts import build_payload
    from notice_route.payload import alert_notification

    destination = next(iter(config.destinations))
    payload = build_payload(
        config, destination, "retries_exhausted", 1, ("direct", "warn", 3, "failed", "[]")
    )
    assert payload is not None
    return payload, alert_notification(payload)


def _render_problems(
    doc: dict[str, Any], code: int, example: Example, rule_link: bool
) -> list[str]:
    where = f"example {example.name!r}"
    problems: list[str] = []
    if schema_errors(doc, "test.json"):
        return [f"{where}: test output does not match schemas/cli/test.json"]
    if example.rule is None:
        if doc["rule"] is not None or doc["reason"] != "no_rule" or code != 22:
            problems.append(f"{where}: expected no_rule, matched rule {doc['rule']!r}")
        return problems
    if doc["rule"] != example.rule:
        return [f"{where}: expected rule {example.rule!r}, matched {doc['rule']!r}"]
    if doc["reason"] == "rule_drop":
        return problems
    if doc["reason"] is not None:
        problems.append(f"{where}: rule {example.rule!r} decided nothing ({doc['reason']})")
    if doc["missing"]:
        problems.append(f"{where}: rule {example.rule!r} renders missing {doc['missing']}")
    if doc["excluded"]:
        problems.append(f"{where}: rule {example.rule!r} renders excluded {doc['excluded']}")
    if not doc["title"] or not doc["message"]:
        problems.append(f"{where}: rule {example.rule!r} renders an empty title or message")
    if rule_link and not doc["link"]:
        problems.append(f"{where}: rule {example.rule!r} renders no link")
    return problems


def check(
    work: Path,
    config_text: str,
    drop_in_text: str,
    examples: Sequence[Example],
    *,
    alert_rule: str = "router-failures",
) -> list[str]:
    """Every problem of the config pair and its examples; empty when the check passes."""
    problems: list[str] = []
    for example in examples:
        if example.schema is not None:
            tool, schema = example.schema
            for message in sibling_schemas.errors(example.payload, tool, schema):
                problems.append(f"example {example.name!r}: pinned {tool}/{schema}: {message}")
    try:
        home, _, commands = _install(work, config_text, drop_in_text)
    except SetupProblem as error:
        return [*problems, f"install: {error}"]
    runner = _Runner(home, work)
    try:
        code, doc = runner.run("validate")
    except AssertionError as error:
        return [*problems, f"validate: {error}"]
    if code != 0 or doc.get("error") is not None:
        return [*problems, validate_failure(code, doc, commands)]
    if schema_errors(doc, "validate.json"):
        problems.append("validate: output does not match schemas/cli/validate.json")
    for warning in doc["warnings"]:
        if warning["code"] not in ALLOWED_WARNINGS:
            problems.append(f"validate: warning {warning['code']}: {warning['message']}")
    gateway = Gateway(passwd_home=lambda: str(home), exec_check_from=str(work), environ=lambda: {})
    try:
        config = load_valid_config(gateway)
    except ConfigError as error:
        return [*problems, f"config: {error}"]
    assert config is not None
    links = {rule.name: bool(getattr(rule, "link", None)) for rule in config.rules}
    if alert_rule not in links:
        problems.append(f"required rule {alert_rule!r} (the router's own failure alert) is absent")
    reached: set[str] = set()
    for example in examples:
        if example.rule == alert_rule:
            continue
        code, rendered = runner.run("test", stdin=json.dumps(example.payload).encode())
        problems.extend(
            _render_problems(rendered, code, example, links.get(example.rule or "", False))
        )
        if rendered.get("rule"):
            reached.add(rendered["rule"])
    # The router's own alert is built by the router (the reserved source cannot be sent).
    if alert_rule in links:
        from notice_route.dryrun import dry_run_document

        payload, note = _own_failure_alert(config)
        for message in schema_errors_hook(payload):
            problems.append(f"alert payload: {message}")
        rendered, code = dry_run_document(config, note, lambda: None, int(NOW), {})
        alert = Example("router failure alert", payload, alert_rule)
        problems.extend(_render_problems(rendered, code, alert, links[alert_rule]))
        if rendered.get("rule"):
            reached.add(rendered["rule"])
    for name in links:
        if name not in reached:
            problems.append(f"rule {name!r} is matched by no example (unreachable)")
    return problems


def schema_errors_hook(payload: dict[str, Any]) -> list[str]:
    import jsonschema
    from referencing import Registry, Resource

    schemas = ROOT / "schemas"
    registry: Registry[Any] = Registry()
    for path in schemas.rglob("*.json"):
        document = json.loads(path.read_text())
        registry = registry.with_resource(document["$id"], Resource.from_contents(document))
    schema = json.loads((schemas / "hooks" / "post-fail.json").read_text())
    validator = jsonschema.Draft202012Validator(schema, registry=registry)
    return [e.message for e in validator.iter_errors(payload)]
