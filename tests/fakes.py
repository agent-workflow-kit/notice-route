"""Build and drive the fake notifier of ``tests/fakebin/notifier.py``.

``build_notifier`` writes an executable stub for one destination: its first line is the absolute
test interpreter (so a scrubbed ``PATH`` cannot break it) and the path of its behavior file is
baked in (so a scrubbed environment cannot lose it). The destination's ``cmd`` is
``[str(notifier.stub)]``, optionally with extra arguments the fake accepts before the
subcommand. Every call is logged; the helper finds the processes a call started (the fake and
its grandchildren) by PID plus start time and signals only those.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FAKEBIN = Path(__file__).resolve().parent / "fakebin"
CANONICAL = ["send", "--stdin-json", "--json", "--actor", "auto:notice-route"]

_STUB = """#!{python} -I
import sys

sys.path.insert(0, {fakebin!r})
import notifier

sys.exit(notifier.main(sys.argv[1:], {behavior!r}))
"""


def proc_start(pid: int) -> str | None:
    """The kernel start time of ``pid`` (field 22 of ``/proc/<pid>/stat``); None when unknown."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text("utf-8")
    except OSError:
        return None
    return text.rsplit(")", 1)[1].split()[19]


def alive(pid: int, start: str | None) -> bool:
    """True while ``pid`` is the same process (same start time) and not a zombie."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text("utf-8")
    except OSError:
        return False
    fields = text.rsplit(")", 1)[1].split()
    if fields[0] == "Z":
        return False
    return start is None or fields[19] == start


@dataclass
class Tracked:
    pid: int
    start: str | None
    role: str


@dataclass
class Notifier:
    name: str
    stub: Path
    behavior_path: Path
    log_path: Path
    # processes the helpers started themselves (``start``/``run``), known even before any log line
    spawned: list[Tracked] = field(default_factory=list)

    @property
    def cmd(self) -> list[str]:
        return [str(self.stub)]

    def set(self, **behavior: Any) -> None:
        """Replace the behavior for later calls (a new file renamed over the old one)."""
        tmp = self.behavior_path.with_name(self.behavior_path.name + ".new")
        tmp.write_text(json.dumps({"log": str(self.log_path), **behavior}), "utf-8")
        tmp.replace(self.behavior_path)

    def records(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text("utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def events(self, event: str) -> list[dict[str, Any]]:
        return [r for r in self.records() if r["event"] == event]

    def wait_for(self, event: str, *, count: int = 1, timeout: float = 10.0) -> dict[str, Any]:
        """Poll (bounded) until ``count`` records of ``event`` exist; return the last."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.events(event)
            if len(found) >= count:
                return found[-1]
            time.sleep(0.02)
        raise AssertionError(f"no {event!r} record x{count} within {timeout}s: {self.records()}")

    def tracked(self) -> list[Tracked]:
        out: list[Tracked] = list(self.spawned)
        for r in self.records():
            if r["event"] == "invoke":
                out.append(Tracked(r["pid"], r["start"], "fake"))
            elif r["event"] in ("grandchild", "grandchild_started", "grandchild_spawn"):
                out.append(Tracked(r["pid"], r["start"], "grandchild"))
        return out

    def kill_tracked(self) -> None:
        """SIGKILL each process this notifier recorded or started, only if PID and start time match.

        A process that was never reaped keeps its PID, so a start-time match proves identity.
        """
        for t in self.tracked():
            if t.start is not None and alive(t.pid, t.start):
                try:
                    os.kill(t.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def build_notifier(directory: Path, name: str = "slack-notice", **behavior: Any) -> Notifier:
    """Create ``<directory>/<name>`` (the stub) and its behavior and log files' paths.

    ``name`` is the executable's basename; ``behavior["tool"]`` (default ``slack-notice``)
    selects which notifier's output shape the fake imitates.
    """
    python = sys.executable
    if any(c.isspace() for c in python):
        raise RuntimeError("the test interpreter path must not contain whitespace")
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / name
    behavior_path = directory / f"{name}.behavior.json"
    log_path = directory / f"{name}.log.jsonl"
    stub.write_text(
        _STUB.format(python=python, fakebin=str(FAKEBIN), behavior=str(behavior_path)), "utf-8"
    )
    stub.chmod(0o755)
    notifier = Notifier(name, stub, behavior_path, log_path)
    notifier.set(**behavior)
    return notifier


def scrubbed_env(home: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A private environment: HOME and XDG_CONFIG_HOME under ``home``, nothing inherited."""
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    env.update(extra or {})
    return env


def start(
    notifier: Notifier,
    home: Path,
    args: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """Start the fake like the router starts a notifier: own session, piped stdio, cwd ``/``."""
    proc = subprocess.Popen(
        [*notifier.cmd, *(CANONICAL if args is None else args)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd="/",
        env=dict(env) if env is not None else scrubbed_env(home),
        start_new_session=True,
    )
    notifier.spawned.append(Tracked(proc.pid, proc_start(proc.pid), "popen"))
    return proc


def feed(proc: subprocess.Popen[bytes], data: bytes) -> None:
    """Write ``data`` to the child's stdin and close it (so ``communicate`` will not flush it)."""
    assert proc.stdin is not None
    proc.stdin.write(data)
    proc.stdin.close()
    proc.stdin = None


def run(
    notifier: Notifier,
    home: Path,
    stdin: bytes | str = b'{"message": "hello"}',
    args: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[bytes]:
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    proc = start(notifier, home, args, env=env)
    try:
        out, err = proc.communicate(data, timeout=timeout)
    except BaseException:
        # Recorded processes first (a setsid grandchild is outside the group and may hold the
        # pipes), then the group this helper created (the leader is not reaped yet), then close
        # the pipes ourselves: nothing here may wait for a process that is still alive.
        notifier.kill_tracked()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def last_json(result: subprocess.CompletedProcess[bytes]) -> Any:
    return json.loads(result.stdout.decode("utf-8"))
