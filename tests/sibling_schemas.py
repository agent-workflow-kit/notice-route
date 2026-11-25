"""Validate against the siblings' schemas, copied under ``tests/sibling-schemas/``.

Each tool's schemas are copied (never imported, never read from the sibling's checkout at test
time) into ``tests/sibling-schemas/<tool>/<version>/``. ``PIN.json`` in that directory records
the tool, the version, the sibling commit the files were copied from, and the SHA-256 of every
copied file (``copied``) and of every file written here (``authored``, the hook payload
examples). ``pin_problems`` fails on any difference between the record and the disk, so an edit
without a re-pin is caught.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parent / "sibling-schemas"
# Version of each pinned tool: the interface version for a design-only tool, the released
# package version otherwise.
PINNED = {"slack-notice": "0.1.0-design", "ha-notice": "0.28.0", "agent-spawn": "0.38.0"}


def pin_dir(tool: str, root: Path | None = None) -> Path:
    return (root or ROOT) / tool / PINNED[tool]


def read_pin(tool: str, root: Path | None = None) -> dict[str, Any]:
    return json.loads((pin_dir(tool, root) / "PIN.json").read_text("utf-8"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pin_problems(tool: str, root: Path | None = None) -> list[str]:
    """Differences between ``PIN.json`` and the files on disk; empty when they agree."""
    base = pin_dir(tool, root)
    pin = read_pin(tool, root)
    problems: list[str] = []
    if pin.get("tool") != tool:
        problems.append(f"{tool}: PIN.json tool is {pin.get('tool')!r}")
    if pin.get("version") != PINNED[tool] or base.name != PINNED[tool]:
        problems.append(f"{tool}: PIN.json version {pin.get('version')!r} != {PINNED[tool]!r}")
    recorded: dict[str, str] = {**pin.get("copied", {}), **pin.get("authored", {})}
    on_disk = {
        p.relative_to(base).as_posix(): p
        for p in sorted(base.rglob("*"))
        if p.is_file() and p.name != "PIN.json"
    }
    for rel, want in recorded.items():
        if rel not in on_disk:
            problems.append(f"{tool}/{rel}: missing")
        elif digest(on_disk[rel]) != want:
            problems.append(f"{tool}/{rel}: changed since it was pinned")
    for rel in on_disk:
        if rel not in recorded:
            problems.append(f"{tool}/{rel}: not recorded in PIN.json")
    return problems


@cache
def _documents(tool: str) -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for path in sorted(pin_dir(tool).rglob("*.json")):
        if path.name == "PIN.json" or "examples" in path.relative_to(pin_dir(tool)).parts:
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        schema_id = document["$id"]
        if schema_id in documents:
            raise ValueError(f"duplicate $id {schema_id!r} ({path})")
        documents[schema_id] = document
    return documents


@cache
def _registry(tool: str) -> Registry[Any]:
    resources = [
        (schema_id, Resource.from_contents(document, default_specification=DRAFT202012))
        for schema_id, document in _documents(tool).items()
    ]
    return Registry().with_resources(resources)  # pyright: ignore[reportUnknownMemberType]


# Defects of a pinned schema itself, as (tool, message) pairs, tolerated by ``errors`` and still
# reported by ``raw_errors``. slack-notice 0.1.0-design: its error schema says
# ``details: {"not": {"required": ["phase"]}}`` outside ``unavailable``, and ``required`` is
# vacuously true for null, so the documented ``"details": null`` is rejected for every code but
# ``unavailable``. Reported upstream; drop the entry when a re-pin brings the fix (a test fails
# when the defect is gone).
KNOWN_DEFECTS = {
    ("slack-notice", "error/details: None should not be valid under {'required': ['phase']}"),
}


def errors(instance: Any, tool: str, schema_name: str) -> list[str]:
    """Violations of ``<tool>/<schema_name>`` apart from the ``KNOWN_DEFECTS``."""
    return [e for e in raw_errors(instance, tool, schema_name) if (tool, e) not in KNOWN_DEFECTS]


def raw_errors(instance: Any, tool: str, schema_name: str) -> list[str]:
    """Every violation of ``<tool>/<schema_name>`` (e.g. ``cli/error.json``)."""
    schema_id = f"{tool}/{schema_name}"
    documents = _documents(tool)
    if schema_id not in documents:
        raise KeyError(f"no schema {schema_id!r} in {pin_dir(tool)}")
    validator = Draft202012Validator(
        documents[schema_id], registry=_registry(tool), format_checker=FormatChecker()
    )
    found: list[str] = []
    for error in validator.iter_errors(instance):
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        found.append(f"{location}: {error.message}")
    return found


def validate(instance: Any, tool: str, schema_name: str) -> None:
    """Raise ``AssertionError`` listing every violation of the pinned schema."""
    problems = errors(instance, tool, schema_name)
    if problems:
        raise AssertionError(
            f"{tool}/{schema_name} ({PINNED[tool]}) rejected the instance:\n" + "\n".join(problems)
        )


def hook_examples(tool: str) -> dict[str, dict[str, Any]]:
    """The authored hook payload examples of a producer, keyed by file stem."""
    base = pin_dir(tool) / "examples"
    return {p.stem: json.loads(p.read_text("utf-8")) for p in sorted(base.glob("*.json"))}
