"""Helpers of the live suite: opt-in, prerequisites, the tool and router runners, the report.

Rules this module keeps (checked offline by ``tests/test_live_harness.py``):

- Nothing here starts a process, opens a socket or reads a file at import time.
- A credential value is read from the environment only, handed to a child only through the
  variable the notifier's own config names, and never put in argv, a file, the report or an
  assertion message. Everything written to the report passes ``Protected.redact`` (each string
  value, before it is encoded or sliced); assertion messages carry names and fixed words only,
  never an output of a notifier or of the router: a test computes ``leaked = ...leaks(...)`` (or
  ``found_in``) first and asserts ``not leaked`` with a fixed text.
- A test whose prerequisite is missing is skipped with a reason that names variables and
  binaries, never values.
- The suite never runs ``crontab`` in any form. The autouse guard of ``tests/conftest.py`` stays
  on for every live test; there is no bypass.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLANNED_MESSAGES = 8  # one send, one deferred-then-flushed per notifier, two signal checks
WORST_CASE_MESSAGES = 10  # plus a stale-interop toast and a duplicate after SIGTERM
OPT_IN_VAR = "NOTICE_ROUTE_LIVE"
REPORT_DIR_VAR = "NOTICE_ROUTE_LIVE_REPORT_DIR"
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
RESERVED_NAMES = frozenset({"HOME", "PATH", "LANG", "TMPDIR"})
RESERVED_PREFIXES = ("LC_", "WSL_", "NOTICE_ROUTE_", "XDG_")


def reserved_name(name: str) -> bool:
    """A variable the suite or the notifiers use for their own purposes cannot hold a credential."""
    return name in RESERVED_NAMES or name.startswith(RESERVED_PREFIXES)


TOOL_TIMEOUT_S = 90.0
ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class ToolSpec:
    """One real notifier: its binary name, the variables that designate it, its probe grammar."""

    name: str
    prefix: str  # NOTICE_ROUTE_LIVE_<prefix>_BIN, _CONFIG, _CRED_ENV
    kind: str  # the router's destination kind
    uses_credential: bool
    probe_args: tuple[str, ...]  # the router's validate call, after the command
    pinned: str | None  # the pin under tests/sibling-schemas, when there is one

    @property
    def bin_var(self) -> str:
        return f"NOTICE_ROUTE_LIVE_{self.prefix}_BIN"

    @property
    def config_var(self) -> str:
        return f"NOTICE_ROUTE_LIVE_{self.prefix}_CONFIG"

    @property
    def cred_var(self) -> str:
        return f"NOTICE_ROUTE_LIVE_{self.prefix}_CRED_ENV"


TOOLS: dict[str, ToolSpec] = {
    "slack-notice": ToolSpec(
        "slack-notice", "SLACK", "slack-notice", True, ("validate", "--json"), "slack-notice"
    ),
    "ha-notice": ToolSpec(
        "ha-notice", "HA", "ha-notice", True, ("validate", "--json", "--probe"), "ha-notice"
    ),
    "wsl-notice": ToolSpec(
        "wsl-notice", "WSL", "wsl-notice", False, ("validate", "--json", "--probe"), None
    ),
}
# Every variable the suite reads, for the docs and the plan (names only, never values).
ENV_VARS: tuple[str, ...] = (
    OPT_IN_VAR,
    REPORT_DIR_VAR,
    *(
        v
        for s in TOOLS.values()
        for v in (s.bin_var, s.config_var, *([s.cred_var] if s.uses_credential else []))
    ),
)


def opted_in(environ: Mapping[str, str]) -> bool:
    """The one opt-in: ``NOTICE_ROUTE_LIVE=1`` exactly."""
    return environ.get(OPT_IN_VAR) == "1"


@dataclass(frozen=True)
class ToolConfig:
    """A real notifier the person designated. Holds no credential value."""

    spec: ToolSpec
    binary: str  # absolute path
    config: str | None  # absolute path of the notifier's own config, or None (its default)
    cred_env: str | None  # the NAME of the variable that holds the credential

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def cmd(self) -> list[str]:
        """What a destination's ``cmd`` is: the binary and its own ``--config`` (no value of a
        credential, never ``send``)."""
        return [self.binary, *(["--config", self.config] if self.config else [])]


def tool_config(
    name: str,
    environ: Mapping[str, str],
    *,
    need_credential: bool,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[ToolConfig | None, str]:
    """``(config, "")`` or ``(None, reason)``. The reason names binaries and variables only."""
    spec = TOOLS[name]
    override = environ.get(spec.bin_var)
    found = override if override else which(spec.name)
    if not found:
        return None, f"{spec.name} is not on PATH (or set {spec.bin_var} to its absolute path)"
    binary = os.path.realpath(found) if os.path.isabs(found) else None
    if binary is None:
        return None, f"{spec.bin_var} must be an absolute path"
    if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
        return None, f"{spec.name} at the designated path is not an executable file"
    config = environ.get(spec.config_var) or None
    if config is not None and not (os.path.isabs(config) and os.path.isfile(config)):
        return None, f"{spec.config_var} must be the absolute path of an existing file"
    cred_env: str | None = None
    if spec.uses_credential and need_credential:
        cred_env = environ.get(spec.cred_var)
        if not cred_env:
            return None, f"set {spec.cred_var} to the NAME of the variable holding the credential"
        if not NAME_RE.fullmatch(cred_env):
            return None, f"{spec.cred_var} must be a variable name ([A-Za-z_][A-Za-z0-9_]*)"
        if reserved_name(cred_env):
            return None, (
                f"{spec.cred_var} names a reserved variable (HOME, PATH, LANG, TMPDIR, LC_*, "
                "WSL_*, NOTICE_ROUTE_*, XDG_*); use a variable of your own"
            )
        if not environ.get(cred_env, "").strip():
            return None, f"the variable named by {spec.cred_var} is unset or empty"
    return ToolConfig(spec, binary, config, cred_env), ""


def wsl_ready(environ: Mapping[str, str]) -> str:
    """ "" when the environment looks like WSL with interop, else the reason it does not."""
    for var in ("WSL_DISTRO_NAME", "WSL_INTEROP"):
        if not environ.get(var):
            return f"{var} is not set (not a WSL session with interop)"
    return ""


# --- what is protected, the report ---------------------------------------------------------


class Protected:
    """The values that must never be shown (credential values). ``redact`` replaces them, also
    in their JSON-escaped forms (a value with a quote, a backslash or a non-ASCII character is
    written differently inside a JSON output)."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def add(self, value: str) -> None:
        if len(value.strip()) < 4:
            return
        for v in (value, value.strip()):
            self._values.add(v)
            self._values.add(json.dumps(v)[1:-1])
            self._values.add(json.dumps(v, ensure_ascii=False)[1:-1])

    def redact(self, text: str) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, "<redacted>")
        return text

    def redact_data(self, data: Any) -> Any:
        """A copy of ``data`` with every string (and key) redacted; other objects by ``repr``."""
        if isinstance(data, str):
            return self.redact(data)
        if isinstance(data, bool) or data is None or isinstance(data, (int, float)):
            return data
        if isinstance(data, Mapping):
            return {self.redact(str(k)): self.redact_data(v) for k, v in data.items()}
        if isinstance(data, (list, tuple, set, frozenset)):
            return [self.redact_data(v) for v in data]
        return self.redact(repr(data))

    def clip(self, text: str, limit: int) -> str:
        """Redact first, then cut: a value that a cut would split is still gone."""
        return self.redact(text)[:limit]

    def leaks(self, *texts: str | bytes) -> bool:
        for t in texts:
            raw = t if isinstance(t, bytes) else t.encode("utf-8", "replace")
            if any(v.encode("utf-8") in raw for v in self._values):
                return True
        return False


def found_in(needle: str, *texts: str | bytes) -> bool:
    """Whether ``needle`` is in one of ``texts``. A test calls it OUTSIDE its ``assert`` and asserts
    the result with a fixed message: a helper that raised would show its arguments (the texts) in
    a long traceback."""
    raw = needle.encode("utf-8")
    return any(raw in (t if isinstance(t, bytes) else t.encode("utf-8", "replace")) for t in texts)


@dataclass
class Report:
    """Observations of a run, written as JSON to a private directory for the person to compare
    with ``tests/fakebin/FIDELITY.md``. Every string value is redacted before it is encoded."""

    protected: Protected = field(default_factory=Protected)
    run_id: str = field(default_factory=lambda: secrets.token_hex(4))
    observations: dict[str, Any] = field(default_factory=dict)
    directory: Path | None = None

    def clip(self, text: str, limit: int) -> str:
        return self.protected.clip(text, limit)

    def record(self, key: str, **data: Any) -> None:
        self.observations[key] = self.protected.redact_data(data)

    def write(self, environ: Mapping[str, str]) -> Path | None:
        if not self.observations:
            return None
        chosen = environ.get(REPORT_DIR_VAR)
        if chosen and os.path.isabs(chosen):
            directory = private_directory(Path(chosen))
        else:
            directory = Path(tempfile.mkdtemp(prefix="notice-route-live-"))  # mode 0700
        self.directory = directory
        path = directory / f"live-report-{self.run_id}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"run_id": self.run_id, "observations": self.observations},
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
        return path


def private_directory(path: Path) -> Path:
    """Create ``path`` (parents as needed) and make the leaf mode 0700 whatever the umask. An
    existing leaf is fixed to 0700 when it is ours; a symlink or another user's directory is
    refused."""
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"{REPORT_DIR_VAR} is not a plain directory")
    if info.st_uid != os.getuid():
        raise RuntimeError(f"{REPORT_DIR_VAR} is owned by another user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)
    return path


def schema_errors(instance: Any, name: str) -> list[str]:
    """Errors of ``instance`` against ``schemas/cli/<name>`` of this repository (offline)."""
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    registry: Registry[Any] = Registry()
    for path in (ROOT / "schemas").rglob("*.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
    schema = json.loads((ROOT / "schemas" / "cli" / name).read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, registry=registry)
    return [e.message for e in validator.iter_errors(instance)]


def title_for(run_id: str) -> str:
    return f"notice-route live test {run_id}"


def message_for(run_id: str, what: str) -> str:
    return (
        f"TEST MESSAGE from the notice-route live suite (run {run_id}, {what}). "
        "Sent on purpose; safe to ignore."
    )


# --- running processes ---------------------------------------------------------------------


@dataclass
class Result:
    code: int
    out: str
    err: str

    def json(self) -> Any:
        return json.loads(self.out)


def tool_env(cfg: ToolConfig, environ: Mapping[str, str], *, credential: bool) -> dict[str, str]:
    """The environment of a direct notifier call: PATH, LANG, the credential (only when asked,
    only under the name the person designated) and WSL's two variables for wsl-notice."""
    env = {"PATH": environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    if credential and cfg.cred_env:
        env[cfg.cred_env] = environ[cfg.cred_env]
    if cfg.name == "wsl-notice":
        for var in ("WSL_INTEROP", "WSL_DISTRO_NAME"):
            if environ.get(var):
                env[var] = environ[var]
    return env


def communicate_bounded(
    proc: subprocess.Popen[bytes], stdin: bytes | None = None, timeout: float = 90.0,
    grace: float = 10.0,
) -> tuple[bytes, bytes, bool]:  # fmt: skip
    """``(out, err, timed_out)``. Never waits unboundedly: after ``timeout`` the process this
    handle started (and the group it created, while it is not yet reaped) is killed, the pipes are
    read for ``grace`` more seconds and then closed."""
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
        return out, err, False
    except subprocess.TimeoutExpired:
        pass
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the session this call created (not yet reaped)
        except ProcessLookupError:
            pass
        proc.kill()
    try:
        out, err = proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired:  # a grandchild outside the group holds the pipes open
        out, err = b"", b""
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    return out, err, True


def wait_group_gone(pgid: int, seconds: float = 20.0) -> bool:
    """Wait (bounded) until no process is left in the group this suite created (signal 0 sends
    nothing). True when it is gone."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def _run(
    argv: Sequence[str], stdin: bytes, env: Mapping[str, str], timeout: float, grace: float = 10.0
) -> Result:
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd="/",
        start_new_session=True,
    )
    out, err, timed_out = communicate_bounded(proc, stdin, timeout, grace)
    if timed_out:
        return Result(
            124, out.decode("utf-8", "replace"), "timeout\n" + err.decode("utf-8", "replace")
        )
    return Result(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


def run_tool(
    cfg: ToolConfig,
    args: Sequence[str],
    environ: Mapping[str, str],
    *,
    stdin: bytes = b"",
    credential: bool = False,
    before: Sequence[str] | None = None,
    timeout: float = TOOL_TIMEOUT_S,
) -> Result:
    """Run the real notifier directly: ``<binary> [before or --config C] <args>``."""
    head = [cfg.binary, *(cfg.cmd[1:] if before is None else before)]
    return _run([*head, *args], stdin, tool_env(cfg, environ, credential=credential), timeout)


DRY_SEND = ("send", "--dry-run", "--stdin-json", "--json", "--actor", "auto:notice-route")


# --- the router ---------------------------------------------------------------------------

ROUTER_SCRIPT = (
    "import sys\n"
    "from notice_route.cli import main\n"
    "from notice_route.gateway import Gateway, SystemClock\n"
    "home, floor, offset = sys.argv[1], sys.argv[2], float(sys.argv[3])\n"
    "class Clock(SystemClock):\n"
    "    def wall(self):\n"
    "        return super().wall() + offset\n"
    "gw = Gateway(passwd_home=lambda: home, exec_check_from=floor, clock=Clock())\n"
    "sys.exit(main(sys.argv[4:], gw))\n"
)


@dataclass
class LiveRouter:
    """The real ``notice-route`` in a child process with a private passwd home (its config and
    state live there, never in the user's), the real notifiers as destinations, and an
    environment that holds only PATH, LANG, HOME and the credential variables it was given."""

    home: Path
    floor: str
    environ: Mapping[str, str]
    pass_names: tuple[str, ...] = ()  # names of credential variables the child may see
    overrides: dict[str, str] = field(default_factory=dict)  # name -> value (e.g. a bad one)
    procs: list[subprocess.Popen[bytes]] = field(default_factory=list)

    @property
    def state(self) -> Path:
        return self.home / ".local" / "state" / "notice-route"

    def env(self) -> dict[str, str]:
        env = {
            "PATH": self.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "HOME": str(self.home),
        }
        for name in self.pass_names:
            if self.environ.get(name):
                env[name] = self.environ[name]
        for var in ("WSL_INTEROP", "WSL_DISTRO_NAME"):
            if self.environ.get(var):
                env[var] = self.environ[var]
        env.update(self.overrides)
        return env

    def argv(self, *args: str, offset: float = 0.0) -> list[str]:
        return [
            sys.executable,
            "-I",
            "-c",
            ROUTER_SCRIPT,
            str(self.home),
            self.floor,
            str(offset),
            *args,
        ]

    def write_config(self, config: dict[str, Any]) -> Path:
        directory = self.home / ".config" / "notice-route"
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        path = directory / "config.yaml"
        path.write_text(json.dumps(config))  # JSON is YAML
        path.chmod(0o600)
        return path

    def run(
        self, *args: str, stdin: bytes = b"", offset: float = 0.0, timeout: float = 150.0
    ) -> Result:
        return _run(self.argv(*args, offset=offset), stdin, self.env(), timeout)

    def start(self, *args: str, stdin: bytes = b"", offset: float = 0.0) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(
            self.argv(*args, offset=offset),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(),
            cwd="/",
            start_new_session=True,
        )
        self.procs.append(proc)
        assert proc.stdin is not None
        proc.stdin.write(stdin)
        proc.stdin.close()
        proc.stdin = None
        return proc

    def send(self, message: str, *, title: str, source: str = "live-test", offset: float = 0.0,
             level: str = "info") -> Result:  # fmt: skip
        body = {"title": title, "message": message, "level": level}
        return self.run(
            "send", "--stdin-json", "--json", "--source", source,
            stdin=json.dumps(body).encode(), offset=offset,
        )  # fmt: skip

    def finish(self) -> None:
        """Continue, then end only the processes this object started (by handle)."""
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGCONT)
                    proc.wait(timeout=20)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    proc.kill()
            communicate_bounded(proc, timeout=10.0, grace=5.0)


def window_around_now(now: float, before_h: int = 1, after_h: int = 2) -> tuple[str, str]:
    """``(start, end)`` HH:MM in UTC of a window that contains ``now``."""

    def hhmm(t: float) -> str:
        g = time.gmtime(t)
        return f"{g.tm_hour:02d}:{g.tm_min:02d}"

    return hhmm(now - before_h * 3600), hhmm(now + after_h * 3600)
