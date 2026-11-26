"""Failure alerts and the hook jobs of ``flush`` (#27): docs/interface.md, Hooks.

``flush`` takes every pending ``surfaced`` row once (:func:`take_surfaced`), before it delivers:
one write transaction per row reads the failing ``deliveries`` row, sets ``processed = 1`` and,
unless the failing notification came from the router itself, routes the ``post-fail`` payload
through the rules as a hook payload of source ``notice-route`` (decisions (1) to (6) of ``send``,
then an unclaimed ``single`` row due now: :func:`notice_route.decide.decide_alert`). Reading the
delivery and marking the row processed happen in the same transaction (``prune`` may delete the
delivery right after). The hook jobs it returns are started by ``flush`` after the transaction:
a crash in between loses the hook (best effort, at most once).

Choices where the interface is silent (the narrower reading):

- A row whose ``deliveries`` row is gone, whose destination is no longer configured,
  or whose columns do not fit the payload grammar (the database is a file anyone with write access
  to the state directory can edit) is marked processed and skipped: no alert, no hook, no error.
- A failure whose notification has source ``notice-route`` (an alert's own failure, cap, overflow
  or expiry) runs the hooks and routes nothing, so an alert never causes another alert.
- An alert no rule routes (``no_rule`` or a ``drop`` rule) writes the destination-less
  ``deliveries`` row ``send`` writes (source ``notice-route``, outcome ``dropped``, reason
  ``no_rule`` or ``rule_drop``) and a log line; it is never surfaced. The failing row's
  ``surfaced`` flag and the hook remain.
- A row of a destination that is disabled now still routes its alert and runs the hook (only
  ``expired`` is restricted to enabled destinations, where it is surfaced); a destination
  removed from the config is skipped.
- The payload carries no title, message, link or payload text of the failing notification: its
  fields come from validated columns only, and the alert's text comes from the rule's templates
  through :mod:`notice_route.render` (so hostile text is sanitized or excluded there).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import log as logmod
from .decide import _object_id, decide_alert, labels_text  # pyright: ignore[reportPrivateUsage]
from .dryrun import make_notice
from .gateway import Gateway
from .links import build_links
from .match import decide as match_decide
from .payload import alert_notification
from .queue import Lease, insert_delivery
from .render import render
from .store import Store, StoreError
from .surfacing import HOURLY_CODES
from .validate import LEVELS, Config

__all__ = ["HookJob", "Taken", "build_payload", "take_surfaced"]

MAX_ROWS = 1000  # surfaced rows one flush takes: the rest wait for the next run
_SOURCE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_LABEL = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}=[A-Za-z0-9._:/#@+=,-]{0,256}")
_LABELS_CHARS = 1024
_OUTCOME_OF_CODE = {
    "rate_capped": "deferred",
    "env_missing": "deferred",
    "overflow": "dropped",
    "expired": "expired",
}


@dataclass(frozen=True)
class HookJob:
    """A payload for the ``post-fail`` hooks."""

    destination: str
    payload: dict[str, Any]


@dataclass
class Taken:
    """What a run took. ``error`` is set when the state database could not be written part way:
    the rows committed before it are all here and stay taken."""

    jobs: list[HookJob] = field(default_factory=list[HookJob])
    log: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])  # delivery records
    taken: int = 0
    error: bool = False
    faults: int = 0  # rows whose routing faulted (counted, never described)


def _labels(text: object) -> list[str] | None:
    if text is None:
        return []
    try:
        found = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None
    if not isinstance(found, list):
        return None
    labels: list[str] = []
    for item in found:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(item, str) and _LABEL.fullmatch(item):
            labels.append(item)
    labels = labels[:16]
    while labels and len(json.dumps(labels)) > _LABELS_CHARS:
        labels.pop()
    return labels


def build_payload(
    config: Config, destination: str, code: str, delivery_id: int, delivery: tuple[Any, ...]
) -> dict[str, Any] | None:
    """The ``post-fail`` payload of one surfaced row, or ``None`` when the destination is not
    configured or a column does not fit the grammar of schemas/hooks/post-fail.json.
    ``delivery`` is ``(source, level, attempts, outcome, labels)`` of the failing row."""
    dest = config.destinations.get(destination)
    if dest is None:
        return None
    source, level, attempts, outcome, labels_text = delivery
    labels = _labels(labels_text)
    if (
        labels is None
        or not isinstance(source, str)
        or _SOURCE.fullmatch(source) is None
        or level not in LEVELS
        or type(attempts) is not int
        or attempts < 0
        or _CODE.fullmatch(code) is None
        or delivery_id < 1
    ):
        return None
    if code in HOURLY_CODES:
        shown = _OUTCOME_OF_CODE[code]
    else:
        shown = outcome if outcome in ("failed", "unknown") else "failed"
    return {
        "tool": "notice-route",
        "hook": "post-fail",
        "object_type": "destination",
        "object_id": destination,
        "event_id": delivery_id,
        "labels": labels,
        "code": code,
        "outcome": shown,
        "attempts": attempts,
        "notification_source": source,
        "notification_level": level,
        "kind": dest.kind,
    }


def take_surfaced(gateway: Gateway, store: Store, config: Config, worst: float) -> Taken:
    """Take the pending ``surfaced`` rows, oldest first, one transaction each (at most
    :data:`MAX_ROWS`). A ``StoreError`` ends the run with ``error`` set: the jobs and log lines
    of the rows committed before it are returned, since those rows stay taken."""
    out = Taken()
    while out.taken < MAX_ROWS:
        try:
            one = _take_one(gateway, store, config, worst)
        except StoreError:
            out.error = True
            break
        if one is None:
            break
        out.taken += 1
        out.jobs += one.jobs
        out.log += one.log
        out.faults += one.faults
    return out


def _take_one(gateway: Gateway, store: Store, config: Config, worst: float) -> Taken | None:
    """One row in one transaction; ``None`` when none is pending. A fault of the routing (not of
    the database) rolls the row's alert back, and the row is still marked processed in a
    transaction of its own, so that it cannot block every later run; its hook job is kept."""
    one = Taken()
    row_id: int | None = None
    try:
        with store.write() as conn:
            row = conn.execute(
                "SELECT id, destination, code, delivery_id FROM surfaced WHERE processed = 0"
                " ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            row_id = int(row[0])
            conn.execute("UPDATE surfaced SET processed = 1 WHERE id = ?", (row_id,))
            delivery_id = row[3]
            if type(delivery_id) is not int:
                return one
            found = conn.execute(
                "SELECT source, level, attempts, outcome, labels FROM deliveries WHERE id = ?",
                (delivery_id,),
            ).fetchone()
            if found is None:
                return one
            payload = build_payload(config, str(row[1]), str(row[2]), delivery_id, tuple(found))
            if payload is None:
                return one
            one.jobs.append(HookJob(str(row[1]), payload))
            if payload["notification_source"] == "notice-route":
                return one  # an alert's own failure: hooks only
            lease = Lease.at(gateway.wall(), worst)
            _route(gateway, conn, config, payload, lease, one)
    except StoreError:
        raise
    except Exception:  # a fault of the routing: never block the later rows
        if row_id is None:
            raise
        one.log.clear()
        one.faults = 1
        with store.write() as conn:
            conn.execute("UPDATE surfaced SET processed = 1 WHERE id = ?", (row_id,))
    return one


def _route(
    gateway: Gateway,
    conn: Any,
    config: Config,
    payload: dict[str, Any],
    lease: Lease,
    out: Taken,
) -> None:
    n = alert_notification(payload)
    decision = match_decide(config.rules, n)
    base: dict[str, Any] = {"source": n.source, "hook": n.hook, "event_id": n.event_id}
    if decision.rule is None or decision.outcome == "dropped":
        reason = "no_rule" if decision.rule is None else "rule_drop"
        insert_delivery(  # as ``send`` records them: in ``history``, never surfaced
            conn,
            ts=lease.now,
            source=n.source,
            level="info",
            outcome="dropped",
            hook=n.hook,
            object_id=_object_id(n.object_id),
            rule=None if decision.rule is None else decision.rule.name,
            reason=reason,
            event_id=None,
            labels=labels_text(n.labels),
        )
        out.log.append(
            base
            | {
                "rule": None if decision.rule is None else decision.rule.name,
                "level": "info",
                "outcome": "dropped",
                "reason": reason,
                "attempts": 0,
            }
        )
        return
    rule = decision.rule
    assert decision.level is not None
    built = build_links(rule, n, config.links)
    rendered = render(
        rule, n, level=decision.level, pr_url=built.pr_url, ticket_url=built.ticket_url
    )
    notice = make_notice(
        n, rule, decision.level, rendered, built.link, pass_through=decision.pass_through
    )
    names = tuple(dict.fromkeys(rule.notify))
    for d in decide_alert(conn, gateway, config, notice, names, lease):
        out.log.append(
            base
            | {
                "rule": rule.name,
                "destination": d.name,
                "level": decision.level,
                "outcome": "deferred" if d.decision == "defer" else d.outcome,
                "reason": d.reason,
                "code": "overflow" if d.reason == "overflow" else None,
                "attempts": 0,
                "title_sha256": logmod.sha16(notice.title),
                "message_sha256": logmod.sha16(notice.message),
                "dedupe_key_sha256": notice.dedupe[:16],
            }
        )
