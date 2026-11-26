"""A fake ``crontab`` for notice-route's tests: it never touches a real crontab.

The state lives in one directory the stub bakes in (see ``tests/fakecron.py``):

- ``table``: the stored crontab (absent means the user has none, as for a real crontab).
- ``calls.jsonl``: one JSON line per invocation (argv, the environment it got, the stdin size and
  what kind of call it was), so a test can count reads and writes.
- ``behavior.json``: how the next calls behave (read on every call).

Supported calls are the two the router uses, ``crontab -l`` and ``crontab -`` (the new table on
stdin). Anything else exits 64 and is logged as ``other``, so a test notices a router that
starts using ``-r`` or ``-e``.

Behavior keys (all optional):

- ``list_exit`` / ``list_stderr``: ``-l`` prints the text on stderr and exits with the code.
- ``write_exit`` / ``write_stderr``: the same for ``-``; the table is then left untouched.
- ``huge_list``: ``-l`` prints this many bytes of one line (more than the router's cap).
- ``hang``: ``list`` or ``write`` sleeps (bounded by ``hang_s``, default 20) instead of working.
- ``mutate_after_list``: ``{"n": 2, "append": "text"}``: right after the n-th ``-l`` has printed
  its table, the stored table gets the text appended (a concurrent editor).
- ``stdout_noise``: text ``-`` prints on stdout (a real crontab prints nothing).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

USER = "fakeuser"


def _log(state: Path, entry: dict[str, Any]) -> int:
    path = state / "calls.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return sum(1 for _ in path.open(encoding="utf-8"))


def main(argv: list[str], state_dir: str) -> int:
    state = Path(state_dir)
    behavior: dict[str, Any] = {}
    behavior_path = state / "behavior.json"
    if behavior_path.exists():
        behavior = json.loads(behavior_path.read_text("utf-8"))
    table = state / "table"
    kind = "list" if argv == ["-l"] else "write" if argv == ["-"] else "other"
    stdin = sys.stdin.buffer.read() if kind == "write" else b""
    _log(
        state,
        {
            "kind": kind,
            "argv": argv,
            "env": dict(os.environ),
            "stdin_len": len(stdin),
        },
    )
    if kind == "other":
        sys.stderr.write("fake crontab: unsupported call\n")
        return 64
    if behavior.get("hang") == kind:
        time.sleep(float(behavior.get("hang_s", 20)))
        return 0
    if kind == "list":
        if behavior.get("list_exit"):
            sys.stderr.write(str(behavior.get("list_stderr", "")))
            return int(behavior["list_exit"])
        if behavior.get("huge_list"):
            sys.stdout.write("# " + "x" * int(behavior["huge_list"]) + "\n")
            return 0
        absent = not table.exists()
        if absent:
            sys.stderr.write(f"no crontab for {USER}\n")
        else:
            sys.stdout.buffer.write(table.read_bytes())
            sys.stdout.buffer.flush()
        mutate = behavior.get("mutate_after_list")
        if mutate:
            lists = sum(
                1
                for line in (state / "calls.jsonl").read_text("utf-8").splitlines()
                if json.loads(line)["kind"] == "list"
            )
            if lists == mutate["n"]:
                old = table.read_bytes() if table.exists() else b""
                table.write_bytes(old + mutate["append"].encode("utf-8"))
        return 1 if absent else 0
    if behavior.get("write_exit"):
        sys.stderr.write(str(behavior.get("write_stderr", "")))
        return int(behavior["write_exit"])
    tmp = state / "table.tmp"
    tmp.write_bytes(stdin)
    os.replace(tmp, table)
    with (state / "writes.log").open("ab") as handle:
        handle.write(stdin + b"\0--end-of-write--\0")
    if behavior.get("stdout_noise"):
        sys.stdout.write(str(behavior["stdout_noise"]))
    return 0
