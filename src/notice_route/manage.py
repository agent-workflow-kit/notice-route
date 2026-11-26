"""``queue list|show|drop`` and ``destination list|enable|disable`` (docs/interface.md: ``queue``
and ``destination``; docs/design.md C-61).

The database is a file that anything with write access to the state directory can change, so
every column is checked against the grammar of ``schemas/defs.json`` on its way out and a row
that fails is left out (counted in a warning), never repaired and never printed raw. A title is
the only free text: cut to 100 characters here and escaped by the printers.

Choices where the interface is silent, all the narrower reading:

- The claim of a row is decided by the one lease predicate of :mod:`notice_route.queue`; this
  module builds no second one. ``drop`` reads and ends a row in one ``BEGIN IMMEDIATE``
  transaction with a lease built after the lock is held.
- No config means the longest lease (so the most claims read as live, as for ``prune``) and no
  log line.
- ``queue drop`` and ``destination enable`` never create ``state.db``; ``destination disable``
  does (it has something to write).
- A destination that the config disables is never touched by ``enable`` or ``disable``.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from . import log as logmod
from . import overrides
from .decide import instant
from .gateway import Gateway
from .history import DESTINATION, DIGEST, HOOK, LEVELS, REASONS, RULE, SOURCE
from .payload import PayloadError, UsageError
from .queue import Lease, QueueView, drop_unclaimed, view_rows
from .report import worst_case_s
from .store import StoreError, open_readonly, open_store
from .validate import Config

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "DropResult",
    "ListResult",
    "SetResult",
    "destination_list",
    "row_document",
    "run_drop",
    "run_list",
    "run_set",
    "run_show",
]

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
_FAR = 1 << 62
_INT64_MAX = (1 << 63) - 1
# no config: the longest lease there is, so that the most claims read as live
_NO_CONFIG_WORST_CASE_S = float(1 << 20)
_SHA16 = re.compile(r"[0-9a-f]{16}")
_ACTOR = re.compile(r"(?:human|auto):[A-Za-z0-9._@/-]{1,64}")
_KINDS = ("held", "single")
EXIT_OK = 0
EXIT_NOOP = 22


def _utf8_replace(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _lease(gateway: Gateway, config: Config | None) -> Lease:
    worst = _NO_CONFIG_WORST_CASE_S if config is None else worst_case_s(config)
    return Lease.at(gateway.wall(), worst)


# --- one row ---------------------------------------------------------------------------------


def _int(value: object, low: int) -> int | None:
    return value if type(value) is int and low <= value <= _FAR else None


def row_document(v: QueueView) -> dict[str, Any] | None:
    """The ``--json`` object of one row (``schemas/defs.json`` ``queue_row``), or ``None`` when a
    column is outside its grammar."""
    rid, delivery_id = _int(v.id, 1), _int(v.delivery_id, 1)
    deliver_after, created_at, attempts = (
        _int(v.deliver_after, 0),
        _int(v.created_at, 0),
        _int(v.attempts, 0),
    )
    destination, source = v.destination, v.source
    if (
        rid is None
        or delivery_id is None
        or deliver_after is None
        or created_at is None
        or attempts is None
        or not isinstance(destination, str)
        or not DESTINATION.fullmatch(destination)
        or not isinstance(source, str)
        or not SOURCE.fullmatch(source)
        or v.kind not in _KINDS
        or v.level not in LEVELS
    ):
        return None
    if v.rule is not None and not (isinstance(v.rule, str) and RULE.fullmatch(v.rule)):
        return None
    if v.title is not None and not isinstance(v.title, str):
        return None
    if v.reason is not None and v.reason not in REASONS:
        return None
    expires = _int(v.claim_expires, 0) if v.claim != "none" else None
    return {
        "id": rid,
        "delivery_id": delivery_id,
        "destination": destination,
        "kind": v.kind,
        "level": v.level,
        "source": source,
        "rule": v.rule,
        "title": None if v.title is None else v.title[:100],
        "deliver_after": deliver_after,
        "created_at": created_at,
        "attempts": attempts,
        "reason": v.reason,
        "claim": v.claim,
        "claim_expires": expires,
    }


# --- list and show -----------------------------------------------------------------------------


@dataclass
class ListResult:
    rows: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    skipped: int = 0  # rows with a column outside its grammar


def _read_rows(
    gateway: Gateway,
    config: Config | None,
    *,
    destination: str | None = None,
    kind: str | None = None,
    row_id: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> ListResult:
    result = ListResult()
    store = open_readonly(gateway)
    if store is None:
        return result
    with store:
        # a column may hold bytes that are not UTF-8: replace them, never fail
        store.conn.text_factory = _utf8_replace
        try:
            views = view_rows(
                store.conn,
                _lease(gateway, config),
                destination=destination,
                kind=kind,  # type: ignore[arg-type]
                row_id=row_id,
                limit=limit,
            )
        except sqlite3.Error:
            raise StoreError("internal") from None
    for view in views:
        doc = row_document(view)
        if doc is None:
            result.skipped += 1
        else:
            result.rows.append(doc)
    return result


def run_list(
    gateway: Gateway,
    config: Config | None,
    *,
    destination: str | None,
    kind: str | None,
    limit: int,
) -> ListResult:
    """The rows by id, lowest first. ``StoreError`` and ``HomeError`` propagate (exit 1 and 2)."""
    return _read_rows(gateway, config, destination=destination, kind=kind, limit=limit)


def run_show(gateway: Gateway, config: Config | None, row_id: int) -> tuple[int, dict[str, Any]]:
    """``(exit code, document)``: 0 with the row, 22 ``not_found`` for an id not queued. A row
    whose columns fail their grammar is exit 1 (``StoreError``): it cannot be shown safely."""
    found = _read_rows(gateway, config, row_id=row_id, limit=1)
    if found.skipped:
        raise StoreError("corrupt")
    if not found.rows:
        return EXIT_NOOP, {"outcome": "no_op", "reason": "not_found", "row": None}
    return EXIT_OK, {"outcome": "ok", "reason": None, "row": found.rows[0]}


# --- drop --------------------------------------------------------------------------------------


@dataclass
class DropResult:
    exit_code: int
    document: dict[str, Any]
    warnings: list[str] = field(default_factory=list[str])


def _not_found(row_id: int, dry_run: bool) -> DropResult:
    doc = {
        "outcome": "no_op",
        "dry_run": dry_run,
        "id": row_id,
        "reason": "not_found",
        "destination": None,
        "delivery_id": None,
    }
    return DropResult(EXIT_NOOP, doc)


def _database_exists(gateway: Gateway) -> bool:
    """``True`` when ``state.db`` is a database (an older one included: only a writing open
    migrates it). Creates nothing."""
    try:
        store = open_readonly(gateway)
    except StoreError as exc:
        if exc.reason == "older":
            return True
        raise
    if store is None:
        return False
    store.close()
    return True


_DELIVERY_COLUMNS = (
    "SELECT d.hook, d.rule, d.title_sha256, d.message_sha256, d.dedupe"
    " FROM queue q JOIN deliveries d ON d.id = q.delivery_id WHERE q.id = ?"
)


def run_drop(
    gateway: Gateway,
    config: Config | None,
    row_id: int,
    *,
    dry_run: bool,
    actor: str,
    decision_id: str | None,
) -> DropResult:
    """End one queue row by hand. ``PayloadError('claimed')`` for a row with a live claim (nothing
    changes); ``StoreError`` and ``HomeError`` propagate."""
    started = gateway.monotonic()
    if dry_run:
        store = open_readonly(gateway)
    elif _database_exists(gateway):
        store = open_store(gateway)
    else:
        store = None
    if store is None:
        return _not_found(row_id, dry_run)
    ended: tuple[QueueView, tuple[object, ...]] | None = None
    live = False
    seen: QueueView | None = None
    with store:
        store.conn.text_factory = _utf8_replace
        if dry_run:
            try:
                views = view_rows(store.conn, _lease(gateway, config), row_id=row_id, limit=1)
            except sqlite3.Error:
                raise StoreError("internal") from None
            seen = views[0] if views else None
            live = seen is not None and seen.claim == "live"
        else:
            with store.write() as conn:
                # the lease is built after the write lock is held (queue.Lease)
                lease = _lease(gateway, config)
                views = view_rows(conn, lease, row_id=row_id, limit=1)
                seen = views[0] if views else None
                live = seen is not None and seen.claim == "live"
                if seen is not None and not live:
                    extra = conn.execute(_DELIVERY_COLUMNS, (row_id,)).fetchone()
                    if not drop_unclaimed(conn, row_id, lease, reason="manual"):
                        raise RuntimeError("an unclaimed row could not be dropped")
                    ended = (seen, tuple(extra) if extra is not None else (None,) * 5)
    if seen is None:
        return _not_found(row_id, dry_run)
    if live:
        raise PayloadError("claimed")
    destination = seen.destination
    delivery_id = _int(seen.delivery_id, 1)
    doc = {
        "outcome": "ok",
        "dry_run": dry_run,
        "id": row_id,
        "reason": None,
        "destination": destination
        if isinstance(destination, str) and DESTINATION.fullmatch(destination)
        else None,
        "delivery_id": delivery_id,
    }
    result = DropResult(EXIT_OK, doc)
    if ended is not None and delivery_id is not None:
        _log_drop(gateway, config, result, ended, actor, decision_id, started)
    return result


def _log_drop(
    gateway: Gateway,
    config: Config | None,
    result: DropResult,
    ended: tuple[QueueView, tuple[object, ...]],
    actor: str,
    decision_id: str | None,
    started: float,
) -> None:
    """One ``delivery`` line, only when the config logs and every required column is inside the
    grammar of ``schemas/log.json``. The row is dropped and committed: a failure here is a
    warning, never an error."""
    if config is None or not config.log:
        return
    view, (hook, rule, title_sha, message_sha, dedupe) = ended
    attempts = _int(view.attempts, 0)
    if not (
        isinstance(view.source, str)
        and SOURCE.fullmatch(view.source)
        and isinstance(view.destination, str)
        and DESTINATION.fullmatch(view.destination)
        and view.level in LEVELS
        and attempts is not None
    ):
        return
    try:
        rec = logmod.record(
            "delivery",
            wall=gateway.wall(),
            actor=actor,
            decision_id=decision_id,
            source=view.source,
            hook=hook if isinstance(hook, str) and HOOK.fullmatch(hook) else None,
            rule=rule if isinstance(rule, str) and RULE.fullmatch(rule) else None,
            destination=view.destination,
            level=view.level,
            outcome="dropped",
            reason="manual",
            attempts=attempts,
            title_sha256=title_sha
            if isinstance(title_sha, str) and _SHA16.fullmatch(title_sha)
            else None,
            message_sha256=message_sha
            if isinstance(message_sha, str) and _SHA16.fullmatch(message_sha)
            else None,
            dedupe_key_sha256=dedupe[:16]
            if isinstance(dedupe, str) and DIGEST.fullmatch(dedupe)
            else None,
            duration_ms=max(0, math.floor((gateway.monotonic() - started) * 1000)),
        )
        _append(gateway, rec, result)
    except (ValueError, TypeError, OverflowError):
        result.warnings.append("the log could not be written")


def _append(gateway: Gateway, rec: dict[str, Any], result: DropResult | SetResult) -> None:
    try:
        with gateway.open_state_dir() as state:
            writer = logmod.LogWriter(gateway, state)
            writer.write(rec)
            problems = writer.problems
    except Exception:  # the change is committed: a log that cannot be opened is a warning
        problems = 1
    if problems:
        result.warnings.append("the log could not be written")


# --- destinations ------------------------------------------------------------------------------


def _override_columns(
    found: overrides.Override | None,
) -> tuple[str | None, str | None, int | None]:
    """``(override, actor, set_at)`` of a stored row, each inside its grammar or ``None``."""
    if found is None:
        return None, None, None
    _value, actor, _decision, set_at = found
    # a row means disabled whatever its ``value`` (design C-61)
    return (
        "disabled",
        actor if isinstance(actor, str) and _ACTOR.fullmatch(actor) else None,
        _int(set_at, 0),
    )


def destination_list(gateway: Gateway, config: Config | None) -> tuple[int, dict[str, Any]]:
    """``(exit code, document)``: every configured destination in config order; 22 and an empty
    list with no config or no destination."""
    if config is None or not config.destinations:
        return EXIT_NOOP, {"destinations": []}
    store = open_readonly(gateway)
    out: list[dict[str, Any]] = []
    try:
        for dest in config.destinations.values():
            found = None
            if store is not None:
                store.conn.text_factory = _utf8_replace
                found = overrides.get(store.conn, dest.name)
            override, actor, set_at = _override_columns(found)
            out.append(
                {
                    "name": dest.name,
                    "kind": dest.kind,
                    "config_enabled": dest.enabled,
                    "override": override,
                    "enabled": dest.enabled and override is None,
                    "override_actor": actor,
                    "override_set_at": set_at,
                }
            )
    finally:
        if store is not None:
            store.close()
    return EXIT_OK, {"destinations": out}


@dataclass
class SetResult:
    exit_code: int
    document: dict[str, Any]
    warnings: list[str] = field(default_factory=list[str])


Action = Literal["enable", "disable"]


def _set_document(
    action: Action,
    name: str,
    dry_run: bool,
    outcome: str,
    done: str | None,
    reason: str | None,
    config_enabled: bool | None,
    override: str | None,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "dry_run": dry_run,
        "destination": name,
        "action": done,
        "reason": reason,
        "config_enabled": config_enabled,
        "override": override,
        "enabled": None if config_enabled is None else config_enabled and override is None,
    }


def _decide(action: Action, config_enabled: bool, present: bool) -> tuple[str | None, str | None]:
    """``(done, reason)``: the action that changes something, or the reason it changes nothing."""
    if not config_enabled:
        return None, "config_disabled"
    if action == "disable":
        return (None, "already_disabled") if present else ("disabled", None)
    return ("enabled", None) if present else (None, "not_disabled")


def _after(done: str | None, present: bool) -> str | None:
    """The override once the change is made: ``disabled`` after a disable, none after an enable,
    and what there was when nothing changed."""
    if done == "disabled":
        return "disabled"
    if done == "enabled":
        return None
    return "disabled" if present else None


def run_set(
    gateway: Gateway,
    config: Config | None,
    action: Action,
    name: str,
    *,
    dry_run: bool,
    actor: str,
    decision_id: str | None,
) -> SetResult:
    """``destination enable|disable``. ``UsageError('destination')`` for a name that is not
    configured (once a config with destinations exists); ``StoreError`` and ``HomeError``
    propagate."""
    started = gateway.monotonic()

    def noop(reason: str) -> SetResult:
        doc = _set_document(action, name, dry_run, "no_op", None, reason, None, None)
        return SetResult(EXIT_NOOP, doc)

    if config is None or not config.destinations:
        return noop("unconfigured")
    dest = config.destinations.get(name)
    if dest is None:
        raise UsageError("destination")
    if not config.enabled:
        return noop("disabled")
    write = not dry_run and dest.enabled and (action == "disable" or _database_exists(gateway))
    if not write:
        return _preview_set(gateway, action, dest.name, dest.enabled, dry_run)
    with open_store(gateway) as store, store.write() as conn:
        present = overrides.get(conn, name) is not None
        done, reason = _decide(action, dest.enabled, present)
        if done == "disabled":
            overrides.set_disabled(
                conn, name, actor=actor, decision_id=decision_id, now=instant(gateway.wall())
            )
        elif done == "enabled":
            overrides.clear(conn, name)
    after = _after(done, present)
    doc = _set_document(
        action, name, dry_run, "ok" if done else "no_op", done, reason, dest.enabled, after
    )
    result = SetResult(EXIT_OK if done else EXIT_NOOP, doc)
    if done and config.log:
        _log_override(gateway, result, name, after, actor, decision_id, started)
    return result


def _preview_set(
    gateway: Gateway, action: Action, name: str, config_enabled: bool, dry_run: bool
) -> SetResult:
    """The result a real run would have, from a read-only look (``--dry-run``, a destination
    that the config disables, or an ``enable`` with no database): nothing is written."""
    store = open_readonly(gateway)
    present = False
    if store is not None:
        with store:
            store.conn.text_factory = _utf8_replace
            present = overrides.get(store.conn, name) is not None
    done, reason = _decide(action, config_enabled, present)
    after = _after(done, present)
    doc = _set_document(
        action, name, dry_run, "ok" if done else "no_op", done, reason, config_enabled, after
    )
    return SetResult(EXIT_OK if done else EXIT_NOOP, doc)


def _log_override(
    gateway: Gateway,
    result: SetResult,
    name: str,
    after: str | None,
    actor: str,
    decision_id: str | None,
    started: float,
) -> None:
    try:
        rec = logmod.record(
            "override",
            wall=gateway.wall(),
            actor=actor,
            decision_id=decision_id,
            destination=name,
            override="disabled" if after == "disabled" else "cleared",
            duration_ms=max(0, math.floor((gateway.monotonic() - started) * 1000)),
        )
        _append(gateway, rec, result)
    except (ValueError, TypeError, OverflowError):
        result.warnings.append("the log could not be written")
