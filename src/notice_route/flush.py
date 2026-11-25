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
7. the due ``held`` rows: one such row is sent as itself. TODO(#24): two or more are folded into
   a digest (:func:`_send_held`); until then they are left untouched.

After a call ends ``queued_retry`` (``lock_busy`` included), ``failed``, ``unknown`` or is deferred
(``env_missing`` found at call time) no further call is made to that destination in the run; its
other rows keep their ``deliver_after``. A trapped signal lets the calls in progress finish, makes
no further call, releases the claims that have not started a call and ends with the result, or
128+N when no call had started (nothing is printed then).

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
  untouched as ``already_running`` and adds one warning.
- A row that was already called ``prior`` times and for which the retry schedule has nothing
  after ``prior`` (a row whose call escaped again and again: ``record_escaped`` adds an attempt each
  time) ends ``failed``/``retries_exhausted`` without a call (the hourly count is given back).
- The send lock being busy, a result whose claim was lost and a signal before a call are all
  handled as in ``send`` (``calling.py``, ``send.record_result``).
- A digest, the retry schedule and surfacing are seams: ``send.Seams`` (``retry_schedule``,
  ``surface``; TODO(#25), TODO(#27)).
"""

from __future__ import annotations

import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from . import dedup
from . import log as logmod
from .calling import Busy, Called, CallEnd, Escaped, Gate, NotRun, Skipped, call_row
from .decide import claim_call, give_back, hour_start, instant
from .gateway import DirHandle, Gateway, SignalTrap
from .outcome import LOCK_BUSY, Applied, Outcome, classify, record_escaped
from .payload import UsageError
from .queue import (
    Lease,
    QueueRow,
    claim_row,
    complete_row,
    count_due,
    drop_unclaimed,
    due_rows,
    expire_unclaimed,
    postpone_unclaimed,
    quiet_deliver_after,
    release_claims,
)
from .report import env_unset, worst_case_s
from .send import Seams, SurfaceEvent, record_result, with_retry
from .sendlock import flush_lock
from .store import Store, StoreError, open_readonly, open_store
from .validate import Config, Destination
from .window import in_window

__all__ = ["FlushOptions", "FlushResult", "run_flush"]

_STOPPING = ("sent", "disabled")  # the classified outcomes after which the run goes on


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
        run.writer = logmod.LogWriter(gateway, state if config.log else None)
        if full:
            _meta(store, "flush_last", wall)
        with ThreadPoolExecutor(max_workers=len(selected)) as pool:
            futures = [pool.submit(_destination, run, d) for d in selected]
            tallies = [f.result() for f in futures]
        result = FlushResult(0)
        if full:
            missing = any(t.state == "env_missing" for t in tallies)
            try:
                _meta(store, "flush_env_missing", wall if missing else None)
            except StoreError:
                result.error = ("internal", "state database cannot be written")
        finished = _finish(run, tallies, result)
        _log_summary(run, tallies)
    if run.writer.problems:
        finished.warnings.append("the log could not be written")
    return finished


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
    if not dest.enabled:
        t.state = "disabled"
        return True
    if env_unset(dest.env, run.gateway.environ()):
        t.state = "env_missing"  # TODO(#27): surfaced as env_missing (once per hour)
        return True
    return False


def _quiet_window(run: _Run, dest: Destination) -> Any:
    return run.config.quiet_hours[dest.quiet] if dest.quiet is not None else None


def _dry_run(run: _Run, selected: list[Destination]) -> list[_Tally]:
    """Lists what is due and changes nothing (``state.db`` is opened read-only)."""
    tallies: list[_Tally] = []
    with _read_only(run.gateway) as store:
        for dest in selected:
            t = _Tally(dest.name)
            tallies.append(t)
            if _skip_reason(run, dest, t) or store is None:
                continue
            lease = Lease.at(run.gateway.wall(), run.worst)
            counts = count_due(store.conn, dest.name, lease)
            t.due_single, t.due_held = counts["single"], counts["held"]
            if not (t.due_single or t.due_held):
                continue
            window = _quiet_window(run, dest)
            quiet = window is not None and in_window(window, lease.now)
            levels = {r.level for r in due_rows(store.conn, dest.name, lease)}
            would_act = not quiet or bool(levels & {"info", "error"})
            t.state = "done" if would_act else "quiet"
    return tallies


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
                t.state = "already_running"
                if held.busy_reason != "timeout":
                    t.warnings.append(f"the flush lock of {dest.name} cannot be used")
                return t
            _locked(run, store, dest, t)
    except Exception:  # StoreError or a fault: the text of an exception is never kept
        t.error = True
    return t


def _locked(run: _Run, store: Store, dest: Destination, t: _Tally) -> None:
    _prepare(run, store, dest, t)
    stop = False
    skipped: set[int] = set()
    while not stop and run.trap.received is None:
        claim = _claim(run, store, dest, "single", skipped, t)
        if claim is None:
            break
        stop = isinstance(claim, _Exhausted) or _call_and_record(run, store, dest, t, claim)
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
        # TODO(#27): surface each expired row (code expired, once per destination per UTC hour)
        expired = expire_unclaimed(conn, lease, destination=dest.name)
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
                elif row.level in ("warn", "action"):
                    if end is None:
                        end = quiet_deliver_after(window, lease.now)  # None is an error
                    postpone_unclaimed(conn, row.id, lease, end)
        t.dropped = dropped
        t.acted = t.acted or dropped > 0
    for row, outcome, reason, code in log_rows:
        run.log_row(row, outcome=outcome, reason=reason, code=code, attempts=0)
    for e in expired:
        run.log(destination=e.destination, level=e.level, outcome="expired", code="expired")


@dataclass(frozen=True)
class _Claim:
    row: QueueRow
    token: str
    count_hour: int


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
        while True:
            rows = due_rows(conn, dest.name, lease, kind=kind, limit=len(skipped) + 1)  # type: ignore[arg-type]
            fresh = [r for r in rows if r.id not in skipped]
            if not fresh:
                break
            row = fresh[0]
            if not claim_call(
                conn,
                destination=dest.name,
                level=row.level,
                now=lease.now,
                cap=dest.max_per_hour,
            ):
                skipped.add(row.id)  # at the cap: waits for the next UTC hour
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
                _exhaust(conn, run, dest, claimed, token, prior, hour)
                exhausted.append((claimed, prior))
                break
            claim = _Claim(claimed, token, hour)
            break
    for row, prior in exhausted:
        t.failed += 1
        t.acted = True
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
    hour: int,
) -> None:
    complete_row(conn, row.id, token)
    conn.execute(
        "UPDATE deliveries SET outcome = 'failed', code = 'retries_exhausted',"
        " deliver_after = NULL WHERE id = ?",
        (row.delivery_id,),
    )
    dedup.release(conn, row.delivery_id)
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
        ),
    )


def _send_held(run: _Run, store: Store, dest: Destination, t: _Tally) -> None:
    """Step 7. One due ``held`` row is sent as itself. TODO(#24): two or more become one digest
    (title ``N held notifications``, claim all, count in the highest class, per-row results);
    until then they are left as they are."""
    with store.write() as conn:
        lease = Lease.at(run.gateway.wall(), run.worst)
        due = due_rows(conn, dest.name, lease, kind="held")
    if len(due) != 1:
        return
    claim = _claim(run, store, dest, "held", set(), t)
    if isinstance(claim, _Claim):
        _call_and_record(run, store, dest, t, claim)


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
    )
    if isinstance(end, (Skipped, NotRun)):  # no call was made: give the row and the count back
        _give_back(run, store, dest, claim)
        t.error = t.error or isinstance(end, NotRun)
        return True
    if isinstance(end, Escaped):
        t.called = t.acted = True
        t.unknown += 1
        try:
            with_retry(run.gateway, row, lambda: record_escaped(store, row=row, token=token))
        except StoreError:
            t.error = True
        run.log_row(row, outcome="unknown", reason=None, code="killed", attempts=1)
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
            attempts=1 if outcome.counted else 0,
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
    return outcome.outcome not in _STOPPING


def _others(applied: Applied, row: QueueRow) -> list[Any]:
    """Rows of the destination the cap dropped when this result was requeued (not the row)."""
    return [e for e in applied.evicted if e.delivery_id != row.delivery_id]


def _tally(t: _Tally, outcome: str, evicted_others: int) -> None:
    # TODO(#27): surface overflow evictions (``applied.evicted``) once per destination per hour
    if outcome in ("sent", "queued_retry", "failed", "unknown", "dropped"):
        setattr(t, outcome, getattr(t, outcome) + 1)
    t.dropped += evicted_others


def _give_back(run: _Run, store: Store, dest: Destination, claim: _Claim) -> None:
    """Release the claim of a row whose call was never made and return its hourly count."""
    try:
        with store.write() as conn:
            release_claims(conn, claim.token, Lease.at(run.gateway.wall(), run.worst))
            give_back(conn, destination=dest.name, level=claim.row.level, hour=claim.count_hour)
    except StoreError:
        pass  # the claim lapses by itself
