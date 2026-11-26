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
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

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


def _install(work: Path, config_text: str, drop_in_text: str) -> tuple[Path, Path]:
    """Write both files under a private home; return (home, the directory standing in for
    ``/home/example``)."""
    home = work / "home"
    example = work / "example"
    directory = home / ".config" / "notice-route"
    (directory / "config.d").mkdir(parents=True)
    directory.chmod(0o700)
    (directory / "config.d").chmod(0o700)
    config_text = config_text.replace(PLACEHOLDER_HOME, str(example))
    (directory / "config.yaml").write_text(config_text, encoding="utf-8")
    (directory / "config.d" / "awkit-wiring.yaml").write_text(
        drop_in_text.replace(PLACEHOLDER_HOME, str(example)), encoding="utf-8"
    )
    for path in (directory / "config.yaml", directory / "config.d" / "awkit-wiring.yaml"):
        path.chmod(0o600)
    for match in re.finditer(
        rf"^\s*- ({re.escape(str(example))}/\.local/bin/[\w.-]+)\s*$", config_text, re.M
    ):
        stub = Path(match.group(1))
        _private_dirs(stub.parent, example)
        stub.write_text("", encoding="utf-8")  # never run: offline commands spawn nothing
        stub.chmod(0o700)
    for match in re.finditer(rf"env_file:\s*({re.escape(str(example))}/\S+)", config_text):
        env_file = Path(match.group(1))
        _private_dirs(env_file.parent, example)
        env_file.write_text("", encoding="utf-8")
        env_file.chmod(0o600)
    return home, example


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
    home, _ = _install(work, config_text, drop_in_text)
    runner = _Runner(home, work)
    try:
        code, doc = runner.run("validate")
    except AssertionError as error:
        return [*problems, f"validate: {error}"]
    if code != 0 or doc.get("error") is not None and "error" in doc:
        problems.append(f"validate: exit {code}, {doc.get('error') or doc.get('outcome')}")
        return problems
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
