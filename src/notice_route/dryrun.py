"""What ``send`` would do, computed without state: the document of ``test`` and ``send --dry-run``.

docs/interface.md (``test``, Other outputs) is normative and schemas/cli/test.json is the shape.
This module is pure: it reads a validated config and a notification, and touches no file, lock,
clock or process, so a dry run cannot change anything.

The per-destination decision is computed from the config alone: a disabled destination is
``disabled`` and a level below the floor is ``drop`` (``below_floor``); every other destination is
``send``. TODO(#18): the quiet, redelivery, repeat, ``env_missing`` and ``rate_capped`` decisions
of ``send`` step 7 (they read ``state.db`` read-only and the clock).
"""

from __future__ import annotations

import hashlib
from typing import Any

from .links import build_links
from .match import decide
from .payload import LEVELS, Notification
from .render import render
from .validate import Config

__all__ = ["EXIT_NOOP", "EXIT_OK", "dedupe_digest", "dry_run_document"]

EXIT_OK = 0
EXIT_NOOP = 22


def dedupe_digest(
    source: str, hook: str | None, level: str, title: str, message: str, link: str
) -> str:
    """The repeat key of docs/interface.md (Dedup): SHA-256 of source, hook (empty for a notice),
    final level, rendered title, message and link, joined with NUL bytes."""
    key = "\0".join([source, hook or "", level, title, message, link])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _base(n: Notification, reason: str | None) -> dict[str, Any]:
    return {
        "shape": n.shape,
        "source": n.source,
        "hook": n.hook if n.shape == "hook" else None,
        "rule": None,
        "reason": reason,
        "title": None,
        "message": None,
        "link": None,
        "missing": [],
        "excluded": [],
        "text_excluded": False,
        "labels_ignored": n.labels_ignored,
        "level": None,
        "dedupe": None,
        "destinations": [],
    }


def _destination(config: Config, name: str, level: str) -> dict[str, Any]:
    dest = config.destinations[name]
    decision, reason = "send", None
    if not dest.enabled:
        decision = "disabled"
    elif LEVELS.index(level) < LEVELS.index(dest.floor):
        decision, reason = "drop", "below_floor"
    return {"name": name, "decision": decision, "reason": reason, "deliver_after": None}


def dry_run_document(config: Config | None, n: Notification) -> tuple[dict[str, Any], int]:
    """The document and the exit code (0, or 22 when nothing would be done)."""
    if config is None or (config.enabled and not config.destinations):
        return _base(n, "unconfigured"), EXIT_NOOP
    if not config.enabled:
        return _base(n, "disabled"), EXIT_NOOP
    decision = decide(config.rules, n)
    if decision.rule is None:
        return _base(n, "no_rule"), EXIT_NOOP
    doc = _base(n, None)
    doc["rule"] = decision.rule.name
    if decision.outcome == "dropped":
        doc["reason"] = "rule_drop"
        return doc, EXIT_OK
    assert decision.level is not None
    built = build_links(decision.rule, n, config.links)
    rendered = render(
        decision.rule, n, level=decision.level, pr_url=built.pr_url, ticket_url=built.ticket_url
    )
    doc.update(
        title=rendered.title,
        message=rendered.message,
        link=built.link or None,
        missing=list(rendered.missing),
        excluded=list(rendered.excluded),
        text_excluded=rendered.text_excluded,
        level=decision.level,
        dedupe=dedupe_digest(
            n.source, n.hook, decision.level, rendered.title, rendered.message, built.link
        ),
        destinations=[_destination(config, name, decision.level) for name in decision.rule.notify],
    )
    if all(d["decision"] == "disabled" for d in doc["destinations"]):
        doc["reason"] = "all_disabled"
        return doc, EXIT_NOOP
    return doc, EXIT_OK
