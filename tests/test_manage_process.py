"""``queue drop`` against real processes (#34): a flusher that holds a claim, a flusher and a
dropper that meet one row, and droppers that meet each other. Only processes the test holds a
handle to are ever signalled (``Rig.finish``)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from notice_route.gateway import SystemClock
from tests.flush_support import seed
from tests.send_support import Rig

pytestmark = pytest.mark.usefixtures("send_environment")


def configure(rig: Rig, **behavior: Any) -> Any:
    n = rig.notifier(**behavior)
    rig.configure({"chat": {"notifier": n}})
    return n


def test_a_row_claimed_by_a_running_flush_is_never_dropped(rig: Rig) -> None:
    n = configure(rig, sleep=2.0)
    now = int(time.time())
    seed(rig, now=now, deliver_after=now)
    proc = rig.spawn("flush", "--json")
    n.wait_for("invoke", timeout=30)  # the call is in flight: the row is claimed
    code, out, err = rig.run(
        "queue", "drop", "1", "--json", gateway=rig.gateway(clock=SystemClock())
    )
    assert code == 20, (out, err)
    assert json.loads(out)["error"]["reason"] == "claimed"
    assert rig.query("SELECT COUNT(*) FROM queue") == [(1,)]
    flushed, ferr = proc.communicate(timeout=60)
    assert proc.returncode == 0, ferr
    assert json.loads(flushed)["destinations"][0]["sent"] == 1
    assert rig.query("SELECT outcome, reason FROM deliveries") == [("sent", None)]
    assert rig.run("queue", "drop", "1", "--json")[0] == 22  # gone now: nothing to drop


def test_a_flusher_and_a_dropper_meeting_one_row_have_exactly_one_winner(rig: Rig) -> None:
    n = configure(rig)
    now = int(time.time())
    seed(rig, now=now, deliver_after=now)
    flusher = rig.spawn("flush", "--json")
    dropper = rig.spawn("queue", "drop", "1", "--json")
    dropped_out, derr = dropper.communicate(timeout=60)
    flushed_out, ferr = flusher.communicate(timeout=60)
    assert flusher.returncode in (0, 22), ferr
    sent = len(n.events("invoke"))
    outcome = rig.query("SELECT outcome, reason FROM deliveries")
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
    if dropper.returncode == 0:  # the dropper won: nothing was ever sent
        assert (sent, outcome) == (0, [("dropped", "manual")])
    else:  # the flusher won: the drop found a live claim or no row
        assert dropper.returncode in (20, 22), derr
        assert (sent, outcome) == (1, [("sent", None)])
    assert json.loads(dropped_out) and json.loads(flushed_out)


def test_droppers_meeting_each_other_have_exactly_one_winner(rig: Rig) -> None:
    configure(rig)
    seed(rig)
    procs = [rig.spawn("queue", "drop", "1", "--json") for _ in range(3)]
    results = [(p.communicate(timeout=60), p.returncode) for p in procs]
    assert sorted(code for _, code in results) == [0, 22, 22], results
    assert rig.query("SELECT outcome, reason FROM deliveries") == [("dropped", "manual")]
    assert rig.query("SELECT COUNT(*) FROM queue") == [(0,)]
