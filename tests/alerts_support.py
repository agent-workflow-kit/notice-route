"""Helpers for the end-to-end tests of surfacing, alerts and the ``post-fail`` hook (#27)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from tests.fakes import Hook
from tests.flush_support import flush
from tests.send_support import NOW, Rig
from tests.test_cli_rules_validate import schema_errors

DAY = 86400
HOUR = 3600
DAY_START = NOW - NOW % DAY  # 2023-11-14T00:00:00Z
ALERT_RULE: dict[str, Any] = {
    "name": "alerts",
    "match": {"source": "notice-route", "hook": "post-fail"},
    "notify": ["ops"],
    "level": "warn",
    "title": "Channel {{object_id}} failed",
    "message": "{{kind}}: {{code}}, {{outcome}} after {{attempts}} attempts ({{notification_source}})",
}
SEND_RULE: dict[str, Any] = {"name": "pt", "match": {"level_min": "info"}, "notify": ["chat"]}

MakeHook = Callable[..., Hook]


def hook_errors(instance: Any) -> list[str]:
    return schema_errors(instance, "../hooks/post-fail.json")


class Setup:
    """``chat`` fails with ``code``; ``ops`` is a healthy destination the alerts go to."""

    def __init__(self, rig: Rig, code: str = "credential_invalid", **top: Any) -> None:
        self.rig = rig
        self.bad = rig.notifier("chat")
        self.behave(code)
        self.ops = rig.notifier("ops")
        rules = top.pop("rules", [ALERT_RULE, SEND_RULE])
        dests: dict[str, dict[str, Any]] = {
            "chat": {"notifier": self.bad},
            "ops": {"notifier": self.ops},
        }
        dests.update(top.pop("destinations", {}))
        rig.configure(dests, rules, **top)

    def behave(self, code: str, *, retryable: bool = False, unknown: bool = False) -> None:
        """``chat`` answers every call with the error envelope of ``code`` (any well-shaped code)."""
        body = {"error": {"code": code, "retryable": retryable, "unknown_outcome": unknown}}
        self.bad.set(exit=12, stdout=json.dumps(body))

    def fail(self, text: str = "boom", **kw: Any) -> tuple[int, dict[str, Any], str]:
        return self.rig.send(text, **kw)

    def surfaced(self) -> list[tuple[Any, ...]]:
        return self.rig.query(
            "SELECT destination, code, period, delivery_id, processed FROM surfaced ORDER BY id"
        )

    def flush(self, *extra: str) -> tuple[int, dict[str, Any], str]:
        return flush(self.rig, *extra)

    def alerts(self) -> list[tuple[Any, ...]]:
        return self.rig.query(
            "SELECT id, destination, outcome, reason, level, hook FROM deliveries"
            " WHERE source = 'notice-route' ORDER BY id"
        )

    def delivered_texts(self) -> list[dict[str, Any]]:
        return [json.loads(e["text"]) for e in self.ops.events("stdin")]


def hooks_config(*hooks: Hook, timeout: str = "30s", is_async: bool = False) -> dict[str, Any]:
    return {
        "hooks": {
            "post-fail": [{"cmd": h.cmd, "timeout": timeout, "async": is_async} for h in hooks]
        }
    }
