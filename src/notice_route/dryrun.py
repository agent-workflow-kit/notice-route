"""What ``send`` would do, without doing it: the document of ``test`` and ``send --dry-run``.

docs/interface.md (``test``, Other outputs) is normative and schemas/cli/test.json is the shape.
This module reads a validated config and a notification, and touches no file, lock,
clock or process, so a dry run cannot change anything.

The per-destination decision is ``decide.preview``: the same classification ``send`` runs, over
a read-only database that is opened only when a destination needs state (``open_state``), at the
instant ``now`` and with the environment ``environ`` the caller passes. It never writes.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping
from typing import Any

from .decide import DestinationDecision, Notice, preview
from .links import build_links
from .match import decide
from .payload import Notification
from .render import Rendered, render
from .store import Store
from .validate import Config, Rule

__all__ = ["EXIT_NOOP", "EXIT_OK", "dedupe_digest", "dry_run_document", "make_notice"]

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


def make_notice(
    n: Notification, rule: Rule, level: str, rendered: Rendered, link: str, *, pass_through: bool
) -> Notice:
    """The rendered notification the decision order works on (shared by ``send`` and ``test`` so
    both use one digest). The rule's window is converted once, to whole seconds, with ``ceil``."""
    return Notice(
        source=n.source,
        hook=n.hook if n.shape == "hook" else None,
        level=level,
        rule=rule.name,
        title=rendered.title,
        message=rendered.message,
        link=link,
        dedupe=dedupe_digest(n.source, n.hook, level, rendered.title, rendered.message, link),
        dedup_window=math.ceil(rule.dedup_window),
        event_id=n.event_id,
        object_id=n.object_id,
        labels=n.labels,
        pass_through=pass_through,
    )


def _destination(d: DestinationDecision) -> dict[str, Any]:
    return {
        "name": d.name,
        "decision": d.decision,
        "reason": d.reason,
        "deliver_after": d.deliver_after,
    }


def dry_run_document(
    config: Config | None,
    n: Notification,
    open_state: Callable[[], Store | None],
    now: int,
    environ: Mapping[str, str],
) -> tuple[dict[str, Any], int]:
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
    notice = make_notice(
        n, decision.rule, decision.level, rendered, built.link, pass_through=decision.pass_through
    )
    results = preview(open_state, config, notice, decision.rule.notify, now=now, environ=environ)
    doc.update(
        title=rendered.title,
        message=rendered.message,
        link=built.link or None,
        missing=list(rendered.missing),
        excluded=list(rendered.excluded),
        text_excluded=rendered.text_excluded,
        level=decision.level,
        dedupe=notice.dedupe,
        destinations=[_destination(d) for d in results],
    )
    if all(d["decision"] == "disabled" for d in doc["destinations"]):
        doc["reason"] = "all_disabled"
        return doc, EXIT_NOOP
    return doc, EXIT_OK
