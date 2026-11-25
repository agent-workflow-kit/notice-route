"""A rig for the end-to-end tests of ``send`` (#22): a private home, fake notifiers as
destinations, a real state directory, real child processes, and a clock whose wall time is fixed
(the decision) while its monotonic time is real (the deadlines), optionally sped up."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from notice_route.cli import main
from notice_route.gateway import Gateway
from tests.fakes import Notifier, scrubbed_env
from tests.test_cli_rules_validate import schema_errors, write_config

NOW = 1_700_000_000  # 2023-11-14 22:13:20 UTC: inside the window 22:00 to 06:00
MORNING = 1_700_028_000  # 06:00 the next day
CANARY = "canary-value-7f3a91"

SCRIPT = (
    "import sys\n"
    "from notice_route.cli import main\n"
    "from notice_route.gateway import Gateway\n"
    "home, tmp = sys.argv[1], sys.argv[2]\n"
    "sys.exit(main(sys.argv[3:], Gateway(passwd_home=lambda: home, exec_check_from=tmp)))\n"
)


class RigClock:
    """Wall time fixed at ``wall`` (settable); monotonic and sleep are real, scaled by ``rate``."""

    def __init__(self, wall: float = NOW, rate: float = 1.0) -> None:
        self._wall = float(wall)
        self.rate = rate
        self._t0 = time.monotonic()
        self.wall_reads = 0

    def wall(self) -> float:
        self.wall_reads += 1
        return self._wall

    def jump_wall(self, seconds: float) -> None:
        self._wall += seconds

    def monotonic(self) -> float:
        return 1000.0 + (time.monotonic() - self._t0) * self.rate

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds / self.rate)


class Rig:
    def __init__(
        self,
        home: Path,
        tmp_path: Path,
        make_notifier: Callable[..., Notifier],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self.home, self.tmp_path, self.capsys = home, tmp_path, capsys
        self._make = make_notifier
        self.clock = RigClock()
        self.fds: list[int] = []
        self.procs: list[subprocess.Popen[bytes]] = []
        self.state = home / ".local" / "state" / "notice-route"

    # configuration -------------------------------------------------------------------------

    def notifier(self, name: str = "slack-notice", **behavior: Any) -> Notifier:
        made = self._make(name, **behavior)
        made.stub.parent.chmod(0o700)  # the exec check refuses a group-writable directory
        return made

    def configure(
        self,
        destinations: dict[str, dict[str, Any]],
        rules: list[dict[str, Any]] | None = None,
        **top: Any,
    ) -> None:
        """``destinations`` maps a name to its keys; ``notifier`` (a :class:`Notifier`) fills in
        ``cmd``. The default rule is a pass-through for every notice to every destination."""
        built: dict[str, Any] = {}
        for name, keys in destinations.items():
            keys = dict(keys)
            n = keys.pop("notifier", None)
            if n is not None:
                keys["cmd"] = n.cmd
            keys.setdefault("kind", "slack-notice")
            built[name] = keys
        config: dict[str, Any] = {
            "quiet_hours": {"night": {"timezone": "UTC", "start": "22:00", "end": "06:00"}},
            "destinations": built,
            "rules": rules
            if rules is not None
            else [{"name": "pt", "match": {"level_min": "info"}, "notify": list(built)}],
        }
        config.update(top)
        write_config(self.home, config)

    # running -------------------------------------------------------------------------------

    def gateway(
        self,
        stdin: bytes | None = None,
        env: dict[str, str] | None = None,
        clock: Any = None,
    ) -> Gateway:
        read, write = os.pipe()
        self.fds.append(read)
        if stdin is not None:
            os.write(write, stdin)
        os.close(write)
        return Gateway(
            passwd_home=lambda: str(self.home),
            clock=clock or self.clock,
            exec_check_from=str(self.tmp_path),
            stdin_fd=read,
            environ=lambda: dict(env or {}),
        )

    def run(
        self,
        *argv: str,
        stdin: bytes | None = None,
        env: dict[str, str] | None = None,
        gateway: Gateway | None = None,
    ) -> tuple[int, str, str]:
        gw = gateway or self.gateway(stdin, env)
        code = main(list(argv), gw)
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err

    def send(
        self,
        message: str = "hello",
        *,
        level: str | None = None,
        source: str | None = "s",
        extra: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        gateway: Gateway | None = None,
    ) -> tuple[int, dict[str, Any], str]:
        """``send --stdin-json --json`` of a channel notice; returns the code, the document and
        stderr. The document is validated against the schema."""
        body: dict[str, Any] = {"message": message}
        if level:
            body["level"] = level
        argv = ["send", "--stdin-json", "--json", *extra]
        if source:
            argv += ["--source", source]
        code, out, err = self.run(*argv, stdin=json.dumps(body).encode(), env=env, gateway=gateway)
        assert out.endswith("\n") and out.count("\n") == 1, out
        doc = json.loads(out)
        if "error" in doc:
            assert schema_errors(doc, "error.json") == []
        else:
            assert schema_errors(doc, "send.json") == [], doc
        return code, doc, err

    def spawn(
        self,
        *argv: str,
        stdin: bytes | None = b"",
        env: dict[str, str] | None = None,
        stdout: int = subprocess.PIPE,
    ) -> subprocess.Popen[bytes]:
        """The real ``main`` in a child process with the private home (a pipe on stdin that the
        caller closes or writes)."""
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", SCRIPT, str(self.home), str(self.tmp_path), *argv],
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=subprocess.PIPE,
            env=scrubbed_env(self.home, env),
            cwd="/",
        )
        self.procs.append(proc)
        if stdin is not None:
            assert proc.stdin is not None
            proc.stdin.write(stdin)
            proc.stdin.close()
            proc.stdin = None  # communicate() would flush a closed file
        return proc

    def finish(self) -> None:
        for fd in self.fds:
            os.close(fd)
        for proc in self.procs:
            if proc.poll() is None:  # only processes this rig started, by handle
                proc.kill()
            proc.communicate()

    # inspecting ----------------------------------------------------------------------------

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        conn = sqlite3.connect(f"file:{self.state / 'state.db'}?mode=ro", uri=True)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def log_lines(self) -> list[dict[str, Any]]:
        path = self.state / "log.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def state_text(self) -> str:
        """Everything the router wrote that a person can read: the log and the database file."""
        out = ""
        for p in sorted(self.state.glob("log*")) + sorted(self.state.glob("state.db*")):
            out += p.read_bytes().decode("latin-1")
        return out


_ROOT = Path(__file__).resolve().parent.parent


def log_errors(instance: Any) -> list[str]:
    """Validate a log record against schemas/log.json."""
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    registry: Registry[Any] = Registry()
    for path in (_ROOT / "schemas").rglob("*.json"):
        doc = json.loads(path.read_text())
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
    schema = json.loads((_ROOT / "schemas" / "log.json").read_text())
    return [
        e.message for e in Draft202012Validator(schema, registry=registry).iter_errors(instance)
    ]
