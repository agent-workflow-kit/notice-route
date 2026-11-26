"""``validate --probe`` (#29): ask each enabled known-kind notifier whether it is ready.

One call per probed destination, made like a send: ``<cmd> validate --json`` (plus ``--probe``
for ``ha-notice`` and ``wsl-notice``) through :func:`notice_route.notify.probe_notifier` (the
scrubbed environment, own process group, deadline and output caps of ``Gateway.run``), under the
destination's send lock (``wsl-notice`` shares ``_wsl``). It never sends, never passes
``--test-message`` and writes nothing: no queue row, no history, no log record, no count, no
``state.db``. The only files it creates are the lock files of ``send_lock``.

Which destinations: enabled, of kind ``slack-notice``, ``ha-notice`` or ``wsl-notice``, with
every named ``env`` variable set (a destination with an unset name, ``generic`` and a disabled
one are not probed: their report is null).

Classification (interface: validate): exit 0 is ``ok``, exit 22 is ``disabled``, anything else is
``failed``. ``exit`` is the exit code, or null when the call was killed at its deadline, killed for
its output size, ended by a signal, never started (``cmd[0]`` check, spawn failure, a busy or
unusable lock, a signal that came before its turn) or was lost. ``enabled`` is the notifier's own
value: the boolean ``enabled`` of a stdout that parses as one strict JSON object, with exit 0 or
22; for exit 22 whose object says ``{"outcome": "disabled"}`` it is false; null otherwise.

The notifier's stdout and stderr are never echoed, stored or logged: only the three fixed fields
leave this module, so a hostile or secret-bearing answer cannot reach the output.

Choices where the interface is silent (the narrower reading):

- A destination whose ``cmd[0]`` fails the check is probed and is ``failed`` with a null exit
  (the call "cannot be started"); the lock is not taken for it.
- Destinations are probed one after the other in config order (a send runs them in parallel).
  A signal ends the wait for a lock and stops later probes; the call in progress finishes within
  its own deadline. Probes that never got their turn are ``failed`` with a null exit; when no call
  started at all the command exits 128+N with no output.
- ``NOTICE_ROUTE_HOOK`` does not refuse a probe: the recursion guard is that of ``send`` and
  ``flush`` (it never leaves the router's environment into the call either way).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .gateway import STDOUT_LIMIT, Gateway, SignalTrap
from .notify import PROBED_KINDS, CallResult, build_env, probe_notifier
from .payload import StrictJsonError, parse_json_object
from .sendlock import send_lock
from .validate import Config, Destination

__all__ = ["ProbeResult", "Probes", "classify_probe", "probe_targets", "run_probes"]

ProbeStatus = Literal["ok", "failed", "disabled"]
EXIT_OK = 0
EXIT_DISABLED = 22


@dataclass(frozen=True)
class ProbeResult:
    status: ProbeStatus
    exit: int | None
    enabled: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "exit": self.exit, "enabled": self.enabled}


NOT_RUN = ProbeResult("failed", None, None)


@dataclass(frozen=True)
class Probes:
    """``results`` by destination name (a destination that is not probed has no entry);
    ``signal`` is the signal number to exit with (128+N) when one arrived before any call
    started, else None."""

    results: dict[str, ProbeResult]
    signal: int | None = None


def _enabled(stdout: bytes, rc: int) -> bool | None:
    try:
        found = parse_json_object(stdout, max_bytes=STDOUT_LIMIT)
    except StrictJsonError:
        return None
    value = found.get("enabled")
    if type(value) is bool:
        return value
    if rc == EXIT_DISABLED and found.get("outcome") == "disabled":
        return False
    return None


def classify_probe(call: CallResult) -> ProbeResult:
    """The report of one finished call. Never echoes any of its output."""
    run = call.run
    if call.status != "ran" or run is None or run.status != "exited" or run.returncode is None:
        return NOT_RUN  # not started, lost, killed, overflowed or ended by a signal
    rc = run.returncode
    if rc == EXIT_OK:
        return ProbeResult("ok", rc, _enabled(run.stdout, rc))
    if rc == EXIT_DISABLED:
        return ProbeResult("disabled", rc, _enabled(run.stdout, rc))
    return ProbeResult("failed", rc, None)


def probe_targets(config: Config, gateway: Gateway) -> list[Destination]:
    """The destinations to probe, in config order."""
    out: list[Destination] = []
    for d in config.destinations.values():
        if not d.enabled or d.kind not in PROBED_KINDS:
            continue
        try:
            if build_env(gateway, d).missing:
                continue
        except ValueError:
            continue
        out.append(d)
    return out


def run_probes(gateway: Gateway, config: Config, trap: SignalTrap) -> Probes:
    targets = probe_targets(config, gateway)
    if not targets:
        return Probes({})  # no state directory is opened for nothing
    results: dict[str, ProbeResult] = {}
    started = False

    def stop() -> bool:
        return trap.received is not None

    with gateway.open_state_dir() as state:
        for d in targets:
            if stop():
                results[d.name] = NOT_RUN
                continue
            if gateway.resolve_exec(d.cmd[0]) is None:
                results[d.name] = NOT_RUN
                continue
            with send_lock(gateway, state, d.name, d.kind, config.lock_wait, stop) as held:
                if not held.acquired:
                    results[d.name] = NOT_RUN  # busy (or cancelled): no call, no row
                    continue
                call = probe_notifier(gateway, d)
                started = started or call.status in ("ran", "lost")
            results[d.name] = classify_probe(call)
    if trap.received is not None and not started:
        return Probes(results, trap.received)
    return Probes(results)
