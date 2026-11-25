"""Helpers for the end-to-end tests of ``flush`` (#23): seeding queue rows in a real state
directory and running ``flush`` through the real entry point."""

from __future__ import annotations

import json
from typing import Any

from notice_route.queue import Lease, enqueue, insert_delivery
from notice_route.store import open_store
from tests.send_support import NOW, Rig
from tests.test_cli_rules_validate import schema_errors


def seed(
    rig: Rig,
    destination: str = "chat",
    *,
    level: str = "info",
    kind: str = "single",
    deliver_after: int = NOW,
    created_at: int | None = None,
    message: str = "hello",
    title: str | None = None,
    link: str | None = None,
    attempts: int = 0,
    token: str | None = None,
    claim_expires: int | None = None,
    source: str = "s",
    now: int = NOW,
    outcome: str = "deferred",
    reason: str | None = None,
) -> tuple[int, int]:
    """One ``deliveries`` row and its queue row; returns (delivery id, queue id)."""
    with open_store(rig.gateway()) as store, store.write() as conn:
        did = insert_delivery(
            conn,
            ts=now,
            source=source,
            level=level,
            outcome=outcome,
            destination=destination,
            attempts=attempts,
            reason=reason,
            deliver_after=deliver_after,
        )
        got = enqueue(
            conn,
            delivery_id=did,
            kind=kind,  # type: ignore[arg-type]
            message=message,
            level=level,
            source=source,
            deliver_after=deliver_after,
            created_at=now if created_at is None else created_at,
            lease=Lease(now, 100),
            title=title,
            link=link,
            dedupe="ab" * 32,
            claim_token=token,
            cap=10_000,
        )
        if token is not None and claim_expires is not None:
            conn.execute(
                "UPDATE queue SET claim_expires = ? WHERE id = ?", (claim_expires, got.row_id)
            )
    return did, got.row_id


def flush(
    rig: Rig, *extra: str, env: dict[str, str] | None = None
) -> tuple[int, dict[str, Any], str]:
    """``flush --json``; the document is validated against the schema."""
    code, out, err = rig.run("flush", "--json", *extra, env=env)
    assert out.endswith("\n") and out.count("\n") == 1, out
    doc = json.loads(out)
    if "error" in doc:
        assert schema_errors(doc, "error.json") == []
    else:
        assert schema_errors(doc, "flush.json") == [], doc
    return code, doc, err


def dest(doc: dict[str, Any], name: str = "chat") -> dict[str, Any]:
    (found,) = [d for d in doc["destinations"] if d["name"] == name]
    return found
