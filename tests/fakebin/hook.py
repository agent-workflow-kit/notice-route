"""A fake ``post-fail`` hook command (#27), standard library only.

``main(argv, behavior_path)`` reads the behavior JSON at every run, appends JSON lines to the
log named in it, and then acts: ``exit`` (exit code), ``sleep`` (seconds before the answer),
``ignore_term`` (ignore SIGTERM), ``stdout_bytes`` (that much output), ``grandchild`` (a
``setsid`` descendant that sleeps ``lifetime`` seconds, at most 30, and holds the pipes when
``hold`` is set), ``call_router`` (run the router's own ``send`` or ``flush`` entry in this
process with the hook's environment and record its exit code and stdout), ``touch`` (a file to
create when the run ends, for a detached hook nobody waits for).

Every sleep ends by itself within ``max_seconds`` (default 30) so a test that forgets a kill leaks
nothing for long.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import Any


def _log(path: str, record: dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as out:
        out.write(json.dumps(record) + "\n")


def _start(pid: int) -> str | None:
    try:
        text = open(f"/proc/{pid}/stat", encoding="utf-8").read()
    except OSError:
        return None
    return text.rsplit(")", 1)[1].split()[19]


def main(argv: list[str], behavior_path: str) -> int:
    with open(behavior_path, encoding="utf-8") as handle:
        b: dict[str, Any] = json.load(handle)
    log = b["log"]
    limit = float(b.get("max_seconds", 30))
    stdin = sys.stdin.read() if not b.get("no_read") else ""
    fds = sorted(int(x) for x in os.listdir("/proc/self/fd") if x.isdigit())
    _log(
        log,
        {
            "event": "run",
            "pid": os.getpid(),
            "start": _start(os.getpid()),
            "pgid": os.getpgrp(),
            "sid": os.getsid(0),
            "cwd": os.getcwd(),
            "argv": argv,
            "env": dict(os.environ),
            "stdin": stdin,
            "fds": fds,
        },
    )
    if b.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if b.get("grandchild"):
        spec = b["grandchild"]
        lifetime = min(float(spec.get("lifetime", 10)), 30.0)
        kw: dict[str, Any] = {"start_new_session": True}
        if not spec.get("hold"):
            kw.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", f"import time; time.sleep({lifetime})"],
            stdin=subprocess.DEVNULL,
            **kw,
        )
        _log(log, {"event": "grandchild", "pid": child.pid, "start": _start(child.pid)})
    if b.get("call_router"):
        from notice_route.cli import main as router_main

        code = router_main(list(b["call_router"]))
        _log(log, {"event": "router", "exit": code})
    if b.get("stdout_bytes"):
        sys.stdout.write("x" * int(b["stdout_bytes"]))
        sys.stdout.flush()
    if b.get("sleep"):
        deadline = time.monotonic() + min(float(b["sleep"]), limit)
        while time.monotonic() < deadline:
            time.sleep(0.02)
    if b.get("touch"):
        open(b["touch"], "w", encoding="utf-8").close()
    _log(log, {"event": "exit"})
    return int(b.get("exit", 0))
