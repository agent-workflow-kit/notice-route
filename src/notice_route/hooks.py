"""The ``post-fail`` hook (#27): docs/interface.md, Hooks.

A hook entry (``hooks.post-fail`` in the config) runs as ``sh -c <cmd>`` through
:meth:`notice_route.gateway.Gateway.run`: its own session and process group, only stdin, stdout
and stderr open, working directory ``/``, the scrubbed environment below, the payload on stdin,
and the group killed at the entry's timeout (default 30 s); stdout and stderr are read through the
bounded reader (more than its cap kills the group) and then dropped, never logged or stored.

The environment is ``PATH``, ``LANG`` and ``LC_ALL`` as for a notifier call plus
``NOTICE_ROUTE_HOOK=post-fail``, ``NOTICE_ROUTE_ID`` (the destination name) and
``NOTICE_ROUTE_OBJECT_TYPE=destination``, and nothing else. The payload never reaches argv or the
environment (the destination name is a validated config key, not payload text).
``NOTICE_ROUTE_HOOK`` makes ``send`` and ``flush`` refuse ``hook_recursion`` in the hook and
everything it starts.

A hook is best effort: what happens to it never changes a result. :func:`run_hook` returns a fixed
word for a failure (never the hook's output or an exception's text).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .gateway import ChildLostError, Gateway, SigchldIgnoredError
from .notify import base_env
from .validate import Hook

__all__ = ["HOOK_SHELL", "hook_argv", "hook_env", "hook_stdin", "run_hook", "start_detached"]

HOOK_SHELL = "/bin/sh"
HOOK_NAME = "post-fail"


def hook_env(gateway: Gateway, destination: str) -> dict[str, str]:
    """The whole environment of a hook."""
    env = base_env(gateway)
    env["NOTICE_ROUTE_HOOK"] = HOOK_NAME
    env["NOTICE_ROUTE_ID"] = destination
    env["NOTICE_ROUTE_OBJECT_TYPE"] = "destination"
    return env


def hook_argv(hook: Hook) -> list[str]:
    return [HOOK_SHELL, "-c", hook.cmd]


def hook_stdin(payload: Mapping[str, Any]) -> str:
    """The payload as one line of ASCII JSON."""
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"


def run_hook(gateway: Gateway, hook: Hook, payload: Mapping[str, Any]) -> str | None:
    """Run one hook and wait for it. ``None`` when it exited 0, else a fixed word for the
    failure: ``nonzero_exit``, ``signaled``, ``timeout``, ``overflow``, ``spawn_failed`` or
    ``lost``."""
    destination = str(payload["object_id"])
    try:
        result = gateway.run(
            hook_argv(hook),
            env=hook_env(gateway, destination),
            timeout=hook.timeout,
            stdin=hook_stdin(payload),
        )
    except ChildLostError:
        return "lost"
    except (ValueError, SigchldIgnoredError):
        return "spawn_failed"
    if result.status == "exited":
        return None if result.returncode == 0 else "nonzero_exit"
    return result.status


def start_detached(gateway: Gateway, hook: Hook, payload: Mapping[str, Any]) -> str | None:
    """Start an ``async`` hook detached (:meth:`Gateway.run_detached`): nobody waits for it, so
    nothing is known of its end. ``None`` when it was started, else ``spawn_failed``."""
    try:
        started = gateway.run_detached(
            hook_argv(hook),
            env=hook_env(gateway, str(payload["object_id"])),
            timeout=hook.timeout,
            stdin=hook_stdin(payload),
        )
    except ValueError:
        return "spawn_failed"
    return None if started else "spawn_failed"
