"""Fake channel notifier (slack-notice / ha-notice call shape) for notice-route's tests.

Standard library only. It is started through a stub written by ``tests/fakes.py`` that bakes in
the path of a JSON behavior file; the router's scrubbed environment therefore cannot lose it.
It is a test aid, not a re-implementation: see ``tests/fakebin/FIDELITY.md`` for what is modeled
and what is not. A broken setup is one ``fakebin: ...`` line on stderr and exit 70.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
import unicodedata
from typing import Any, NoReturn

VERSION = "0.0.0-fake"
MAX_STDIN = 1_048_576
CODES_EXIT = {"usage": 2, "config_invalid": 2, "internal": 1, "refused": 20}
ERROR_CODES = {
    "usage",
    "config_invalid",
    "internal",
    "refused",
    "host_rejected",
    "auth_required",
    "credential_invalid",
    "rate_limited",
    "upstream_error",
    "network_error",
    "outcome_unknown",
    "wrong_host_response",
    "unavailable",
}
SIGNALS = {
    "KILL": signal.SIGKILL,
    "TERM": signal.SIGTERM,
    "SEGV": signal.SIGSEGV,
    "ABRT": signal.SIGABRT,
    "HUP": signal.SIGHUP,
    "INT": signal.SIGINT,
}
HANDLED = ("TERM", "HUP", "INT")
KEYS = {
    "log", "tool", "exit", "error", "result", "stdout", "stdout_b64", "stderr", "stdout_bytes",
    "stderr_bytes", "no_output", "sleep", "linger", "hang", "hang_before_read", "max_seconds",
    "signals", "grandchild", "close_stdin", "die_by_signal", "validate_input", "disabled",
    "sequence",
}  # fmt: skip
GRANDCHILD_KEYS = {"setsid", "hold_stdout", "hold_stderr", "double_fork", "lifetime"}


class Setup(Exception):
    """The test setup is broken (not the program under test)."""


class Refuse(Exception):
    def __init__(self, reason: str, pointer: str | None = None) -> None:
        self.reason = reason
        self.pointer = pointer


class Usage(Exception):
    pass


class Unsupported(Exception):
    pass


class Ended(Exception):
    """The fake ends now with this exit code (a hang that hit its cap, a handled signal)."""

    def __init__(self, code: int) -> None:
        self.code = code


# behavior -------------------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_behavior(b: dict[str, Any], *, top: bool) -> None:
    unknown = set(b) - KEYS
    if unknown:
        raise Setup(f"unknown behavior key {sorted(unknown)[0]!r}")
    if not top and ({"log", "sequence"} & set(b)):
        raise Setup("a sequence entry may not set log or sequence")
    if top:
        log = b.get("log")
        if (
            not isinstance(log, str)
            or not os.path.isabs(log)
            or not os.path.isdir(os.path.dirname(log))
        ):
            raise Setup("log must be an absolute path in an existing directory")
    if b.get("tool", "slack-notice") not in ("slack-notice", "ha-notice"):
        raise Setup("tool must be slack-notice or ha-notice")
    if "exit" in b and not (
        isinstance(b["exit"], int) and not isinstance(b["exit"], bool) and 0 <= b["exit"] <= 255
    ):
        raise Setup("exit must be an integer 0..255")
    for key in ("sleep", "linger"):
        if key in b and not (_is_number(b[key]) and 0 <= b[key] <= 300):
            raise Setup(f"{key} must be a number 0..300")
    if "max_seconds" in b and not (_is_number(b["max_seconds"]) and 0 < b["max_seconds"] <= 300):
        raise Setup("max_seconds must be a number above 0 and at most 300")
    for key in ("hang", "hang_before_read", "close_stdin", "no_output", "disabled",
                "validate_input"):  # fmt: skip
        if key in b and not isinstance(b[key], bool):
            raise Setup(f"{key} must be a boolean")
    if b.get("hang_before_read") and not b.get("hang"):
        raise Setup("hang_before_read needs hang")
    for key in ("stdout_bytes", "stderr_bytes"):
        if key in b and not (
            isinstance(b[key], int) and not isinstance(b[key], bool) and 0 <= b[key] <= 50_000_000
        ):
            raise Setup(f"{key} must be an integer 0..50000000")
    for key in ("stdout", "stderr", "stdout_b64"):
        if key in b and not isinstance(b[key], str):
            raise Setup(f"{key} must be a string")
    if "stdout_b64" in b:
        try:
            base64.b64decode(b["stdout_b64"], validate=True)
        except ValueError:
            raise Setup("stdout_b64 is not base64") from None
    for key in ("error", "result"):
        if key in b and not isinstance(b[key], dict):
            raise Setup(f"{key} must be an object")
    if "error" in b and b["error"].get("code") not in ERROR_CODES:
        raise Setup("error.code must be one of the notifiers' codes")
    if "signals" in b:
        if not isinstance(b["signals"], dict):
            raise Setup("signals must be an object")
        for name, action in b["signals"].items():
            ok = name in HANDLED and isinstance(action, str)
            if ok and action not in ("ignore", "default"):
                ok = re.fullmatch(r"exit:[0-9]{1,3}", action) is not None and int(action[5:]) < 256
            if not ok:
                raise Setup(f"signals.{name}: use ignore, default or exit:N for TERM, HUP, INT")
    if "die_by_signal" in b and b["die_by_signal"] not in SIGNALS:
        raise Setup(f"die_by_signal must be one of {sorted(SIGNALS)}")
    if "grandchild" in b:
        g = b["grandchild"]
        if not isinstance(g, dict) or set(g) - GRANDCHILD_KEYS:
            raise Setup(f"grandchild keys are {sorted(GRANDCHILD_KEYS)}")
        for key in ("setsid", "hold_stdout", "hold_stderr", "double_fork"):
            if key in g and not isinstance(g[key], bool):
                raise Setup(f"grandchild.{key} must be a boolean")
        if "lifetime" in g and not (_is_number(g["lifetime"]) and 0 < g["lifetime"] <= 120):
            raise Setup("grandchild.lifetime must be a number above 0 and at most 120")
    if "sequence" in b:
        if not isinstance(b["sequence"], list) or not b["sequence"]:
            raise Setup("sequence must be a non-empty list")
        for entry in b["sequence"]:
            if not isinstance(entry, dict):
                raise Setup("sequence entries must be objects")
            check_behavior(entry, top=False)


def load_behavior(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            b = json.load(f)
    except (OSError, ValueError) as exc:
        raise Setup(f"cannot read the behavior file: {type(exc).__name__}") from None
    if not isinstance(b, dict):
        raise Setup("the behavior file must hold an object")
    check_behavior(b, top=True)
    return b


def next_call(log: str) -> int:
    fd = os.open(log + ".count", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 32)
        n = int(raw) if raw else 0
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(n + 1).encode())
        return n
    finally:
        os.close(fd)


# logging --------------------------------------------------------------------------------------


class Log:
    def __init__(self, path: str, n: int) -> None:
        self.path = path
        self.n = n

    def write(self, event: str, **fields: Any) -> None:
        line = json.dumps({"event": event, "n": self.n, **fields}, ensure_ascii=False) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


def proc_start(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def open_fds() -> list[int] | None:
    for base in ("/proc/self/fd", "/dev/fd"):
        try:
            names = os.listdir(base)
        except OSError:
            continue
        fds: list[int] = []
        for name in names:
            fd = int(name)
            try:
                os.fstat(fd)
            except OSError:
                continue  # the descriptor listdir itself used
            fds.append(fd)
        return sorted(fds)
    return None


# argv grammar ---------------------------------------------------------------------------------

BOOL_FLAGS = {
    "--json", "--dry-run", "--verbose", "--quiet", "--version", "--help", "--stdin-json",
    "--probe", "--test-message",
}  # fmt: skip
VALUE_FLAGS = {
    "--config", "--actor", "--file", "--title", "--message", "--level", "--link", "--dedupe-key",
    "--decision-id",
}  # fmt: skip
ACTOR = re.compile(r"(?:human|auto):[A-Za-z0-9._@/-]{1,64}")
SUBCOMMANDS = {"send", "clear", "validate"}


class Args:
    def __init__(self) -> None:
        self.flags: set[str] = set()
        self.values: dict[str, str] = {}
        self.command: str | None = None


def parse_argv(argv: list[str]) -> Args:
    a = Args()
    i = 0
    while i < len(argv):
        tok = argv[i]
        i += 1
        if tok == "--":
            raise Usage
        if tok == "-h":
            a.flags.add("--help")
            continue
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            if name in BOOL_FLAGS:
                if eq:
                    raise Usage
                a.flags.add(name)
                continue
            if name in VALUE_FLAGS:
                if not eq:
                    if i >= len(argv) or argv[i].startswith("-"):
                        raise Usage
                    value = argv[i]
                    i += 1
                if name in a.values and name != "--decision-id":
                    raise Usage
                a.values[name] = value
                continue
            raise Usage  # unknown, or an abbreviation: flags are exact
        if tok.startswith("-"):
            raise Usage  # no short option but -h
        if a.command is not None or tok not in SUBCOMMANDS:
            raise Usage
        a.command = tok
    if "--help" in a.flags or "--version" in a.flags:
        return a
    if a.command is None:
        raise Usage
    if "--verbose" in a.flags and "--quiet" in a.flags:
        raise Usage
    actor = a.values.get("--actor")
    if actor is not None and ACTOR.fullmatch(actor) is None:
        raise Usage
    key = a.values.get("--dedupe-key")
    if key is not None:
        try:
            key.encode("utf-8")
        except UnicodeEncodeError:
            raise Usage from None
        if not 1 <= len(key) <= 1024:
            raise Usage
    if a.command != "validate" and ({"--probe", "--test-message"} & a.flags):
        raise Usage
    if a.command == "send":
        text = {"--title", "--message", "--level", "--link"} & set(a.values)
        file = a.values.get("--file")
        if "--stdin-json" in a.flags and file is not None and file != "-":
            raise Usage
        if ("--stdin-json" in a.flags or file is not None) and text:
            raise Usage
        if text and "--message" not in a.values:
            raise Usage
        if ("--stdin-json" in a.flags or file == "-") and os.isatty(0):
            raise Usage
        if not ("--stdin-json" in a.flags or file is not None or text) and os.isatty(0):
            raise Usage
    return a


# payload --------------------------------------------------------------------------------------

HOST = r"(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[[0-9A-Fa-f:.]+\])"
TCHAR = r"(?:[A-Za-z0-9\-._~:/?@!$&'()*+,;=]|%[0-9A-Fa-f]{2})"
LINK = re.compile(
    rf"(?i:https?)://(?P<host>{HOST})(?::(?P<port>[0-9]{{1,5}}))?(?:[/?]{TCHAR}*)?(?:#{TCHAR}*)?"
)
REMOVED = re.compile("[­؜᠎​‎‏‪-‮⁠-⁯﻿￹-￻\U000e0000-\U000e007f\U000e0100-\U000e01efᅟᅠㅤﾠ⠀᠋-᠍᠏]")
COMMAND_FAMILY = re.compile(r"(?:command|kiosk|update|clear|remove|request)_[a-z0-9_]*")
LEVELS = ("info", "warn", "action", "error")
NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(v) for v in value), default=0)
    return 0


def strict_load(raw: bytes, *, tool: str) -> Any:
    if len(raw) > MAX_STDIN or raw.startswith(b"\xef\xbb\xbf"):
        raise Refuse("invalid_payload", "")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in items:
            if k in out:
                raise ValueError("duplicate key")
            out[k] = v
        return out

    def bad_constant(_: str) -> NoReturn:
        raise ValueError("NaN or Infinity")

    def no_number(_: str) -> NoReturn:
        raise ValueError("number")

    try:
        text = raw.decode("utf-8")
        # slack-notice refuses every number at parse time; ha-notice parses them and then fails
        # the type check of the field (pointer /<field>).
        extra: dict[str, Any] = {"parse_int": no_number} if tool == "slack-notice" else {}
        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=bad_constant,
            parse_float=no_number if tool == "slack-notice" else float,
            **extra,
        )
        if _depth(value) > 100:
            raise ValueError("too deep")
    except (ValueError, RecursionError, OverflowError):
        raise Refuse("invalid_payload", "") from None
    return value


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in text)


def _empty(text: str) -> bool:
    kept = [
        c
        for c in text
        if not REMOVED.fullmatch(c)
        and not (unicodedata.category(c) in ("Cc", "Co", "Cn") and c != "\n")
    ]
    return all(c.isspace() or unicodedata.category(c) in ("Mn", "Mc", "Me") for c in kept)


def check_link(link: str) -> None:
    if re.fullmatch(r"[\x21-\x7e]{1,1024}", link) is None:
        raise Refuse("link_invalid")
    scheme = re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", link)
    if scheme and scheme.group(0)[:-1].lower() not in ("http", "https"):
        raise Refuse("link_scheme")
    m = LINK.fullmatch(link)
    if m is None:
        raise Refuse("link_invalid")
    if m.group("port") is not None and not 1 <= int(m.group("port")) <= 65535:
        raise Refuse("link_invalid")
    host = m.group("host")
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            raise Refuse("link_invalid") from None


def check_payload(raw: bytes, *, tool: str) -> dict[str, Any]:
    value = strict_load(raw, tool=tool)
    if not isinstance(value, dict):
        raise Refuse("invalid_payload", "")
    known = {"title", "message", "level", "link"}
    if tool == "ha-notice" and "actions" in value:
        raise Unsupported("actions are not modeled")
    unknown = sorted(set(value) - known)
    if unknown:
        raise Refuse("invalid_payload", "/" + (unknown[0] if NAME.fullmatch(unknown[0]) else "?"))
    for field in ("title", "message"):
        if field in value:
            v = value[field]
            if not isinstance(v, str) or len(v) > 100_000 or _has_surrogate(v):
                raise Refuse("invalid_payload", f"/{field}")
    if "level" in value and value["level"] not in LEVELS:
        raise Refuse("invalid_payload", "/level")
    if "link" in value:
        if not isinstance(value["link"], str):
            raise Refuse("invalid_payload", "/link")
        check_link(value["link"])
    message = value.get("message")
    if message is None or _empty(message):
        raise Refuse("empty_message")
    if tool == "ha-notice":
        folded = unicodedata.normalize("NFKC", message).casefold().strip()
        if folded == "tts" or COMMAND_FAMILY.fullmatch(folded):
            raise Refuse("command_keyword")
    return value


# output ---------------------------------------------------------------------------------------


def dumps(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def write_fd(fd: int, data: bytes) -> None:
    """Write everything; a reader that went away is ignored, as the real tools ignore it."""
    view = memoryview(data)
    try:
        while view:
            view = view[os.write(fd, view) :]
    except OSError:
        pass


def write_padding(fd: int, size: int) -> None:
    chunk = b"a" * 65536
    left = size
    try:
        while left > 0:
            n = os.write(fd, chunk[: min(left, len(chunk))])
            left -= n
    except OSError:
        pass


# Per-code defaults of the flags and details, as the notifiers' interfaces fix them (the pinned
# error schema enforces the same table). A scripted error may override any key.
CODE_DEFAULTS: dict[str, dict[str, Any]] = {
    "config_invalid": {"details": {"key": "?"}},
    "internal": {"retryable": False, "unknown_outcome": False},
    "host_rejected": {
        "retryable": False, "unknown_outcome": False,
        "details": {"http_status": 400, "host_error": None},
    },
    "auth_required": {
        "retryable": False, "unknown_outcome": False, "details": {"http_status": 401},
    },
    "credential_invalid": {
        "retryable": False, "unknown_outcome": False, "details": {"env": "NOTICE_FAKE_SECRET"},
    },
    "rate_limited": {
        "retryable": True, "unknown_outcome": False, "retry_after_s": 4,
        "details": {"http_status": 429},
    },
    "upstream_error": {
        "retryable": False, "unknown_outcome": True, "details": {"http_status": 502},
    },
    "network_error": {
        "retryable": True, "unknown_outcome": False, "details": {"tls": False, "timeout": False},
    },
    "outcome_unknown": {"retryable": False, "unknown_outcome": True},
    "wrong_host_response": {
        "retryable": False, "unknown_outcome": False, "details": {"http_status": 404},
    },
    "unavailable": {
        "retryable": True, "unknown_outcome": False, "details": {"phase": "input"},
    },
}  # fmt: skip


def envelope(error: dict[str, Any], message: str) -> dict[str, Any]:
    base = {
        "code": None,
        "message": message,
        "reason": None,
        "details": None,
        "retryable": None,
        "retry_after_s": None,
        "unknown_outcome": None,
    }
    base.update(CODE_DEFAULTS.get(error["code"], {}))
    base.update(error)
    return {"error": base}


def success(tool: str, payload: dict[str, Any], key: str | None) -> dict[str, Any]:
    level = payload.get("level", "info")
    if tool == "slack-notice":
        body = {"body": "\n".join(x for x in (payload.get("title"), payload.get("message")) if x)}
    else:
        body = {k: payload[k] for k in ("title", "message") if k in payload}
    out: dict[str, Any] = {
        "outcome": "sent",
        "tool": tool,
        "level": level,
        "truncated": False,
        "bytes": len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        "duration_ms": 0,
        "http_status": 200,
    }
    if tool == "ha-notice":
        out["tag_set"] = key is not None
    out["dedupe_key"] = key
    if tool == "ha-notice":
        out["warnings"] = []
    return out


# the run --------------------------------------------------------------------------------------

GRANDCHILD = r"""
import os, sys, time
lifetime = float(sys.argv[1])
double, setsid = sys.argv[2] == "1", sys.argv[3] == "1"
report = int(sys.argv[4])
if double and os.fork():
    os._exit(0)
if setsid:
    os.setsid()
os.write(report, str(os.getpid()).encode())
os.close(report)
end = time.monotonic() + lifetime
while time.monotonic() < end:
    time.sleep(0.05)
"""


class Run:
    def __init__(self, b: dict[str, Any], log: Log) -> None:
        self.b = b
        self.log = log
        self.tool: str = b.get("tool", "slack-notice")
        self.max_seconds: float = b.get("max_seconds", 60.0)
        self.started = time.monotonic()
        self.ppid = os.getppid()  # a parent that is already PID 1 cannot be watched
        self.json_mode = False

    # time that can never outlive its test
    def pause(self, seconds: float, *, hang: bool = False) -> None:
        end = self.started + self.max_seconds
        if not hang:
            end = min(end, time.monotonic() + seconds)
        while time.monotonic() < end:
            if self.ppid != 1 and os.getppid() != self.ppid:
                raise Ended(124)
            time.sleep(0.02)
        if hang or seconds >= self.max_seconds:
            raise Ended(124)

    def install_signals(self) -> None:
        for name in HANDLED:
            action = self.b.get("signals", {}).get(name, "default")
            num = SIGNALS[name]
            if action == "default":
                signal.signal(num, signal.SIG_DFL)
            elif action == "ignore":
                signal.signal(num, lambda *_a, n=name: self.log.write("signal", name=n))
            else:
                code = int(action[5:])

                def handler(*_a: Any, n: str = name, c: int = code) -> None:
                    self.log.write("signal", name=n)
                    self.log.write("exit", code=c)
                    os._exit(c)

                signal.signal(num, handler)

    def read_stdin(self) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_STDIN:
            try:
                data = os.read(0, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
            size += len(data)
        raw = b"".join(chunks)
        fields: dict[str, Any] = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        if len(raw) <= 65536:
            fields["text"] = raw.decode("utf-8", errors="replace")
        self.log.write("stdin", **fields)
        return raw

    def spawn_grandchild(self) -> None:
        g = self.b["grandchild"]
        lifetime = float(g.get("lifetime", 20.0))
        hold_out, hold_err = g.get("hold_stdout", False), g.get("hold_stderr", False)
        double = g.get("double_fork", False)
        r, w = os.pipe()
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", GRANDCHILD, str(lifetime), "1" if double else "0",
             "1" if g.get("setsid", False) else "0", str(w)],
            stdin=subprocess.DEVNULL,
            stdout=1 if hold_out else subprocess.DEVNULL,
            stderr=2 if hold_err else subprocess.DEVNULL,
            pass_fds=(w,),
        )  # fmt: skip
        os.close(w)
        ready, _, _ = select.select([r], [], [], 10)
        raw = os.read(r, 32) if ready else b""
        os.close(r)
        proc.wait(timeout=10) if double else None
        if not raw:
            raise Setup("the grandchild did not report its PID")
        pid = int(raw)
        self.log.write(
            "grandchild", pid=pid, start=proc_start(pid), pgid=os.getpgid(pid), sid=os.getsid(pid),
            setsid=g.get("setsid", False), hold_stdout=hold_out, hold_stderr=hold_err,
            double_fork=double, lifetime=lifetime,
        )  # fmt: skip

    def emit_error(self, error: dict[str, Any], exit_code: int | None) -> int:
        code = error["code"]
        message = "invalid arguments" if code == "usage" else f"fake notifier: {code}"
        if self.json_mode:
            write_fd(1, dumps(envelope(error, message)))
        reason = error.get("reason")
        line = f"refused ({reason}): {message}" if code == "refused" else f"{code}: {message}"
        write_fd(2, (line + "\n").encode("utf-8"))
        if exit_code is not None:
            return exit_code
        return CODES_EXIT.get(code, 12)

    def go(self, argv: list[str]) -> int:
        b = self.b
        self.json_mode = any(t == "--json" or t.startswith("--json=") for t in argv)
        try:
            args = parse_argv(argv)
        except Usage:
            return self.emit_error({"code": "usage"}, None)
        if "--version" in args.flags:
            write_fd(1, f"{self.tool} {VERSION}\n".encode())
            return 0
        if "--help" in args.flags:
            write_fd(1, f"usage: {self.tool} [flags] send|clear|validate\n".encode())
            return 0
        if args.command != "send" or "--dry-run" in args.flags:
            return self.emit_error({"code": "fake_unsupported"}, 1)
        if "--stdin-json" not in args.flags and args.values.get("--file") != "-":
            if not ({"--title", "--message", "--level", "--link", "--file"} & set(args.values)):
                pass  # default source: stdin (not a terminal, checked by the grammar)
            else:
                return self.emit_error({"code": "fake_unsupported"}, 1)
        if b.get("disabled"):
            write_fd(1, dumps({"outcome": "disabled"})) if self.json_mode else write_fd(
                2, b"disabled in config\n"
            )
            return 22
        if b.get("hang_before_read"):
            self.log.write("hang_started", before_read=True)
            self.pause(0, hang=True)
        raw = b""
        if b.get("close_stdin"):
            os.close(0)
            self.log.write("stdin_closed")
        else:
            raw = self.read_stdin()
        payload: dict[str, Any] = {}
        if b.get("validate_input", True) and not b.get("close_stdin"):
            try:
                payload = check_payload(raw, tool=self.tool)
            except Refuse as r:
                error: dict[str, Any] = {"code": "refused", "reason": r.reason}
                if r.pointer is not None:
                    error["details"] = {"pointer": r.pointer}
                return self.emit_error(error, None)
            except Unsupported:
                return self.emit_error({"code": "fake_unsupported"}, 1)
        elif raw:
            try:
                value = json.loads(raw)
                payload = value if isinstance(value, dict) else {}
            except ValueError:
                payload = {}
        if "grandchild" in b:
            self.spawn_grandchild()
        if b.get("hang"):
            self.log.write("hang_started", before_read=False)
            self.pause(0, hang=True)
        if "sleep" in b:
            self.pause(float(b["sleep"]))
        code = self.answer(args, payload)
        if "linger" in b:
            self.pause(float(b["linger"]))
        if "die_by_signal" in b:
            num = SIGNALS[b["die_by_signal"]]
            if num != signal.SIGKILL:
                signal.signal(num, signal.SIG_DFL)
            os.kill(os.getpid(), num)
            time.sleep(5)
            raise Setup("the process survived its own signal")
        return code

    def answer(self, args: Args, payload: dict[str, Any]) -> int:
        b = self.b
        exit_code: int | None = b.get("exit")
        if "stdout_bytes" in b:
            write_padding(1, b["stdout_bytes"])
        elif "stdout_b64" in b:
            write_fd(1, base64.b64decode(b["stdout_b64"]))
        elif "stdout" in b:
            write_fd(1, b["stdout"].encode("utf-8"))
        elif b.get("no_output"):
            pass
        elif "error" in b:
            code = self.emit_error(b["error"], exit_code)
            return self.finish_stderr(code if exit_code is None else exit_code)
        else:
            body = success(self.tool, payload, args.values.get("--dedupe-key"))
            body.update(b.get("result", {}))
            if self.json_mode:
                write_fd(1, dumps(body))
            else:
                write_fd(2, f"sent ({body['level']}, {body['duration_ms']} ms)\n".encode())
        return self.finish_stderr(0 if exit_code is None else exit_code)

    def finish_stderr(self, code: int) -> int:
        if "stderr" in self.b:
            write_fd(2, self.b["stderr"].encode("utf-8"))
        if "stderr_bytes" in self.b:
            write_padding(2, self.b["stderr_bytes"])
        return code


def main(argv: list[str], behavior_path: str) -> int:
    try:
        base = load_behavior(behavior_path)
        n = next_call(base["log"])
        entries = base.get("sequence")
        merged = {k: v for k, v in base.items() if k != "sequence"}
        if entries:
            merged.update(entries[min(n, len(entries) - 1)])
        log = Log(base["log"], n)
        run = Run(merged, log)
        run.install_signals()
        log.write(
            "invoke", pid=os.getpid(), ppid=os.getppid(), pgid=os.getpgid(0), sid=os.getsid(0),
            start=proc_start(os.getpid()), cwd=os.getcwd(), argv=argv, env=dict(os.environ),
            open_fds=open_fds(), version=sys.version.split()[0],
        )  # fmt: skip
        try:
            code = run.go(argv)
        except Ended as ended:
            code = ended.code
        log.write("exit", code=code)
        return code
    except Setup as exc:
        sys.stderr.write(f"fakebin: {exc}\n")
        return 70
