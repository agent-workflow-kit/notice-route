"""``flush`` (#23): docs/interface.md, ``flush`` steps 1 to 7, Output, Exit codes and Files.

The cli parses the flags and loads the config; this module does the rest. One thread per
destination (destinations run in parallel); each thread opens a connection of its own, because a
SQLite connection is not shared between threads. Per destination, in order:

1. a disabled destination is skipped (state ``disabled``);
2. one with a named variable unset is skipped and left untouched (state ``env_missing``);
3. the flush lock ``locks/<name>.flush``, taken without waiting (state ``already_running``);
4. every unclaimed row 24 hours old or more ends ``expired``;
5. while the destination is quiet, a due ``info`` row ends ``dropped``/``quiet``, a due ``warn``
   or ``action`` row gets the window's end as ``deliver_after`` (``window_end`` is asked again
   here, and an answer of ``None`` is an internal fault, never permission to skip the check);
6. the due ``single`` rows, oldest first, one call each: a transaction claims the row with a
   fresh token and counts the hourly call (a capped row is left alone), the send lock is taken,
   the notifier is called, the lock is released, and the result is recorded in a transaction of
   its own, fenced by the token;
7. the due ``held`` rows: one such row is sent as itself, two or more are folded into one digest
   (:func:`_send_held`, :mod:`notice_route.digest`): a transaction claims every member with one
   shared token and counts one call in the class of the highest level (never held by the cap), the
   call is made with no dedupe key, and one transaction records every member's result.

After a call ends ``queued_retry`` (``lock_busy`` included), ``failed``, ``unknown`` or is deferred
(``env_missing`` found at call time) no further call is made to that destination in the run; its
other rows keep their ``deliver_after``. A trapped signal lets the calls in progress finish, makes
no further call (a send-lock wait ends at once), releases the claims that have not started a call
and ends with the result, or 128+N when no call had started (nothing is printed then); once a call
has started, a signal during the output no longer changes the exit.

Choices where the interface is silent (docs/design.md C-53 has the reasons), all the narrower
reading:

- "Heartbeat" is ``meta``: a run that covers every destination (no ``--dry-run``, no
  ``--destination``) writes ``flush_last`` (its start, UTC epoch seconds) before it starts and
  ``flush_env_missing`` (the same time, or NULL) when it ends. A router with no config or
  ``enabled: false`` reads and writes no state, so it writes none. Claims are not extended: a
  call's lease is twice ``send_worst_case_s`` and covers the lock wait and the call.
- ``due_single`` and ``due_held`` count the due unclaimed rows at the start of the run, before
  step 4 (a row that then expires or is dropped is counted in both places).
- A flush lock that cannot be used (``locks/`` unsafe, any ``OSError``) leaves the destination
  untouched and ends the run 1 (``internal``, fixed message); only a lock another flusher holds is
  ``already_running``. A fault in a destination's thread, and a stored row with an invalid level
  (skipped, one warning, the next row goes on), also end the run 1 with a fixed message.
- A row that was already called ``prior`` times and for which the retry schedule has nothing
  after ``prior`` (a row whose call escaped again and again: ``record_escaped`` adds an attempt each
  time) ends ``failed``/``retries_exhausted`` without a call (the hourly count is given back).
- The send lock being busy, a result whose claim was lost and a signal before a call are all
  handled as in ``send`` (``calling.py``, ``send.record_result``).
- The retry schedule (:mod:`notice_route.retry` by default) and surfacing are seams:
  ``send.Seams`` (``retry_schedule``, ``surface``).
- Digest (docs/design.md C-54): ``digests`` counts the digests built and not released unsent in the
  run; the result counts (``sent``, ``failed``, ...) count member rows, as ``due_held`` does.
"""

from __future__ import annotations

import math
import secrets
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from . import dedup, outbox
from . import log as logmod
from .alerts import HookJob, stored_payload, take_surfaced
from .calling import Busy, Called, CallEnd, Escaped, Gate, NotRun, Skipped, call_row
from .decide import claim_call, count_call, give_back, hour_start, instant
from .digest import Digest, build, record_digest, record_escaped_digest
from .gateway import DirHandle, Gateway, SignalTrap
from .hooks import run_hook, start_detached
from .outcome import LOCK_BUSY, Applied, Outcome, classify, record_escaped
from .overrides import disabled_among
from .payload import LEVELS, UsageError
from .queue import (
    EXPIRY_S,
    Lease,
    QueueRow,
    claim_row,
    complete_row,
    count_due,
    count_expirable,
    drop_unclaimed,
    due_rows,
    expire_unclaimed,
    postpone_unclaimed,
    quiet_deliver_after,
    release_claims,
)
from .report import env_unset, worst_case_s
from .send import Seams, SurfaceEvent, escaped_surface, record_result, with_retry
from .sendlock import flush_lock
from .store import Store, StoreError, open_readonly, open_store
from .surfacing import ALERT_SOURCE, surface_evictions
from .validate import Config, Destination, Hook
from .window import in_window

__all__ = ["FlushOptions", "FlushResult", "run_flush"]

_STOPPING = ("sent", "disabled")  # the classified outcomes after which the run goes on
_ROW_FAULT = "a queue row could not be used"  # fixed texts of an exit 1 (never an exception's)
_ROUTING_FAULT = "a failure alert could not be routed"
_LOCK_FAULT = "a flush lock cannot be used"
_THREAD_FAULT = "internal error"
_HOOK_THREADS = 4  # hooks that run at the same time


@dataclass(frozen=True)
class FlushOptions:
    actor: str
    decision_id: str | None = None
    dry_run: bool = False
    destinations: tuple[str, ...] = ()
    verbose: bool = False


@dataclass
class FlushResult:
    """What the cli prints; the fields are those of :class:`notice_route.send.SendResult` that
    ``flush`` needs (``document`` is ``None`` with ``error`` or when a signal ended the run)."""

    exit_code: int
    document: dict[str, Any] | None = None
    error: tuple[str, str] | None = None
    lines: list[str] = field(default_factory=list[str])
    advisories: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])
    verbose: list[str] = field(default_factory=list[str])
    called: bool = False  # a notifier call started: a signal then no longer changes the exit


@dataclass
class _Tally:
    """What one destination did. ``skipped`` marks a destination a signal kept from starting."""

    name: str
    state: str = "nothing_due"
    due_single: int = 0
    due_held: int = 0
    sent: int = 0
    queued_retry: int = 0
    failed: int = 0
    unknown: int = 0
    dropped: int = 0
    expired: int = 0
    digests: int = 0
    acted: bool = False  # a call, a record, a drop or an expiry happened
    called: bool = False
    skipped: bool = False
    error: bool = False
    fault: str | None = None  # a fixed message: the run ends 1 (internal)
    warnings: list[str] = field(default_factory=list[str])
    verbose: list[str] = field(default_factory=list[str])

    def entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "due_single": self.due_single,
            "due_held": self.due_held,
            "sent": self.sent,
            "queued_retry": self.queued_retry,
            "failed": self.failed,
            "unknown": self.unknown,
            "dropped": self.dropped,
            "expired": self.expired,
            "digests": self.digests,
        }


@dataclass
class _Run:
    gateway: Gateway
    config: Config
    opts: FlushOptions
    trap: SignalTrap
    gate: Gate
    seams: Seams
    worst: float
    started: float
    state: DirHandle | None = None
    writer: logmod.LogWriter | None = None
    log_lock: threading.Lock = field(default_factory=threading.Lock)
    # destinations an operator disabled (``destination disable``), read once when the run starts;
    # every claim transaction asks again
    overrides: frozenset[str] = frozenset()

    def cancelled(self) -> bool:
        """Asked while a send-lock wait is going: a trapped signal ends the wait."""
        return self.trap.received is not None

    def say(self, t: _Tally, text: str) -> None:
        """One ``--verbose`` line: fixed words, a destination name and a row id, nothing else."""
        if self.opts.verbose:
            t.verbose.append(f"{t.name}: {text}")

    def log(self, **fields: Any) -> None:
        if self.writer is None:
            return
        with self.log_lock:
            self.writer.write(
                logmod.record(
                    "delivery",
                    wall=self.gateway.wall(),
                    actor=self.opts.actor,
                    decision_id=self.opts.decision_id,
                    duration_ms=_elapsed_ms(self.gateway, self.started),
                    **fields,
                )
            )

    def log_hook(self, destination: str, mode: str, result: str) -> None:
        if self.writer is None:
            return
        with self.log_lock:
            self.writer.write(
                logmod.record(
                    "hook",
                    wall=self.gateway.wall(),
                    actor=self.opts.actor,
                    decision_id=self.opts.decision_id,
                    duration_ms=_elapsed_ms(self.gateway, self.started),
                    hook="post-fail",
                    destination=destination,
                    mode=mode,
                    result=result,
                )
            )

    def log_flush(self, **fields: Any) -> None:
        if self.writer is None:
            return
        with self.log_lock:
            self.writer.write(
                logmod.record(
                    "flush",
                    wall=self.gateway.wall(),
                    actor=self.opts.actor,
                    decision_id=self.opts.decision_id,
                    duration_ms=_elapsed_ms(self.gateway, self.started),
                    **fields,
                )
            )

    def log_row(
        self,
        row: QueueRow,
        *,
        outcome: str,
        reason: str | None,
        code: str | None,
        attempts: int,
    ) -> None:
        self.log(
            source=row.source,
            rule=row.rule,
            destination=row.destination,
            level=row.level,
            outcome=outcome,
            reason=reason,
            code=code,
            attempts=attempts,
            title_sha256=logmod.sha16(row.title or ""),
            message_sha256=logmod.sha16(row.message),
            dedupe_key_sha256=None if row.dedupe is None else row.dedupe[:16],
        )


def _elapsed_ms(gateway: Gateway, started: float) -> int:
    return max(0, math.floor((gateway.monotonic() - started) * 1000))


# --- the command -----------------------------------------------------------------------------


def run_flush(
    gateway: Gateway,
    config: Config | None,
    opts: FlushOptions,
    started: float,
    trap: SignalTrap,
    seams: Seams = Seams(),  # noqa: B008 - frozen, shared on purpose
) -> FlushResult:
    """Everything after the flags and the config. ``UsageError`` (an unknown ``--destination``),
    ``StoreError`` and ``HomeError`` before any work propagate (exit 2 and 1 in the cli)."""
    doc: dict[str, Any] = {
        "outcome": "no_op",
        "dry_run": opts.dry_run,
        "destinations": [],
        "duration_ms": 0,
    }
    if config is None or not config.destinations:
        return _done(FlushResult(22, doc), gateway, started, advisory="no config")
    unknown = set(opts.destinations) - set(config.destinations)
    if unknown:
        raise UsageError("destination")
    if not config.enabled:
        return _done(FlushResult(22, doc), gateway, started)
    chosen = set(opts.destinations)
    selected = [d for d in config.destinations.values() if not chosen or d.name in chosen]
    trap.armed = False
    gate = Gate(trap, strict=True)
    run = _Run(gateway, config, opts, trap, gate, seams, worst_case_s(config), started)
    if opts.dry_run:
        tallies = _dry_run(run, selected)
        return _finish(run, tallies, FlushResult(0))
    full = not chosen
    wall = instant(gateway.wall())
    with open_store(gateway) as store, gateway.open_state_dir() as state:
        run.state = state
        run.overrides = disabled_among(store.conn, config.destinations)
        run.writer = logmod.LogWriter(gateway, state if config.log else None)
        if full:
            _meta(store, "flush_last", wall)
        taken = _take_surfaced(run, store)
        with (
            ThreadPoolExecutor(max_workers=len(selected)) as pool,
            ThreadPoolExecutor(max_workers=_HOOK_THREADS) as hook_pool,
        ):
            planned = _run_outbox(run, store, hook_pool)
            futures = [pool.submit(_destination, run, d) for d in selected]
            tallies = [f.result() for f in futures]
            attempts = [(p, _attempt_of(p.future)) for p in planned.pending]
        hook_records: list[tuple[str, str, str]] = []  # destination, mode, result
        hook_failures: list[str] = []
        gave_up: list[str] = []
        for destination, word in planned.ended:
            hook_records.append((destination, "sync", "gave_up"))
            gave_up.append(word)
        late: list[_Pending] = []  # a signal came first: started detached instead
        for pending, attempt in attempts:
            if attempt.not_started:
                late.append(pending)
            elif attempt.ran:
                destination = pending.row.destination
                hook_records.append((destination, "sync", attempt.word or "ok"))
                if attempt.word is not None:
                    (gave_up if attempt.gave_up else hook_failures).append(attempt.word)
                if attempt.gave_up:
                    hook_records.append((destination, "sync", "gave_up"))
        result = FlushResult(0)
        if taken.error or planned.error:
            result.error = ("internal", "state database cannot be written")
        if full:
            missing = any(t.state == "env_missing" for t in tallies)
            try:
                _meta(store, "flush_env_missing", wall if missing else None)
            except StoreError:
                result.error = ("internal", "state database cannot be written")
        finished = _finish(run, tallies, result)
        _log_summary(run, tallies)
        # detached: nobody waits for these, and no thread is running now; the supervisor closes
        # every descriptor, so it holds neither the flush lock nor the database
        detached = [(j, h) for j in taken.jobs for h in config.hooks_post_fail if h.is_async]
        for job, hook in detached:
            failed = start_detached(gateway, hook, job.payload)
            hook_records.append((job.destination, "async", failed or "started"))
            if failed is not None:
                hook_failures.append(failed)
        for pending in late:  # logged as mode sync; the row is deleted once it is started
            failed = _start_late(run, store, pending)
            if failed == _GONE:
                continue
            hook_records.append((pending.row.destination, "sync", failed or "started"))
            if failed is not None:
                hook_failures.append(failed)
        for destination, mode, word in hook_records:
            run.log_hook(destination, mode, word)
    for word in dict.fromkeys(gave_up):
        finished.warnings.append(f"a post-fail hook gave up ({word})")
    for word in dict.fromkeys(hook_failures):
        finished.warnings.append(f"a post-fail hook failed ({word})")
    if taken.faults:
        finished.warnings.append(_ROUTING_FAULT)
    if run.writer.problems:
        finished.warnings.append("the log could not be written")
    return finished


@dataclass
class _Attempt:
    """What one worker did with one outbox row."""

    ran: bool = False
    not_started: bool = False  # a signal came first
    word: str | None = None  # the failure word of a run that failed
    gave_up: bool = False  # that run was the last one


@dataclass
class _Pending:
    row: outbox.Row
    hook: Hook
    future: Future[_Attempt]


@dataclass
class _Planned:
    pending: list[_Pending] = field(default_factory=list[_Pending])
    ended: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])  # dest, word
    error: bool = False


def _run_outbox(run: _Run, store: Store, hook_pool: ThreadPoolExecutor) -> _Planned:
    """Plan and start the due outbox rows (design C-62): rows that reached a limit are ended, rows
    of an entry or destination the config no longer has are deleted, and each other due row (at
    most :data:`outbox.RUN_LIMIT`) goes to a worker."""
    planned = _Planned()
    config = run.config
    hooks = config.hooks_post_fail
    by_key = {k: h for k, h in zip(outbox.hook_keys(hooks), hooks, strict=True) if k is not None}
    try:
        with store.write() as conn:
            now = instant(run.gateway.wall())
            planned.ended = [(d, w) for _, d, w in outbox.end_lapsed(conn, now)]
            rows: list[tuple[outbox.Row, Hook]] = []
            for row in outbox.due(conn, now):
                hook = by_key.get(row.hook_key)
                if hook is None or row.destination not in config.destinations:
                    outbox.delete(conn, row.id)
                else:
                    rows.append((row, hook))
    except StoreError:
        planned.error = True
        return planned
    for row, hook in rows:
        future = hook_pool.submit(_outbox_attempt, run, row, hook)
        planned.pending.append(_Pending(row, hook, future))
    return planned


def _outbox_attempt(run: _Run, row: outbox.Row, hook: Hook) -> _Attempt:
    """One attempt: claim the row, run the hook, then delete the row or schedule the next
    attempt. Not started once a signal was trapped (a hook already running is like a call in
    progress and finishes within its timeout)."""
    if run.trap.received is not None:
        return _Attempt(not_started=True)
    token = secrets.token_hex(16)
    lease = math.ceil(hook.timeout) + outbox.LEASE_GRACE_S
    try:
        with open_store(run.gateway) as store:
            with store.write() as conn:
                claimed = outbox.claim(conn, row.id, instant(run.gateway.wall()), lease, token)
            if claimed is None:
                return _Attempt()  # another run has it, or it is not due after all
            payload = stored_payload(run.config, row.destination, claimed.payload)
            if payload is None:
                with store.write() as conn:
                    outbox.succeed(conn, row.id, token)  # not a payload we made: dropped
                return _Attempt()
            try:
                word = run_hook(run.gateway, hook, payload)
            except Exception:  # a hook never changes the result
                word = "lost"
            try:
                with store.write() as conn:
                    if word is None:
                        outbox.succeed(conn, row.id, token)
                        return _Attempt(ran=True)
                    gave_up = outbox.fail(conn, row.id, token, instant(run.gateway.wall()))
            except StoreError:  # the claim lapses; the row is tried again
                return _Attempt(ran=True, word=word)
            return _Attempt(ran=True, word=word, gave_up=gave_up)
    except StoreError:
        return _Attempt()


def _attempt_of(future: Future[_Attempt]) -> _Attempt:
    try:
        return future.result()
    except Exception:  # a worker never changes the result
        return _Attempt(ran=True, word="lost")


_GONE = "gone"  # the row was not there or not ours: nothing was started


def _start_late(run: _Run, store: Store, pending: _Pending) -> str | None:
    """A synchronous hook that a signal kept from starting: started detached once (as in 1.0),
    and its row deleted when that worked. ``None`` when started, ``_GONE`` when there was nothing to
    start, else a failure word."""
    payload = None
    try:
        with store.write() as conn:
            found = conn.execute(
                "SELECT payload FROM hook_outbox WHERE id = ?", (pending.row.id,)
            ).fetchone()
            if found is not None:
                payload = stored_payload(run.config, pending.row.destination, found[0])
                if payload is None:
                    outbox.delete(conn, pending.row.id)
    except StoreError:
        return "spawn_failed"
    if payload is None:
        return _GONE
    failed = start_detached(run.gateway, pending.hook, payload)
    if failed is None:
        try:
            with store.write() as conn:
                outbox.delete(conn, pending.row.id)
        except StoreError:
            pass  # the row stays and is run again: at least once
    return failed


@dataclass
class _TakenRows:
    jobs: list[HookJob]
    error: bool = False
    faults: int = 0


def _take_surfaced(run: _Run, store: Store) -> _TakenRows:
    """Every run (but a dry run) first takes the new ``surfaced`` rows: alerts are routed (rows
    due now, which the destinations' step 6 delivers in this run) and the hook jobs returned."""
    taken = take_surfaced(run.gateway, store, run.config, run.worst)
    for record in taken.log:
        run.log(**record)
    return _TakenRows(taken.jobs, taken.error, taken.faults)


def _meta(store: Store, key: str, value: int | None) -> None:
    with store.write() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _done(
    result: FlushResult, gateway: Gateway, started: float, *, advisory: str | None = None
) -> FlushResult:
    if advisory:
        result.advisories.append(advisory)
    if result.document is not None:
        result.document["duration_ms"] = _elapsed_ms(gateway, started)
    return result


def _finish(run: _Run, tallies: list[_Tally], result: FlushResult) -> FlushResult:
    """The document, the lines and the exit code from the destinations' tallies."""
    for t in tallies:
        result.warnings += t.warnings
        result.verbose += t.verbose
    if run.trap.received is not None and not run.gate.started and not run.opts.dry_run:
        return FlushResult(128 + run.trap.received, warnings=result.warnings)  # nothing was called
    result.called = run.gate.started
    shown = [t for t in tallies if not t.skipped]
    entries = [t.entry() for t in shown]
    failed = any(t.failed or t.unknown for t in shown)
    quiet_states = ("nothing_due", "disabled")
    doc: dict[str, Any] = {
        "outcome": "partial"
        if failed
        else ("no_op" if all(t.state in quiet_states for t in shown) else "ok"),
        "dry_run": run.opts.dry_run,
        "destinations": entries,
        "duration_ms": _elapsed_ms(run.gateway, run.started),
    }
    result.document = doc
    result.lines = [_line(e) for e in entries]
    if result.error is None and any(t.error for t in tallies):
        result.error = ("internal", "state database cannot be written")
    if result.error is None:
        for t in tallies:
            if t.fault is not None:
                result.error = ("internal", t.fault)
                break
    if result.error is not None:
        result.exit_code = 1
    elif failed:
        result.exit_code = 12
    elif doc["outcome"] == "no_op":
        result.exit_code = 22
    return result


def _line(entry: dict[str, Any]) -> str:
    counts = " ".join(
        f"{k}={entry[k]}"
        for k in ("sent", "queued_retry", "failed", "unknown", "dropped", "expired")
        if entry[k]
    )
    return f"{entry['name']}: {entry['state']}" + (f" ({counts})" if counts else "")


def _log_summary(run: _Run, tallies: list[_Tally]) -> None:
    shown = [t for t in tallies if not t.skipped]
    total = {
        k: sum(getattr(t, k) for t in shown)
        for k in ("sent", "queued_retry", "failed", "unknown", "dropped", "expired")
    }
    run.log_flush(
        due=sum(t.due_single + t.due_held for t in shown),
        **total,
    )


# --- one destination -------------------------------------------------------------------------


def _skip_reason(run: _Run, dest: Destination, t: _Tally) -> bool:
    """Steps 1 and 2: ``True`` when the destination is skipped."""
    if not dest.enabled or dest.name in run.overrides:
        t.state = "disabled"
        return True
    if env_unset(dest.env, run.gateway.environ()):
        t.state = "env_missing"
        return True
    return False


def _quiet_window(run: _Run, dest: Destination) -> Any:
    return run.config.quiet_hours[dest.quiet] if dest.quiet is not None else None


def _dry_run(run: _Run, selected: list[Destination]) -> list[_Tally]:
    """Lists what is due and changes nothing (``state.db`` is opened read-only)."""
    tallies: list[_Tally] = []
    with _read_only(run.gateway) as store:
        if store is not None:
            run.overrides = disabled_among(store.conn, run.config.destinations)
        for dest in selected:
            t = _Tally(dest.name)
            tallies.append(t)
            if _skip_reason(run, dest, t) or store is None:
                continue
            lease = Lease.at(run.gateway.wall(), run.worst)
            counts = count_due(store.conn, dest.name, lease)
            t.due_single, t.due_held = counts["single"], counts["held"]
            acts = count_expirable(store.conn, dest.name, lease) > 0  # step 4 (due or not)
            if not (acts or t.due_single or t.due_held):
                continue
            window = _quiet_window(run, dest)
            quiet = window is not None and in_window(window, lease.now)
            left = [r for r in due_rows(store.conn, dest.name, lease) if _unexpired(r, lease)]
            if quiet:  # step 5: info is dropped, warn and action wait for the window's end
                acts = acts or any(r.level == "info" for r in left)
                left = [r for r in left if r.level not in ("info", "warn", "action")]
            held = [r for r in left if r.kind == "held"]
            # steps 6 and 7: a due single row is sent; due held rows are sent as one digest, or
            # one row as itself
            acts = acts or any(r.kind == "single" for r in left) or bool(held)
            t.state = "done" if acts else ("quiet" if quiet else "nothing_due")
    return tallies


def _unexpired(row: QueueRow, lease: Lease) -> bool:
    return row.created_at > lease.now - EXPIRY_S


class _read_only:  # noqa: N801 - a context manager that tolerates "no database"
    def __init__(self, gateway: Gateway) -> None:
        self._gateway = gateway
        self._store: Store | None = None

    def __enter__(self) -> Store | None:
        self._store = open_readonly(self._gateway)
        return self._store

    def __exit__(self, *exc: object) -> None:
        if self._store is not None:
            self._store.close()


def _destination(run: _Run, dest: Destination) -> _Tally:
    t = _Tally(dest.name)
    try:
        if _skip_reason(run, dest, t):
            if t.state == "env_missing":
                _surface_env_missing(run, dest, t)
            return t
        if run.trap.received is not None:
            t.skipped = True
            return t
        assert run.state is not None
        with (
            open_store(run.gateway) as store,
            flush_lock(run.gateway, run.state, dest.name) as held,
        ):
            if not held.acquired:
                if held.busy_reason == "timeout":  # another flusher holds it
                    t.state = "already_running"
                    run.say(t, "already_running")
                else:  # the lock cannot be used: the destination is left untouched, exit 1
                    t.fault = _LOCK_FAULT
                return t
            _locked(run, store, dest, t)
    except StoreError:  # the text of an exception is never kept
        t.error = True
    except Exception:  # a fault of the thread: its other destinations still report
        t.fault = _THREAD_FAULT
    return t


def _surface_env_missing(run: _Run, dest: Destination, t: _Tally) -> None:
    """The destination is skipped for an unset variable: surface ``env_missing`` (once per UTC
    hour) for its oldest unclaimed due row, one that is not a failure alert when there is one. A
    destination with nothing due and unclaimed has nothing waiting on the credential, so it
    surfaces nothing."""
    try:
        with open_store(run.gateway) as store, store.write() as conn:
            lease = Lease.at(run.gateway.wall(), run.worst)
            due = due_rows(conn, dest.name, lease)
            picked = next((r for r in due if r.source != ALERT_SOURCE), due[0] if due else None)
            if picked is not None:
                attempts = conn.execute(
                    "SELECT attempts FROM deliveries WHERE id = ?", (picked.delivery_id,)
                ).fetchone()
                run.seams.surface(
                    conn,
                    SurfaceEvent(
                        picked.delivery_id, dest.name, dest.kind, picked.source, picked.level,
                        "deferred", "env_missing", int(attempts[0]) if attempts else 0, lease.now,
                    ),
                )  # fmt: skip
    except StoreError:
        t.error = True


def _locked(run: _Run, store: Store, dest: Destination, t: _Tally) -> None:
    _prepare(run, store, dest, t)
    stop = False
    skipped: set[int] = set()
    while not stop and run.trap.received is None:
        claim = _claim(run, store, dest, "single", skipped, t)
        if claim is None:
            break
        if isinstance(claim, _Exhausted):  # no call was made: the next row is still due
            continue
        stop = _call_and_record(run, store, dest, t, claim)
    if not stop and run.trap.received is None:
        _send_held(run, store, dest, t)
    if t.acted:
        t.state = "done"
    elif t.due_single or t.due_held:
        window = _quiet_window(run, dest)
        if window is not None and in_window(window, run.gateway.wall()):
            t.state = "quiet"


def _prepare(run: _Run, store: Store, dest: Destination, t: _Tally) -> None:
    """Steps 4 and 5 in one transaction: count the due rows, expire the old ones, then handle the
    due rows of a quiet destination."""
    log_rows: list[tuple[QueueRow, str, str | None, str | None]] = []
    with store.write() as conn:
        lease = Lease.at(run.gateway.wall(), run.worst)
        counts = count_due(conn, dest.name, lease)
        t.due_single, t.due_held = counts["single"], counts["held"]
        expired = expire_unclaimed(conn, lease, destination=dest.name)
        if expired:  # one ``expired`` surfacing per destination per UTC hour, no alert's if any
            e = next((x for x in expired if x.source != ALERT_SOURCE), expired[0])
            run.seams.surface(
                conn,
                SurfaceEvent(
                    e.delivery_id, dest.name, dest.kind, e.source, e.level,
                    "expired", "expired", e.attempts, lease.now,
                ),
            )  # fmt: skip
        t.expired = len(expired)
        t.acted = bool(expired)
        window = _quiet_window(run, dest)
        dropped = 0
        if window is not None and in_window(window, lease.now):
            end: int | None = None
            for row in due_rows(conn, dest.name, lease):
                if row.level == "info":
                    if drop_unclaimed(conn, row.id, lease, reason="quiet"):
                        dropped += 1
                        log_rows.append((row, "dropped", "quiet", None))
                        run.say(t, f"row {row.id} dropped (quiet)")
                elif row.level in ("warn", "action"):
                    if end is None:
                        end = quiet_deliver_after(window, lease.now)  # None is an error
                    postpone_unclaimed(conn, row.id, lease, end)
                    run.say(t, f"row {row.id} postponed (quiet)")
        t.dropped = dropped
        t.acted = t.acted or dropped > 0
    for row, outcome, reason, code in log_rows:
        run.log_row(row, outcome=outcome, reason=reason, code=code, attempts=0)
    for e in expired:
        run.say(t, f"row {e.queue_id} expired")
        run.log(
            source=e.source,
            destination=e.destination,
            level=e.level,
            outcome="expired",
            code="expired",
            attempts=e.attempts,
        )


@dataclass(frozen=True)
class _Claim:
    row: QueueRow
    token: str
    count_hour: int
    prior: int  # the attempts the row had before this call


@dataclass(frozen=True)
class _Exhausted:
    """A claimed row ended without a call: its attempts were used up."""


def _claim(
    run: _Run, store: Store, dest: Destination, kind: str, skipped: set[int], t: _Tally
) -> _Claim | _Exhausted | None:
    """A transaction that claims the next due row of ``kind`` for one call: the hourly count is
    taken (a row whose class is at ``max_per_hour`` is left alone), a fresh token is written
    and, for a row whose attempts are used up, the row ends without a call."""
    exhausted: list[tuple[QueueRow, int]] = []
    claim: _Claim | None = None
    with store.write() as conn:
        lease = Lease.at(run.gateway.wall(), run.worst)
        if disabled_among(conn, (dest.name,)):  # disabled since the run began: no further claim
            return None
        after = max(skipped, default=0)  # rows are read in id order: step past the skipped ones
        while True:
            rows = due_rows(conn, dest.name, lease, kind=kind, limit=1, after_id=after)  # type: ignore[arg-type]
            if not rows:
                break
            row = rows[0]
            after = row.id
            try:
                counted = claim_call(
                    conn,
                    destination=dest.name,
                    level=row.level,
                    now=lease.now,
                    cap=dest.max_per_hour,
                )
            except ValueError:  # a stored level that is none of the four: leave the row alone
                skipped.add(row.id)
                if _ROW_FAULT not in t.warnings:
                    t.warnings.append(_ROW_FAULT)
                t.fault = t.fault or _ROW_FAULT
                run.say(t, f"row {row.id} skipped (invalid level)")
                continue
            if not counted:
                skipped.add(row.id)  # at the cap: waits for the next UTC hour
                run.say(t, f"row {row.id} skipped (rate_capped)")
                continue
            token = run.gateway.new_token()
            claimed = claim_row(conn, row.id, lease, token)
            assert claimed is not None  # inside BEGIN IMMEDIATE
            hour = hour_start(lease.now)
            prior = int(
                conn.execute(
                    "SELECT attempts FROM deliveries WHERE id = ?", (row.delivery_id,)
                ).fetchone()[0]
            )
            retry_at = run.seams.retry_schedule(dest.kind, lease.now)
            if prior >= 1 and retry_at(prior) is None:
                _exhaust(conn, run, dest, claimed, token, prior, hour, lease.now)
                exhausted.append((claimed, prior))
                break
            claim = _Claim(claimed, token, hour, prior)
            break
    for row, prior in exhausted:
        t.failed += 1
        t.acted = True
        run.say(t, f"row {row.id} failed (retries_exhausted)")
        run.log_row(row, outcome="failed", reason=None, code="retries_exhausted", attempts=prior)
        return _Exhausted()
    return claim


def _exhaust(
    conn: sqlite3.Connection,
    run: _Run,
    dest: Destination,
    row: QueueRow,
    token: str,
    prior: int,
    hour: int | None,
    now: int,
) -> None:
    """End a claimed row ``failed``/``retries_exhausted`` without a call; ``hour`` is where its
    hourly count is given back (``None``: it took none, a digest member)."""
    complete_row(conn, row.id, token)
    conn.execute(
        "UPDATE deliveries SET outcome = 'failed', code = 'retries_exhausted',"
        " deliver_after = NULL WHERE id = ?",
        (row.delivery_id,),
    )
    dedup.release(conn, row.delivery_id)
    if hour is not None:
        give_back(conn, destination=dest.name, level=row.level, hour=hour)
    run.seams.surface(
        conn,
        SurfaceEvent(
            row.delivery_id,
            dest.name,
            dest.kind,
            row.source,
            row.level,
            "failed",
            "retries_exhausted",
            prior,
            now,
        ),
    )


def _send_held(run: _Run, store: Store, dest: Destination, t: _Tally) -> None:
    """Step 7. Two or more usable due ``held`` rows become one digest; otherwise the one row left
    is sent as itself (the hourly cap applies to it)."""
    while run.trap.received is None:
        found = _claim_digest(run, store, dest, t)
        if isinstance(found, _Exhausted):  # members ended without a call: look again
            continue
        if found is not None:
            _call_digest(run, store, dest, t, found)
            return
        break
    if run.trap.received is not None:
        return
    claim = _claim(run, store, dest, "held", set(), t)
    if isinstance(claim, _Claim):
        _call_and_record(run, store, dest, t, claim)


@dataclass(frozen=True)
class _DigestClaim:
    digest: Digest
    token: str
    count_hour: int
    call_row: QueueRow  # what the call sends: the digest's text under the first member's ids
    priors: dict[int, int]  # each member's attempts before the call, by delivery id


def _claim_digest(
    run: _Run, store: Store, dest: Destination, t: _Tally
) -> _DigestClaim | _Exhausted | None:
    """One transaction: claim every usable due ``held`` row with one fresh token and count one
    call in the class of the highest level (no cap check: a digest is never held by the cap). A
    member whose attempts are used up ends ``failed`` without a call and is left out. ``None``
    when fewer than two members are left (their claims are released)."""
    spent: list[tuple[QueueRow, int]] = []
    result: _DigestClaim | None = None
    with store.write() as conn:
        lease = Lease.at(run.gateway.wall(), run.worst)
        if disabled_among(conn, (dest.name,)):  # disabled since the run began: no further claim
            return None
        usable: list[QueueRow] = []
        for row in due_rows(conn, dest.name, lease, kind="held"):
            if row.level in LEVELS:
                usable.append(row)
            else:  # a stored level that is none of the four: leave the row alone
                if _ROW_FAULT not in t.warnings:
                    t.warnings.append(_ROW_FAULT)
                t.fault = t.fault or _ROW_FAULT
                run.say(t, f"row {row.id} skipped (invalid level)")
        if len(usable) < 2:
            return None
        token = run.gateway.new_token()
        retry_at = run.seams.retry_schedule(dest.kind, lease.now)
        live: list[QueueRow] = []
        priors: dict[int, int] = {}
        for row in usable:
            claimed = claim_row(conn, row.id, lease, token)
            assert claimed is not None  # inside BEGIN IMMEDIATE
            prior = int(
                conn.execute(
                    "SELECT attempts FROM deliveries WHERE id = ?", (row.delivery_id,)
                ).fetchone()[0]
            )
            if prior >= 1 and retry_at(prior) is None:
                _exhaust(conn, run, dest, claimed, token, prior, None, lease.now)
                spent.append((claimed, prior))
            else:
                live.append(claimed)
                priors[claimed.delivery_id] = prior
        if len(live) >= 2:
            digest = build(live)
            count_call(conn, destination=dest.name, level=digest.level, now=lease.now)
            first = digest.rows[0]
            sent_as = QueueRow(
                first.id, first.delivery_id, dest.name, "held", digest.title, digest.message,
                None, digest.level, first.source, None, None, lease.now, lease.now, token,
                lease.expires,
            )  # fmt: skip
            result = _DigestClaim(digest, token, hour_start(lease.now), sent_as, priors)
        else:
            surface_evictions(conn, release_claims(conn, token, lease).evicted, lease.now)
    for row, prior in spent:
        t.failed += 1
        t.acted = True
        run.say(t, f"row {row.id} failed (retries_exhausted)")
        run.log_row(row, outcome="failed", reason=None, code="retries_exhausted", attempts=prior)
    if result is not None:
        run.say(t, f"digest of {len(result.digest.rows)} rows")
        return result
    return _Exhausted() if spent else None


def _call_digest(
    run: _Run, store: Store, dest: Destination, t: _Tally, claim: _DigestClaim
) -> None:
    """Call for the claimed digest (no dedupe key), then record every member in one transaction."""
    assert run.state is not None
    members = claim.digest.rows
    end: CallEnd = call_row(
        run.gateway,
        dest,
        claim.call_row,
        run.state,
        run.config.lock_wait,
        None,
        run.seams.call,
        run.gate,
        run.cancelled,
    )
    if isinstance(end, (Skipped, NotRun)):  # no call was made: release every member and the count
        _give_back(run, store, dest, _Claim(claim.call_row, claim.token, claim.count_hour, 0))
        run.say(t, "digest not called (released)")
        t.error = t.error or isinstance(end, NotRun)
        return
    t.digests += 1  # built, claimed and not released unsent by a signal
    if isinstance(end, Escaped):
        t.called = t.acted = True
        recorded: list[int | None] | None = None
        try:
            recorded = with_retry(
                run.gateway,
                claim.call_row,
                lambda: record_escaped_digest(
                    store, members, claim.token, _digest_surface(run, dest)
                ),
            )
        except StoreError:
            t.error = True
        t.unknown += len(members)
        run.say(t, "digest unknown")
        for i, m in enumerate(members):
            new = None if recorded is None else recorded[i]
            if new is None:  # nothing recorded (a failed write) or the claim was lost
                new = _attempts_of(store, claim, m, 1, read=recorded is not None)
            run.log_row(m, outcome="unknown", reason="digest", code="killed", attempts=new)
        return
    outcome: Outcome = (
        LOCK_BUSY
        if isinstance(end, Busy)
        else classify(end.result, kind=dest.kind, has_dedupe_key=False)
    )
    if isinstance(end, Called):
        t.called = True
    try:
        done = record_digest(
            store, run.gateway, run.seams, run.worst, dest, members, claim.token, outcome,
            claim.count_hour,
        )  # fmt: skip
    except Exception:  # StoreError or a fault in the seam: the claims lapse, flush resends
        t.error = True
        t.acted = True
        for _ in members:
            _tally(t, outcome.outcome or "unknown", 0)
        return
    if outcome.internal:  # not_started: nothing is recorded, the claims lapse
        t.error = True
        return
    t.acted = True
    lost = False
    for m, a in zip(members, done.applied, strict=True):
        if not a.applied:
            lost = True
            name = outcome.outcome or "unknown"
            _tally(t, name, 0)
            run.log_row(
                m,
                outcome=name,
                reason=outcome.reason or "digest",
                code=outcome.code,
                attempts=_attempts_of(store, claim, m, 1 if outcome.counted else 0, read=True),
            )
            continue
        assert a.outcome is not None
        if m.delivery_id in done.evicted_ids and a.outcome in ("queued_retry", "deferred"):
            # dropped by the cap when a later member went back
            _tally(t, "dropped", 0)
            run.log_row(
                m, outcome="dropped", reason="overflow", code="overflow", attempts=a.attempts
            )
            continue
        _tally(t, a.outcome, 0)
        run.log_row(m, outcome=a.outcome, reason=a.reason, code=a.code, attempts=a.attempts)
    others = done.evicted_ids - {m.delivery_id for m in members}
    t.dropped += len(others)
    if lost:
        t.warnings.append("a result was logged only: the claim was lost")
    run.say(t, f"digest {outcome.outcome or 'unknown'}")


def _digest_surface(
    run: _Run, dest: Destination
) -> Callable[[sqlite3.Connection, QueueRow, int], None]:
    """Surfacing for an escaped digest call: once, for the first member recorded."""

    def surface(conn: sqlite3.Connection, member: QueueRow, attempts: int) -> None:
        run.seams.surface(
            conn,
            SurfaceEvent(
                member.delivery_id,
                dest.name,
                dest.kind,
                member.source,
                member.level,
                "unknown",
                "killed",
                attempts,
                instant(run.gateway.wall()),
            ),  # fmt: skip
        )

    return surface


def _attempts_of(
    store: Store, claim: _DigestClaim, row: QueueRow, counted: int, *, read: bool
) -> int:
    """The attempts of a digest member whose result was not recorded, for its log line: what the
    delivery has now (a read, no write transaction) when ``read`` and the row is still there,
    else the attempts it had before the call plus ``counted``, as for a single row."""
    if read:
        try:
            found = store.read("SELECT attempts FROM deliveries WHERE id = ?", (row.delivery_id,))
        except StoreError:
            found = []
        if found:
            return int(str(found[0][0]))
    return claim.priors[row.delivery_id] + counted


# --- one call --------------------------------------------------------------------------------


def _call_and_record(run: _Run, store: Store, dest: Destination, t: _Tally, claim: _Claim) -> bool:
    """Call for the claimed row (send lock, call, unlock), then record. ``True`` when the run
    makes no further call to this destination."""
    assert run.state is not None
    row, token = claim.row, claim.token
    end: CallEnd = call_row(
        run.gateway,
        dest,
        row,
        run.state,
        run.config.lock_wait,
        row.dedupe,
        run.seams.call,
        run.gate,
        run.cancelled,
    )
    if isinstance(end, (Skipped, NotRun)):  # no call was made: give the row and the count back
        _give_back(run, store, dest, claim)
        run.say(t, f"row {row.id} not called (released)")
        t.error = t.error or isinstance(end, NotRun)
        return True
    if isinstance(end, Escaped):
        t.called = t.acted = True
        t.unknown += 1
        try:
            with_retry(
                run.gateway,
                row,
                lambda: record_escaped(
                    store,
                    row=row,
                    token=token,
                    surface=escaped_surface(run.gateway, run.seams, dest, row),
                ),
            )
        except StoreError:
            t.error = True
        run.say(t, f"row {row.id} unknown")
        run.say(t, "stop after unknown")
        run.log_row(row, outcome="unknown", reason=None, code="killed", attempts=claim.prior + 1)
        return True
    outcome: Outcome = (
        LOCK_BUSY
        if isinstance(end, Busy)
        else classify(end.result, kind=dest.kind, has_dedupe_key=row.dedupe is not None)
    )
    if isinstance(end, Called):
        t.called = True
    try:
        applied = record_result(
            store, run.gateway, run.seams, run.worst, dest, row, token, outcome, claim.count_hour
        )
    except Exception:  # StoreError or a fault in the seam: the claim lapses, flush resends
        t.error = True
        t.acted = True
        _tally(t, outcome.outcome or "unknown", 0)
        return True
    if applied.internal:  # not_started: nothing is recorded, the claim lapses
        t.error = True
        return True
    t.acted = True
    if not applied.applied:
        t.warnings.append("a result was logged only: the claim was lost")
        _tally(t, outcome.outcome or "unknown", 0)
        run.log_row(
            row,
            outcome=outcome.outcome or "unknown",
            reason=outcome.reason,
            code=outcome.code,
            attempts=claim.prior + (1 if outcome.counted else 0),
        )
    else:
        assert applied.outcome is not None
        _tally(t, applied.outcome, len(_others(applied, row)))
        run.log_row(
            row,
            outcome=applied.outcome,
            reason=applied.reason,
            code=applied.code,
            attempts=applied.attempts,
        )
    run.say(t, f"row {row.id} {outcome.outcome or 'unknown'}")
    stop = outcome.outcome not in _STOPPING
    if stop:
        run.say(t, f"stop after {outcome.outcome or 'unknown'}")
    return stop


def _others(applied: Applied, row: QueueRow) -> list[Any]:
    """Rows of the destination the cap dropped when this result was requeued (not the row)."""
    return [e for e in applied.evicted if e.delivery_id != row.delivery_id]


def _tally(t: _Tally, outcome: str, evicted_others: int) -> None:
    if outcome in ("sent", "queued_retry", "failed", "unknown", "dropped"):
        setattr(t, outcome, getattr(t, outcome) + 1)
    t.dropped += evicted_others


def _give_back(run: _Run, store: Store, dest: Destination, claim: _Claim) -> None:
    """Release the claim of a row whose call was never made and return its hourly count."""
    try:
        with store.write() as conn:
            lease = Lease.at(run.gateway.wall(), run.worst)
            surface_evictions(conn, release_claims(conn, claim.token, lease).evicted, lease.now)
            give_back(conn, destination=dest.name, level=claim.row.level, hour=claim.count_hour)
    except StoreError:
        pass  # the claim lapses by itself
