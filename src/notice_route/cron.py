"""``cron install`` and ``cron uninstall``: the one tagged block in the user's crontab
(docs/interface.md, "cron install and cron uninstall").

The block is five lines: the header ``# notice-route managed v1 flush=1m``, a ``PATH=`` line, the
``flush --quiet`` line every minute, the ``prune --quiet`` line at 03:17 and the end line. Every
other byte of the crontab is kept as it is (CRLF, bytes that are not UTF-8, a NUL); the only
change outside the block is a final newline when the table written would not end in one (cron
refuses such a table).

How the crontab is handled (every call through ``Gateway.run_crontab``):

- ``crontab -l`` from the cleaned ``PATH`` with ``LC_ALL=C``. Only exit 1 with ``no crontab for``
  on stderr is an empty crontab; every other failure (a kill, a timeout, more than the output cap,
  another exit code) is exit 1 and nothing is written.
- The crontab is read a second time just before the write; when it differs from the first read,
  nothing is written (exit 1). A crontab that is absent and one that is empty are the same.
- The whole new table goes in with one ``crontab -`` call. Nothing is written when the new table
  equals the old one. ``crontab``'s own text is never echoed (it can quote crontab lines).
- The block is found by exact line shapes (after removing trailing spaces, tabs and carriage
  returns): a start line begins ``# notice-route managed v`` and the end line is exactly
  ``# notice-route managed end``. Anything but one start followed by one end, or neither, is
  refused (exit 2) untouched.

Judgment calls where the interface is silent (the conservative reading in each case):

- ``uninstall`` loads no config and checks no program path: it must work when the config is
  broken or the program has moved.
- An emptied crontab is written as an empty table through ``crontab -`` (never ``crontab -r``).
- The ``PATH=`` line is the three system directories first, then the program's directory when it
  is not one of them (a directory ahead of them could shadow a system tool).
- A block line longer than 999 bytes (cron reads a line into 1000 bytes) is refused (exit 2).
- The program must be an executable file; it is not otherwise checked, because the cron line runs
  what the user ran, and its path must match ``[A-Za-z0-9_./+-]+``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Literal

from .gateway import Gateway, RunResult
from .validate import Config

__all__ = [
    "END_LINE",
    "HEADER",
    "START_PREFIX",
    "CronError",
    "CronResult",
    "build_block",
    "path_line",
    "run_cron",
]

START_PREFIX: Final = "# notice-route managed v"
HEADER: Final = "# notice-route managed v1 flush=1m"
END_LINE: Final = "# notice-route managed end"
SYSTEM_DIRS: Final = ("/usr/local/bin", "/usr/bin", "/bin")
MAX_LINE_BYTES: Final = 999
VALUE: Final = re.compile(r"[A-Za-z0-9_./+-]+")

_START: Final = START_PREFIX.encode()
_END: Final = END_LINE.encode()


class CronError(Exception):
    """A refusal or a failure with a fixed message: ``code`` is ``usage`` (exit 2) or ``internal``
    (exit 1). The message never holds a value from the config, the crontab or ``crontab``."""

    def __init__(self, code: Literal["usage", "internal"], message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def exit_code(self) -> int:
        return 2 if self.code == "usage" else 1


@dataclass(frozen=True)
class CronResult:
    document: dict[str, Any]
    lines: list[str]  # the human output, one string per line


# --- the block -------------------------------------------------------------------------------


def path_line(program_dir: str) -> str:
    dirs: list[str] = list(SYSTEM_DIRS)
    if program_dir not in dirs:
        dirs.append(program_dir)
    return "PATH=" + ":".join(dirs)


def _plain(value: str) -> bool:
    return VALUE.fullmatch(value) is not None


def build_block(program: str, env_file: str | None) -> list[str]:
    """The block's lines. ``program`` is the resolved absolute path; ``program`` and ``env_file``
    must match ``[A-Za-z0-9_./+-]+`` (else ``CronError`` usage), so quoting them with single
    quotes is enough and nothing needs cron's ``%`` escape."""
    if not program.startswith("/") or not _plain(program):
        raise CronError(
            "usage", "the path of notice-route has characters that cannot go in a crontab"
        )
    if env_file is not None and (not env_file.startswith("/") or not _plain(env_file)):
        raise CronError("usage", "cron.env_file has characters that cannot go in a crontab")
    program_dir = program.rsplit("/", 1)[0] or "/"
    source = f"set -a && . '{env_file}' && set +a && " if env_file is not None else ""
    lines = [
        HEADER,
        path_line(program_dir),
        f"* * * * * {source}'{program}' flush --quiet",
        f"17 3 * * * {source}'{program}' prune --quiet",
        END_LINE,
    ]
    if any(len(line.encode()) > MAX_LINE_BYTES for line in lines):
        raise CronError("usage", "a line of the cron block would be longer than cron reads")
    return lines


# --- the crontab text ------------------------------------------------------------------------


def _norm(line: bytes) -> bytes:
    return line.rstrip(b" \t\r")


def _split(raw: bytes) -> list[bytes]:
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def _locate(lines: list[bytes]) -> tuple[int, int] | None:
    """``(start, end)`` of the one block, None when there is none; any other shape raises."""
    ends = [i for i, line in enumerate(lines) if _norm(line) == _END]
    starts = [i for i, line in enumerate(lines) if _norm(line).startswith(_START) and i not in ends]
    if not starts and not ends:
        return None
    if len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0]:
        return starts[0], ends[0]
    if len(starts) > 1 or len(ends) > 1:
        problem = "more than one managed block line"
    elif not ends:
        problem = "a start line without an end line"
    elif not starts:
        problem = "an end line without a start line"
    else:
        problem = "an end line before the start line"
    raise CronError(
        "usage", f"the crontab has {problem} for notice-route; nothing was changed, fix it by hand"
    )


def _join(lines: list[bytes]) -> bytes:
    return b"\n".join(lines) + b"\n" if lines else b""


def _install_table(raw: bytes, block: list[str]) -> tuple[bytes, bool]:
    """The table with the block added or replaced, and whether a block was there."""
    lines = _split(raw)
    new = [line.encode() for line in block]
    where = _locate(lines)
    if where is None:
        return _join(lines + new), False
    start, end = where
    return _join(lines[:start] + new + lines[end + 1 :]), True


def _uninstall_table(raw: bytes) -> tuple[bytes, list[bytes] | None]:
    lines = _split(raw)
    where = _locate(lines)
    if where is None:
        return raw, None
    start, end = where
    return _join(lines[:start] + lines[end + 1 :]), lines[start : end + 1]


# --- the crontab program ---------------------------------------------------------------------


def _failed(what: str, result: RunResult) -> CronError:
    if result.status == "spawn_failed":
        return CronError("internal", f"{what}: crontab cannot be run")
    if result.status == "timeout":
        return CronError("internal", f"{what}: crontab timed out")
    if result.status == "overflow":
        return CronError("internal", f"{what}: the output of crontab is too large")
    if result.status == "signaled":
        return CronError("internal", f"{what}: crontab was killed")
    return CronError("internal", f"{what}: crontab exited with {result.returncode}")


def _read(gateway: Gateway) -> bytes:
    """The stored crontab; an absent one is empty."""
    result = gateway.run_crontab(["-l"])
    if result.status == "exited" and result.returncode == 0:
        return result.stdout
    if result.status == "exited" and result.returncode == 1 and b"no crontab for" in result.stderr:
        return b""
    raise _failed("cannot read the crontab", result)


def _write(gateway: Gateway, table: bytes) -> None:
    result = gateway.run_crontab(["-"], stdin=table)
    if not (result.status == "exited" and result.returncode == 0):
        raise _failed("cannot write the crontab (it is unchanged)", result)


def _commit(gateway: Gateway, before: bytes, after: bytes) -> None:
    """Write ``after`` unless the crontab is no longer ``before``."""
    if _read(gateway) != before:
        raise CronError(
            "internal", "the crontab changed while it was being edited; nothing written"
        )
    _write(gateway, after)


# --- commands --------------------------------------------------------------------------------


def _text(block: list[str]) -> str:
    return "\n".join(block) + "\n"


def run_cron(
    gateway: Gateway, config: Config | None, action: Literal["install", "uninstall"], dry_run: bool
) -> CronResult:
    """``install`` needs the config (for ``cron.env_file``); ``uninstall`` ignores it."""
    if action == "uninstall":
        return _uninstall(gateway, dry_run)
    return _install(gateway, config, dry_run)


def _install(gateway: Gateway, config: Config | None, dry_run: bool) -> CronResult:
    env_file = config.cron_env_file if config is not None else None
    program = gateway.program_path()
    if program is None:
        raise CronError("internal", "cannot find the notice-route executable")
    if env_file is not None and not (_plain(env_file) and gateway.check_env_file(env_file)):
        raise CronError("usage", "cron.env_file is missing, unsafe or has unusable characters")
    block = build_block(program, env_file)
    before = _read(gateway)
    after, existed = _install_table(before, block)
    if after == before:
        verb = "unchanged"
    else:
        verb = "updated" if existed else "installed"
        if not dry_run:
            _commit(gateway, before, after)
    suffix = " (dry run)" if dry_run else ""
    document = {"action": verb, "block": _text(block), "dry_run": dry_run, "env_file": env_file}
    lines = [verb + suffix]
    if verb != "unchanged":
        lines += block
    return CronResult(document, lines)


def _uninstall(gateway: Gateway, dry_run: bool) -> CronResult:
    before = _read(gateway)
    after, removed = _uninstall_table(before)
    suffix = " (dry run)" if dry_run else ""
    if removed is None:
        return CronResult(
            {"action": "unchanged", "block": None, "dry_run": dry_run}, ["unchanged" + suffix]
        )
    if not dry_run:
        _commit(gateway, before, after)
    shown = "\n".join(line.decode("utf-8", "replace") for line in removed) + "\n"
    return CronResult(
        {"action": "removed", "block": shown, "dry_run": dry_run}, ["removed" + suffix]
    )
