"""Helpers for the tests of ``queue`` and ``destination`` (#34): running the real entry point with
``--json``, validating every document against its schema, golden files, and direct writes to a
real ``state.db`` (the database is a file: tests change it as anything with write access could)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from notice_route.store import open_store
from tests.send_support import Rig
from tests.test_cli_rules_validate import schema_errors

GOLDEN = Path(__file__).resolve().parent / "golden"


def run_json(
    rig: Rig, *argv: str, schema: str, env: dict[str, str] | None = None
) -> tuple[int, Any, str]:
    """``<argv> --json``: one line of ASCII JSON on stdout, valid against ``schema`` (the error
    envelope against ``error.json``). Returns the code, the document and stderr."""
    code, out, err = rig.run(*argv, "--json", env=env)
    assert out.endswith("\n") and out.count("\n") == 1, out
    out.encode("ascii")  # no raw text of a row reaches a terminal
    doc = json.loads(out)
    if isinstance(doc, dict) and "error" in doc:
        assert schema_errors(doc, "error.json") == [], doc
    else:
        assert schema_errors(doc, schema) == [], doc
    return code, doc, err


def golden(directory: str, name: str, doc: Any) -> None:
    """Compare ``doc`` with ``tests/golden/<directory>/<name>.json``; rewrite it with
    ``NOTICE_ROUTE_UPDATE_GOLDEN=1`` after a deliberate change and review the diff."""
    path = GOLDEN / directory / f"{name}.json"
    text = json.dumps(doc, indent=2) + "\n"
    if os.environ.get("NOTICE_ROUTE_UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
    assert path.read_text() == text


def write(rig: Rig, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Change the database as a process with write access could, bypassing the router."""
    conn = sqlite3.connect(rig.state / "state.db")
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def make_v1_database(rig: Rig) -> None:
    """A database as release 0.x left it: migration 1 only (``user_version`` 1, no override
    table), made by creating a current one and taking version 2 off it."""
    open_store(rig.gateway()).close()
    conn = sqlite3.connect(rig.state / "state.db")
    try:
        conn.execute("DROP TABLE destination_overrides")
        conn.execute("DROP TABLE hook_outbox")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
    finally:
        conn.close()
