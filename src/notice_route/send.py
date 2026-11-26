"""``send`` end to end (#22): docs/interface.md, ``send`` steps 4 to 8, Output and Exit codes.

The caller (``cli.py``) reads the input and the config and passes them in; this module runs
everything after that:

1. no config or no destinations is ``unconfigured`` and a disabled router is ``disabled``
   (exit 22, no state read or written);
2. the first matching rule (``match.decide``); ``no_rule`` and a ``drop`` rule write one history
   row without a destination and one log line;
3. ``links.build_links``, ``render.render`` and ``dryrun.make_notice``;
4. ``decide.decide_and_record`` (one transaction: repeat check, delivery rows, window binding,
   event ids, queue rows, counters), then the advisory line when a row was queued for later;
5. the notifier calls, in parallel threads (one per send-now row): ``send_lock``, then
   ``notify.call_notifier``; the lock is released when the call returns, before the result is
   recorded; the main thread records each result (``outcome.apply_outcome`` in a transaction of
   its own, with the surfacing seam inside it) as the calls finish;
6. the output document, the log lines and the exit code (0, 12, 22; 1 for an internal fault).

Choices where the interface is silent (docs/design.md C-52 has the reasons):

- A signal before the first call (``trap.received`` set when the decision is done, or set while a
  call thread waits for its lock and before any call started) releases the claims of this process
  and exits 128+N; once a call has started, signals only set the flag and the calls finish within
  their deadlines (the cli arms the trap for the input read and config load only and keeps it
  installed until the output is written).
- Anything that escapes a call thread is ``record_escaped`` (``unknown``/``killed``, the queue row
  stays claimed). The exception text is never kept.
- A result that cannot be recorded (``StoreError`` after one retry, or any other exception of the
  recording) is logged only, the claim lapses and ``flush`` resends (a duplicate is possible); the
  command exits 1 ``internal`` after it has handled every other destination. A result whose claim
  was lost goes to the log and the output as classified (a queued retry is shown as classified:
  nothing is stored for a lost claim, the claimant that took the row decides).
- ``not_started`` (an internal fault) records nothing, is not logged (``outcome`` has no word for
  it) and ends the command with exit 1.
- Seams, injectable through :class:`Seams`: the retry schedule (:mod:`notice_route.retry` by
  default); TODO(#27) surfacing a ``failed`` or ``unknown`` end
  (:class:`SurfaceEvent`; ``rate_capped`` is already surfaced by ``decide``, ``env_missing`` and
  ``overflow`` are not yet).
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from . import log as logmod
from .calling import Busy, Called, Escaped, Gate, NotRun, Skipped, call_row
from .decide import (  # pyright: ignore[reportPrivateUsage]
    DestinationDecision,
    Notice,
    _object_id,
    decide_and_record,
    hour_start,
    instant,
    labels_text,
)
from .dryrun import make_notice
from .gateway import DirHandle, Gateway, SignalTrap
from .links import build_links
from .match import decide as match_decide
from .notify import CallResult, call_notifier
from .outcome import LOCK_BUSY, Applied, Outcome, apply_outcome, classify, record_escaped
from .payload import Notification
from .queue import Lease, QueueRow, insert_delivery, release_claims
from .render import render
from .report import worst_case_s
from .retry import retry_schedule as default_retry_schedule
from .store import SEND_BUSY_S, Store, StoreError, open_store
from .validate import Config, Destination

__all__ = [
    "RECORD_RETRY_PAUSE_S",
    "ADVISORY_FLUSH_AGE_S",
    "Seams",
    "SendOptions",
    "SendResult",
    "SurfaceEvent",
    "record_result",
    "run_send",
    "with_retry",
]

ADVISORY_FLUSH_AGE_S = 300
_FAILED = ("failed", "unknown")
RECORD_RETRY_PAUSE_S = 0.2  # before the one second try to record a result (C-52)


@dataclass(frozen=True)
class SendOptions:
    actor: str
    decision_id: str | None = None
    verbose: bool = False


@dataclass(frozen=True)
class SurfaceEvent:
    """A ``failed`` or ``unknown`` end, given to the surfacing seam inside the recording
    transaction (TODO(#27): it inserts the ``surfaced`` row and marks the delivery). It may be
    run again after a rolled-back failure (the recording is retried once), so it writes only
    through the given connection (a rollback undoes it) and has no external effect."""

    delivery_id: int
    destination: str
    kind: str
    source: str
    level: str
    outcome: str
    code: str | None
    attempts: int


def _no_surface(conn: sqlite3.Connection, event: SurfaceEvent) -> None:
    """TODO(#27): insert the ``surfaced`` row for ``event`` and mark its delivery."""


@dataclass(frozen=True)
class Seams:
    """The injectable parts: the notifier call, the retry schedule and the surfacing."""

    call: Callable[..., CallResult] = call_notifier
    retry_schedule: Callable[[str, int], Callable[[int], int | None]] = default_retry_schedule
    surface: Callable[[sqlite3.Connection, SurfaceEvent], None] = _no_surface


@dataclass
class SendResult:
    """What the cli prints. ``document`` is the ``--json`` object (``None`` with ``error`` or
    when a signal ended the command); ``error`` is ``(code, fixed message)`` for exit 1.
    ``lines`` are the per-destination stderr lines (printed without ``--json``), ``advisories``
    are dropped by ``--quiet``, ``warnings`` never are, ``verbose`` only with ``--verbose``."""

    exit_code: int
    document: dict[str, Any] | None = None
    error: tuple[str, str] | None = None
    lines: list[str] = field(default_factory=list[str])
    advisories: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])
    verbose: list[str] = field(default_factory=list[str])


# --- the document ----------------------------------------------------------------------------


def _document(n: Notification, started: float, gateway: Gateway) -> dict[str, Any]:
    return {
        "outcome": "ok",
        "shape": n.shape,
        "source": n.source,
        "hook": n.hook if n.shape == "hook" else None,
        "rule": None,
        "level": None,
        "reason": None,
        "text_excluded": False,
        "labels_ignored": n.labels_ignored,
        "destinations": [],
        "dedupe": None,
        "duration_ms": _elapsed_ms(gateway, started),
    }


def _elapsed_ms(gateway: Gateway, started: float) -> int:
    return max(0, math.floor((gateway.monotonic() - started) * 1000))


def _entry(
    name: str,
    outcome: str,
    *,
    reason: str | None = None,
    attempts: int = 0,
    code: str | None = None,
    count: int | None = None,
    deliver_after: int | None = None,
    notifier_outcome: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "outcome": outcome,
        "reason": reason,
        "attempts": attempts,
        "code": code,
        "count": count,
        "deliver_after": deliver_after,
        "notifier_outcome": notifier_outcome,
    }


def _decided_entry(d: DestinationDecision) -> dict[str, Any]:
    """The entry of a destination that was decided and not called."""
    code = "overflow" if d.reason == "overflow" else None
    count = (d.count or 1) if d.reason == "repeat" else None
    outcome = "deferred" if d.decision == "defer" else d.outcome
    return _entry(
        d.name,
        outcome,
        reason=d.reason,
        code=code,
        count=count,
        deliver_after=d.deliver_after if d.decision == "defer" else None,
    )


def _line(entry: dict[str, Any]) -> str:
    detail = entry["reason"] or entry["code"]
    text = f"{entry['name']}: {entry['outcome']}" + (f" ({detail})" if detail else "")
    if entry["deliver_after"] is not None:
        text += f" deliver_after={entry['deliver_after']}"
    return text


# --- the log ---------------------------------------------------------------------------------


@dataclass
class _Context:
    gateway: Gateway
    started: float
    n: Notification
    opts: SendOptions
    writer: logmod.LogWriter
    rule: str | None = None
    level: str | None = None
    notice: Notice | None = None

    def log(self, entry: dict[str, Any] | None, *, reason: str | None = None) -> None:
        """One ``delivery`` record: for a destination entry, or (``entry`` ``None``) the line of
        a ``drop`` rule or ``no_rule`` (``reason``)."""
        notice = self.notice
        fields: dict[str, Any] = {
            "source": self.n.source,
            "hook": self.n.hook if self.n.shape == "hook" else None,
            "rule": self.rule,
            "level": self.level,
            "event_id": self.n.event_id,
            "duration_ms": _elapsed_ms(self.gateway, self.started),
        }
        if entry is None:
            fields.update(outcome="dropped", reason=reason, attempts=0)
        else:
            fields.update(
                destination=entry["name"],
                outcome=entry["outcome"],
                reason=entry["reason"],
                code=entry["code"],
                attempts=entry["attempts"],
            )
        if notice is not None:
            fields.update(
                title_sha256=logmod.sha16(notice.title),
                message_sha256=logmod.sha16(notice.message),
                dedupe_key_sha256=notice.dedupe[:16],
            )
        self.writer.write(
            logmod.record(
                "delivery",
                wall=self.gateway.wall(),
                actor=self.opts.actor,
                decision_id=self.opts.decision_id,
                **fields,
            )
        )


# --- the pipeline ----------------------------------------------------------------------------


def run_send(
    gateway: Gateway,
    config: Config | None,
    n: Notification,
    opts: SendOptions,
    started: float,
    trap: SignalTrap,
    seams: Seams = Seams(),  # noqa: B008 - frozen, shared on purpose
) -> SendResult:
    """Steps 4 to 8 of ``send`` for a validated notification. ``StoreError`` and ``HomeError``
    before the first call propagate (exit 1 and 2 in the cli, nothing sent)."""
    doc = _document(n, started, gateway)
    if config is None or (config.enabled and not config.destinations):
        doc.update(outcome="no_op", reason="unconfigured")
        return _finish(SendResult(22, doc), gateway, started, advisory="no config")
    if not config.enabled:
        doc.update(outcome="no_op", reason="disabled")
        return _finish(SendResult(22, doc), gateway, started)
    decision = match_decide(config.rules, n)
    trap.armed = False
    with open_store(gateway, busy_timeout=SEND_BUSY_S) as store, gateway.open_state_dir() as state:
        ctx = _Context(
            gateway, started, n, opts, logmod.LogWriter(gateway, state if config.log else None)
        )
        result = _routed(gateway, config, n, decision, store, state, ctx, doc, trap, seams)
    if ctx.writer.problems:
        result.warnings.append("the log could not be written")
    return _finish(result, gateway, started)


def _finish(
    result: SendResult, gateway: Gateway, started: float, *, advisory: str | None = None
) -> SendResult:
    if advisory:
        result.advisories.append(advisory)
    if result.document is not None:
        result.document["duration_ms"] = _elapsed_ms(gateway, started)
    return result


def _routed(
    gateway: Gateway,
    config: Config,
    n: Notification,
    decision: Any,
    store: Store,
    state: DirHandle,
    ctx: _Context,
    doc: dict[str, Any],
    trap: SignalTrap,
    seams: Seams,
) -> SendResult:
    worst = worst_case_s(config)
    if decision.rule is None or decision.outcome == "dropped":
        reason = "no_rule" if decision.rule is None else "rule_drop"
        ctx.rule = None if decision.rule is None else decision.rule.name
        with store.write() as conn:
            insert_delivery(
                conn,
                ts=Lease.at(gateway.wall(), worst).now,
                source=n.source,
                level=n.level or "info",
                outcome="dropped",
                hook=n.hook if n.shape == "hook" else None,
                object_id=_object_id(n.object_id),
                rule=ctx.rule,
                reason=reason,
                event_id=None,
                labels=labels_text(n.labels),
            )
        ctx.log(None, reason=reason)
        doc["rule"] = ctx.rule
        doc["reason"] = reason
        if decision.rule is None:
            doc["outcome"] = "no_op"
            return SendResult(22, doc)
        return SendResult(0, doc)

    rule = decision.rule
    built = build_links(rule, n, config.links)
    rendered = render(
        rule, n, level=decision.level, pr_url=built.pr_url, ticket_url=built.ticket_url
    )
    notice = make_notice(
        n, rule, decision.level, rendered, built.link, pass_through=decision.pass_through
    )
    ctx.rule, ctx.level, ctx.notice = rule.name, decision.level, notice
    names = tuple(dict.fromkeys(rule.notify))
    decisions = decide_and_record(store, gateway, config, notice, names)
    doc.update(
        rule=rule.name,
        level=decision.level,
        text_excluded=rendered.text_excluded,
        dedupe=notice.dedupe,
    )
    result = SendResult(0, doc)
    entries: dict[str, dict[str, Any]] = {}
    sending: list[DestinationDecision] = []
    for d in decisions:
        result.verbose.append(
            f"decision {d.name}: {d.decision}" + (f" ({d.reason})" if d.reason else "")
        )
        if d.outcome == "sending":
            sending.append(d)
            continue
        entries[d.name] = _decided_entry(d)
    decided = list(entries.values())
    if sending and trap.received is not None:  # a signal before the first call
        _release(store, gateway, worst, sending)
        _log_all(ctx, decided)
        return SendResult(128 + trap.received)
    if all(d.decision == "disabled" for d in decisions):
        _log_all(ctx, decided)
        doc.update(outcome="no_op", reason="all_disabled", destinations=list(entries.values()))
        result.exit_code = 22
        result.lines = [_line(e) for e in entries.values()]
        return result
    if any(d.decision == "defer" and d.queue_id is not None for d in decisions):
        _advisory(store, gateway, result)
    if sending:
        gate = Gate(trap, strict=False)
        _call_all(
            gateway, config, store, state, ctx, notice, sending, entries, result, worst, seams, gate
        )
        if not gate.started and trap.received is not None:  # no call was made: nothing was sent
            _log_all(ctx, decided)
            return SendResult(128 + trap.received)
    _log_all(ctx, decided)  # after the calls: a line waits up to 0.5 s for log.lock
    ordered = [entries[name] for name in names if name in entries]
    doc["destinations"] = ordered
    result.lines = [_line(e) for e in ordered]
    if result.error is None and any(e["outcome"] in _FAILED for e in ordered):
        doc["outcome"] = "partial"
        result.exit_code = 12
    return result


def _log_all(ctx: _Context, entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        ctx.log(entry)


def _release(
    store: Store, gateway: Gateway, worst: float, sending: list[DestinationDecision]
) -> None:
    with store.write() as conn:
        lease = Lease.at(gateway.wall(), worst)
        for d in sending:
            assert d.claim_token is not None
            release_claims(conn, d.claim_token, lease)


def _advisory(store: Store, gateway: Gateway, result: SendResult) -> None:
    """The line for a row queued for later when no ``flush`` has started in 5 minutes or the last
    full ``flush`` skipped a destination as ``env_missing`` (it can only be advice: any failure to
    read the heartbeat is no advice)."""
    try:
        rows = store.read(
            "SELECT key, value FROM meta WHERE key IN (?, ?)", ("flush_last", "flush_env_missing")
        )
    except StoreError:
        return
    meta = {str(k): v for k, v in rows}

    def number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None

    last = number(meta.get("flush_last"))
    now = gateway.wall()
    if last is None or now - last > ADVISORY_FLUSH_AGE_S:
        result.advisories.append("no recent flush: queued rows wait until flush runs")
    elif number(meta.get("flush_env_missing")) is not None:
        result.advisories.append("the last flush skipped a destination (env_missing)")


def _call_all(
    gateway: Gateway,
    config: Config,
    store: Store,
    state: DirHandle,
    ctx: _Context,
    notice: Notice,
    sending: list[DestinationDecision],
    entries: dict[str, dict[str, Any]],
    result: SendResult,
    worst: float,
    seams: Seams,
    gate: Gate,
) -> None:
    decided_at = sending[0].decided_at
    decided_hour = None if decided_at is None else hour_start(decided_at)
    queued: dict[str, tuple[Destination, QueueRow, str]] = {}
    for d in sending:
        assert d.queue_id is not None and d.delivery_id is not None and d.claim_token is not None
        dest = config.destinations[d.name]
        row = QueueRow(
            id=d.queue_id,
            delivery_id=d.delivery_id,
            destination=d.name,
            kind="single",
            title=notice.title or None,
            message=notice.message,
            link=notice.link or None,
            level=notice.level,
            source=notice.source,
            rule=notice.rule,
            dedupe=notice.dedupe,
            deliver_after=0,
            created_at=0,
            claimed_by=d.claim_token,
            claim_expires=d.claim_expires,
        )
        queued[d.name] = (dest, row, d.claim_token)
    with ThreadPoolExecutor(max_workers=len(queued)) as pool:
        futures = {
            pool.submit(
                call_row,
                gateway,
                dest,
                row,
                state,
                config.lock_wait,
                notice.dedupe,
                seams.call,
                gate,
            ): name
            for name, (dest, row, _token) in queued.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            dest, row, token = queued[name]
            what = future.result()
            _record(
                gateway,
                store,
                ctx,
                seams,
                worst,
                dest,
                row,
                token,
                what,
                decided_hour,
                entries,
                result,
            )


def _record(
    gateway: Gateway,
    store: Store,
    ctx: _Context,
    seams: Seams,
    worst: float,
    dest: Destination,
    row: QueueRow,
    token: str,
    what: Called | Busy | Escaped | Skipped | NotRun,
    count_hour: int | None,
    entries: dict[str, dict[str, Any]],
    result: SendResult,
) -> None:
    """Record and log one destination's result. Whatever goes wrong here (a ``StoreError``, a
    failing surfacing seam, anything else) is this destination's problem only: its claim is left
    to lapse, the result is logged with what the call said, and the other destinations go on."""
    try:
        _record_one(gateway, store, ctx, seams, worst, dest, row, token, what, count_hour, entries,
                    result)  # fmt: skip
    except Exception:  # the text of an exception is never kept
        _unrecorded(result)
        if dest.name not in entries:
            entry = _fallback_entry(dest, what, gateway)
            entries[dest.name] = entry
            ctx.log(entry)


def _fallback_entry(
    dest: Destination, what: Called | Busy | Escaped | Skipped | NotRun, gateway: Gateway
) -> dict[str, Any]:
    if isinstance(what, (Called, Busy)):
        try:
            oc = (
                LOCK_BUSY
                if isinstance(what, Busy)
                else classify(what.result, kind=dest.kind, has_dedupe_key=True)
            )
            return _unapplied_entry(dest.name, oc, gateway)
        except Exception:  # classify itself failed: all that is known is that a call was made
            pass
    return _entry(dest.name, "unknown", code="killed", attempts=1)


def _release_row(store: Store, gateway: Gateway, worst: float, token: str) -> None:
    with store.write() as conn:
        release_claims(conn, token, Lease.at(gateway.wall(), worst))


def _record_one(
    gateway: Gateway,
    store: Store,
    ctx: _Context,
    seams: Seams,
    worst: float,
    dest: Destination,
    row: QueueRow,
    token: str,
    what: Called | Busy | Escaped | Skipped | NotRun,
    count_hour: int | None,
    entries: dict[str, dict[str, Any]],
    result: SendResult,
) -> None:
    if isinstance(what, Skipped):  # no call was made: give the row back, the command is 128+N
        try:
            _release_row(store, gateway, worst, token)
        except StoreError:
            pass  # the claim lapses by itself
        return
    if isinstance(what, NotRun):  # nothing was sent: give the row back, no attempt, exit 1
        try:
            _release_row(store, gateway, worst, token)
        except StoreError:
            pass  # the claim lapses by itself
        result.error = ("internal", "a notifier call could not be started")
        result.exit_code = 1
        return
    if isinstance(what, Escaped):
        try:
            held = with_retry(gateway, row, lambda: record_escaped(store, row=row, token=token))
        except StoreError:
            held = None
        entry = _entry(dest.name, "unknown", code="killed", attempts=1)
        if held is None:
            _unrecorded(result)
        elif not held:
            result.warnings.append("a result was logged only: the claim was lost")
        entries[dest.name] = entry
        ctx.log(entry)
        return
    outcome: Outcome = (
        LOCK_BUSY
        if isinstance(what, Busy)
        else classify(what.result, kind=dest.kind, has_dedupe_key=True)
    )

    try:
        applied = record_result(store, gateway, seams, worst, dest, row, token, outcome, count_hour)
    except StoreError:
        _unrecorded(result)
        entry = _unapplied_entry(dest.name, outcome, gateway)
        entries[dest.name] = entry
        if outcome.outcome is not None:
            ctx.log(entry)
        return
    if applied.internal:
        result.error = ("internal", "a notifier call could not be started")
        result.exit_code = 1
        return
    if not applied.applied:
        result.warnings.append("a result was logged only: the claim was lost")
        entry = _unapplied_entry(dest.name, outcome, gateway)
    else:
        entry = _applied_entry(dest.name, applied)
    entries[dest.name] = entry
    ctx.log(entry)


def record_result(
    store: Store,
    gateway: Gateway,
    seams: Seams,
    worst: float,
    dest: Destination,
    row: QueueRow,
    token: str,
    outcome: Outcome,
    count_hour: int | None,
) -> Applied:
    """Record one classified result in a transaction of its own, with the surfacing seam inside it
    (shared with ``flush``). Retried once after a busy database (:func:`with_retry`)."""

    def write() -> Applied:
        # may run twice (see with_retry): the whole transaction rolls back on a failure, so
        # ``seams.surface`` must write only through ``conn`` and have no external effect
        with store.write() as conn:
            lease = Lease.at(gateway.wall(), worst)
            applied = apply_outcome(
                conn,
                row=row,
                token=token,
                outcome=outcome,
                lease=lease,
                retry_at=seams.retry_schedule(dest.kind, lease.now),
                count_hour=count_hour,
            )
            if applied.applied and applied.outcome in _FAILED:
                assert applied.outcome is not None
                seams.surface(
                    conn,
                    SurfaceEvent(
                        row.delivery_id,
                        dest.name,
                        dest.kind,
                        row.source,
                        row.level,
                        applied.outcome,
                        applied.code,
                        applied.attempts,
                    ),
                )
            return applied

    return with_retry(gateway, row, write)


def with_retry[T](gateway: Gateway, row: QueueRow, write: Callable[[], T]) -> T:
    """Run a recording once more after a short pause when the database was unusable (busy), if
    the claim has life left for it: the pause is bounded by the claim's remaining life."""
    try:
        return write()
    except StoreError:
        left = (row.claim_expires or 0) - gateway.wall()
        if left < 4 * RECORD_RETRY_PAUSE_S:
            raise
        gateway.clock.sleep(RECORD_RETRY_PAUSE_S)
        return write()


def _unrecorded(result: SendResult) -> None:
    result.warnings.append(
        "a result could not be recorded: the row will be sent again (a duplicate is possible)"
    )
    result.error = ("internal", "state database cannot be written")
    result.exit_code = 1


def _applied_entry(name: str, a: Applied) -> dict[str, Any]:
    assert a.outcome is not None
    return _entry(
        name,
        a.outcome,
        reason=a.reason,
        attempts=a.attempts,
        code=a.code,
        deliver_after=a.deliver_after,
        notifier_outcome=a.notifier_outcome,
    )


def _unapplied_entry(name: str, oc: Outcome, gateway: Gateway) -> dict[str, Any]:
    name_out = oc.outcome or "unknown"
    later = instant(gateway.wall()) if name_out in ("queued_retry", "deferred") else None
    return _entry(
        name,
        name_out,
        reason=oc.reason,
        attempts=1 if oc.counted else 0,
        code=oc.code,
        deliver_after=later,
        notifier_outcome=oc.notifier_outcome,
    )
