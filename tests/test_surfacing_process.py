"""Taking surfaced rows across real processes (#27): a kill in the middle, a loss after the
commit, and two flushers racing. Each child is a ``python -I`` with the private home of the
test; every signal goes to the PID the child wrote itself, checked against its ``Popen`` handle."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from notice_route.gateway import SYSTEM_TZ_DIRS
from notice_route.queue import insert_delivery
from notice_route.store import open_store
from tests.alerts_support import MakeHook, Setup, hooks_config
from tests.fakes import scrubbed_env
from tests.send_support import NOW, Rig

_HAVE_ZONES = any((Path(d) / "America" / "New_York").is_file() for d in SYSTEM_TZ_DIRS)
pytestmark = [
    pytest.mark.skipif(not _HAVE_ZONES, reason="no system time zone database"),
    pytest.mark.usefixtures("send_environment"),
]

_HEAD = (
    "import os, sys, time\n"
    "from notice_route import alerts, flush as flush_mod\n"
    "from notice_route.cli import main\n"
    "from notice_route.gateway import Gateway\n"
    "home, tmp, marker = sys.argv[1:4]\n"
    "def announce():\n"
    "    open(marker + '.tmp', 'w').write(str(os.getpid()))\n"
    "    os.replace(marker + '.tmp', marker)\n"
    "gw = Gateway(passwd_home=lambda: home, exec_check_from=tmp)\n"
)
_IN_TRANSACTION = (
    _HEAD
    + "def route(*a, **k):\n"
    + "    announce()\n"
    + "    time.sleep(30)\n"
    + "alerts._route = route\n"
    + "sys.exit(main(['flush', '--json'], gw))\n"
)
_AFTER_COMMIT = (
    _HEAD
    + "real = flush_mod._take_surfaced\n"
    + "def take(run, store):\n"
    + "    taken = real(run, store)\n"
    + "    announce()\n"
    + "    time.sleep(30)\n"
    + "    return taken\n"
    + "flush_mod._take_surfaced = take\n"
    + "sys.exit(main(['flush', '--json'], gw))\n"
)
_RACER = (
    _HEAD
    + "alerts.MAX_ROWS = 1\n"  # one row per run: the two processes alternate
    + "seen = open(marker + '.rows', 'a')\n"
    + "real = flush_mod.take_surfaced\n"
    + "count = [0]\n"
    + "def take(*a, **k):\n"
    + "    taken = real(*a, **k)\n"
    + "    for job in taken.jobs:\n"
    + "        seen.write(str(job.payload['event_id']) + '\\n'); seen.flush()\n"
    + "    count[0] = taken.taken\n"
    + "    return taken\n"
    + "flush_mod.take_surfaced = take\n"
    + "def skip(run, dest):\n"
    + "    t = flush_mod._Tally(dest.name)\n"
    + "    t.skipped = True\n"
    + "    return t\n"
    + "flush_mod._destination = skip\n"
    + "announce()\n"
    + "while not os.path.exists(marker + '.go'):\n"
    + "    time.sleep(0.005)\n"
    + "while True:\n"
    + "    main(['flush', '--json'], gw)\n"
    + "    if count[0] == 0:\n"
    + "        break\n"
)


def child(rig: Rig, script: str, marker: Path) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", script, str(rig.home), str(rig.tmp_path), str(marker)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=scrubbed_env(rig.home),
        cwd="/",
    )
    rig.procs.append(proc)
    return proc


def wait_for_file(path: Path, timeout: float = 10.0) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return path.read_text()
        time.sleep(0.01)
    raise AssertionError(f"{path} did not appear within {timeout}s")


def kill_when_announced(proc: subprocess.Popen[bytes], marker: Path) -> None:
    assert int(wait_for_file(marker)) == proc.pid  # the exact child, not a guess
    os.kill(proc.pid, signal.SIGKILL)
    assert proc.wait(timeout=5) == -signal.SIGKILL


def test_a_kill_inside_the_taking_transaction_leaves_the_row_for_the_next_flush(
    rig: Rig,
) -> None:
    s = Setup(rig)
    s.fail("boom")
    marker = rig.tmp_path / "pid-a"
    kill_when_announced(child(rig, _IN_TRANSACTION, marker), marker)
    assert [r[4] for r in s.surfaced()] == [0]  # rolled back with the transaction
    assert s.ops.events("invoke") == []
    code, _, _ = s.flush()
    assert code == 0 and len(s.ops.events("invoke")) == 1  # taken, exactly once
    s.flush()
    assert len(s.ops.events("invoke")) == 1


def test_a_kill_after_the_commit_loses_neither_the_alert_nor_a_sync_hook(
    rig: Rig, make_hook: MakeHook
) -> None:
    """At least once (the outbox, #37): the hook of a row taken by a killed flush runs in the next
    flush, once."""
    h = make_hook()
    s = Setup(rig, **hooks_config(h))
    s.fail("boom")
    marker = rig.tmp_path / "pid-b"
    kill_when_announced(child(rig, _AFTER_COMMIT, marker), marker)
    assert [r[4] for r in s.surfaced()] == [1]
    assert rig.query("SELECT COUNT(*) FROM queue") == [(1,)]  # the alert is queued
    assert rig.query("SELECT COUNT(*) FROM hook_outbox") == [(1,)]  # and the hook is kept
    assert h.runs() == []
    rig.clock.jump_wall(time.time() - NOW + 60)  # the child queued the alert at the real time
    s.flush()
    assert len(s.ops.events("invoke")) == 1  # the alert is delivered by the next flush
    assert len(h.runs()) == 1  # the hook ran
    s.flush()
    assert len(h.runs()) == 1  # and only once
    assert rig.query("SELECT COUNT(*) FROM hook_outbox") == [(0,)]


def test_two_flushers_taking_one_row_per_run_split_sixty_rows_exactly(rig: Rig) -> None:
    s = Setup(rig, rules=[{"name": "pt", "match": {"level_min": "info"}, "notify": ["chat"]}])
    with open_store(rig.gateway()) as store, store.write() as conn:
        for i in range(60):
            did = insert_delivery(
                conn,
                ts=NOW,
                source="s",
                level="info",
                outcome="failed",
                destination="chat",
                code=f"fault_{i}",
                attempts=1,
                surfaced=1,
            )
            conn.execute(
                "INSERT INTO surfaced (destination, code, period, delivery_id) VALUES (?, ?, ?, ?)",
                ("chat", f"fault_{i}", "2023-11-14", did),
            )
    markers = [rig.tmp_path / "racer-1", rig.tmp_path / "racer-2"]
    procs = [child(rig, _RACER, m) for m in markers]
    for m in markers:
        wait_for_file(m, timeout=10)  # both are imported and waiting
    (rig.tmp_path / "racer-1.go").write_text("")
    (rig.tmp_path / "racer-2.go").write_text("")
    for p in procs:
        assert p.wait(timeout=60) == 0
    taken = [
        sorted(int(x) for x in Path(f"{m}.rows").read_text().split())
        if Path(f"{m}.rows").exists()
        else []
        for m in markers
    ]
    assert not set(taken[0]) & set(taken[1])  # disjoint
    assert sorted(taken[0] + taken[1]) == list(range(1, 61))  # complete
    assert [r[4] for r in s.surfaced()] == [1] * 60
