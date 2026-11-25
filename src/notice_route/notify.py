"""The notifier call (#19): the argv, the scrubbed environment, the stdin payload and the spawn.

docs/interface.md (Calling the notifiers) is normative. One call is
``cmd... send --stdin-json --json --actor auto:notice-route [--dedupe-key=<hex>]``, with the
notification as one line of JSON on stdin and a child environment that holds only the allow-list.

Safety properties:

- Argv holds only configuration (the resolved ``cmd[0]``, the rest of ``cmd``) and fixed flags;
  :func:`build_argv` takes no notification field at all. The one non-config word is the dedupe
  digest, which must be lowercase hex.
- The environment is built from nothing: ``PATH`` (cleaned by the gateway), ``LANG`` and
  ``LC_ALL``, for ``wsl-notice`` the two ``WSL_*`` names, and the destination's ``env`` names
  with the router's own values. It is read through ``Gateway.environ``, never ``os.environ``;
  notification text is never an input of :func:`build_env`. The router never reads ``cron.env_file``
  (the cron lines source it), so no value of it exists here to log or echo.
- Text travels only in the JSON on stdin, written with ``ensure_ascii`` so that NUL, newlines
  and every other code point round-trip exactly and stay on one ASCII line. An empty message and
  a lone surrogate in any text field cannot be sent (the siblings refuse both): the call is
  ``not_started``.
- A result carries the :class:`~notice_route.gateway.RunResult` of a call that ran (its
  ``stderr`` emptied and its ``error`` None), plus fixed words and the trusted variable names. No
  exception text, no notifier stderr and no environment value is kept.

Choices where the interface is silent (the narrower reading):

- A named variable whose value holds NUL counts as unset (the value cannot be passed).
- ``WSL_INTEROP`` and ``WSL_DISTRO_NAME`` pass only when non-empty and without NUL.
- ``LANG`` and ``LC_ALL`` are judged one by one; a UTF-8 locale is
  ``[A-Za-z0-9_]{1,32}.utf8|utf-8`` (any case) with an optional ``@modifier``.
- ``PATH`` is the router's own with the entries that fail ``Gateway.clean_path`` removed
  (a missing, relative, non-directory, foreign-owned or group/world-writable entry; only the
  entry itself is judged, never its ancestors).
- A named variable unset at call time ends ``env_missing`` before anything is started, even
  though the decision already checked it (the environment is read again here).
- Order of refusals: bad inputs (``not_started``), unset variables (``env_missing``), the
  ``cmd[0]`` check (``exec_failed``), then the spawn.
- A spawn failure (``spawn_failed``) is ``exec_failed``: the command cannot be started.

Classification of a :class:`CallResult` lives in :mod:`notice_route.outcome` (#20).
The send lock (:func:`notice_route.sendlock.send_lock`, #21) is taken by the caller around
:func:`call_notifier`; the call itself does not lock.
TODO(#22): the send pipeline builds the arguments from the ``sending`` row of ``decide``.
TODO(#22): any exception that escapes :func:`call_notifier` (it raises only for faults outside the
child's own fate, after the gateway has killed the group) is ``unknown``: the pipeline keeps the
row claimed and never retries it as ``not_started``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from .gateway import ChildLostError, Gateway, RunResult, SigchldIgnoredError
from .validate import LEVELS, Destination

__all__ = [
    "ACTOR",
    "DEADLINE_EXTRA",
    "DEFAULT_PATH",
    "FIXED_NAMES",
    "CallResult",
    "CallStatus",
    "EnvBuild",
    "build_argv",
    "build_env",
    "call_deadline",
    "call_notifier",
    "notification_json",
]

ACTOR = "auto:notice-route"
DEADLINE_EXTRA = 10.0  # seconds past the destination's ``timeout`` to the kill deadline
DEFAULT_PATH = "/usr/bin:/bin"
FIXED_NAMES = ("PATH", "LANG", "LC_ALL", "WSL_INTEROP", "WSL_DISTRO_NAME")
_WSL_NAMES = ("WSL_INTEROP", "WSL_DISTRO_NAME")
_DEFAULT_LOCALE = "C.UTF-8"
_FIXED_FLAGS = ("send", "--stdin-json", "--json", "--actor", ACTOR)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_UTF8_LOCALE = re.compile(r"[A-Za-z0-9_]{1,32}\.[Uu][Tt][Ff]-?8(?:@[A-Za-z0-9]{1,32})?")
_HEX = re.compile(r"[0-9a-f]{64}")  # a SHA-256 digest

CallStatus = Literal["ran", "env_missing", "exec_failed", "not_started", "lost"]


@dataclass(frozen=True)
class EnvBuild:
    """``env`` is the child's whole environment; ``missing`` the named variables that are unset
    or empty in the router's environment (in the destination's order; none is in ``env``)."""

    env: dict[str, str]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class CallResult:
    """What one call came to.

    ``ran``: the child started and ``run`` says how it ended (status ``exited``, ``signaled``,
    ``timeout`` or ``overflow``; its ``error`` is always None and its ``stderr`` always empty:
    the notifier's stderr is never stored).
    ``env_missing``: nothing was started; ``missing`` lists the unset names.
    ``exec_failed``: nothing ran; ``cmd[0]`` failed the check or could not be started.
    ``not_started``: the call was refused before anything started (a bad argument, or SIGCHLD
    ignored); an internal fault, never the notifier's.
    ``lost``: the child started and its fate is unknown (it could not be reaped, or another
    reaper took it: :class:`~notice_route.gateway.ChildLostError`); the group was killed; treat
    as ``unknown``.
    """

    status: CallStatus
    run: RunResult | None = None
    missing: tuple[str, ...] = ()


def notification_json(*, title: str, message: str, level: str, link: str) -> str:
    """The notification as one line of ASCII JSON holding only the present fields.

    An empty ``title`` or ``link`` is absent (never null); ``message`` is required and
    non-empty. ``level`` is always present and one of the four levels. Raises ``ValueError``
    for a non-string, an empty message, a bad level or a lone surrogate in a text field."""
    if not all(isinstance(v, str) for v in (title, message, level, link)):
        raise ValueError("notification fields must be strings")
    if level not in LEVELS:
        raise ValueError("level must be info, warn, action or error")
    if not message:
        raise ValueError("message must not be empty")
    for value in (title, message, link):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("text must not hold a lone surrogate") from None
    body: dict[str, str] = {}
    if title:
        body["title"] = title
    body["message"] = message
    body["level"] = level
    if link:
        body["link"] = link
    return json.dumps(body, ensure_ascii=True, separators=(",", ":"))


def build_argv(executable: str, cmd: Sequence[str], dedupe_key: str | None) -> list[str]:
    """``executable`` (the resolved ``cmd[0]``), ``cmd[1:]``, the fixed flags and, when
    ``dedupe_key`` is given, ``--dedupe-key=<hex>``. Takes no notification text."""
    if not cmd:
        raise ValueError("cmd must not be empty")
    argv = [executable, *cmd[1:], *_FIXED_FLAGS]
    if dedupe_key is not None:
        if not isinstance(dedupe_key, str) or _HEX.fullmatch(dedupe_key) is None:
            raise ValueError("the dedupe key must be lowercase hex")
        argv.append(f"--dedupe-key={dedupe_key}")
    return argv


def _locale(value: str | None) -> str:
    if value is not None and _UTF8_LOCALE.fullmatch(value):
        return value
    return _DEFAULT_LOCALE


def _passable(value: str | None) -> str | None:
    """``value`` when it is set, non-empty and holds no NUL, else None."""
    if value is None or value == "" or "\x00" in value:
        return None
    return value


def build_env(gateway: Gateway, destination: Destination) -> EnvBuild:
    """The scrubbed environment of one call. Raises ``ValueError`` for an ``env`` name outside
    ``[A-Za-z_][A-Za-z0-9_]{0,127}`` or in the fixed set (config validation forbids both)."""
    for name in destination.env:
        if _NAME.fullmatch(name) is None or name in FIXED_NAMES:
            raise ValueError("a destination env name is outside the grammar or fixed")
    source = gateway.environ()
    path = gateway.clean_path(source.get("PATH", ""))
    env: dict[str, str] = {
        "PATH": path or DEFAULT_PATH,
        "LANG": _locale(source.get("LANG")),
        "LC_ALL": _locale(source.get("LC_ALL")),
    }
    if destination.kind == "wsl-notice":
        for name in _WSL_NAMES:
            value = _passable(source.get(name))
            if value is not None:
                env[name] = value
    missing: list[str] = []
    for name in destination.env:
        value = _passable(source.get(name))
        if value is None:
            missing.append(name)
        else:
            env[name] = value
    return EnvBuild(env, tuple(missing))


def call_deadline(destination: Destination) -> float:
    """The kill deadline in seconds from the process start: ``timeout`` plus 10."""
    return destination.timeout + DEADLINE_EXTRA


def call_notifier(
    gateway: Gateway,
    destination: Destination,
    *,
    title: str,
    message: str,
    level: str,
    link: str,
    dedupe_key: str | None,
) -> CallResult:
    """Make the one call to ``destination`` for an already rendered notification.

    It decides nothing and counts nothing: the caller took the decision, the claim and the
    hourly count. It never raises for a refusal, a failed spawn, a ``SigchldIgnoredError``
    (``not_started``) or a ``ChildLostError`` (``lost``). Any other exception (a clock failure,
    a ``RecursionError``, an interrupt) propagates, after the gateway has killed the group; the
    caller treats it as ``unknown`` and keeps the row claimed."""
    try:
        stdin = notification_json(title=title, message=message, level=level, link=link)
        # Validate the key before anything is resolved or read.
        build_argv("/", ("/",), dedupe_key)
        built = build_env(gateway, destination)
    except ValueError:
        return CallResult("not_started")
    if built.missing:
        return CallResult("env_missing", missing=built.missing)
    executable = gateway.resolve_exec(destination.cmd[0]) if destination.cmd else None
    if executable is None:
        return CallResult("exec_failed")
    try:
        run = gateway.run(
            build_argv(executable, destination.cmd, dedupe_key),
            env=built.env,
            timeout=call_deadline(destination),
            stdin=stdin,
        )
    except (ValueError, SigchldIgnoredError):  # nothing was started
        return CallResult("not_started")
    except ChildLostError:  # the child ran and its fate is unknown
        return CallResult("lost")
    if run.status == "spawn_failed":
        return CallResult("exec_failed")
    return CallResult("ran", run=replace(run, error=None, stderr=b""))
