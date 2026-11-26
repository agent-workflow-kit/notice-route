"""``prune`` next to ``send`` and ``flush`` in real processes (#26): nobody waits past a busy
timeout, nothing is lost or sent twice, a live claim is left alone."""

from __future__ import annotations

import json
import time

import pytest

from tests.flush_support import seed
from tests.prune_support import (
    DAY,
    add_many_deliveries,
    add_many_event_ids,
    add_many_expired_queue,
    counts,
)
from tests.send_support import Rig

pytestmark = pytest.mark.usefixtures("send_environment")


def real_now() -> int:
    return int(time.time())


def send_body(text: str) -> bytes:
    return json.dumps({"message": text}).encode()


def test_prune_during_sends_and_a_flush_loses_and_duplicates_nothing(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n, "max_per_hour": 0}})
    t = real_now()
    add_many_deliveries(rig, 12_000, t - 40 * DAY)
    add_many_event_ids(rig, 2_500, t - 20 * DAY)
    add_many_expired_queue(rig, 1_500, "gone", t - 3 * DAY)
    for i in range(12):
        seed(rig, message=f"queued{i:02}", now=t, deliver_after=t)
    expected = [f"queued{i:02}" for i in range(12)] + [f"sent{i}" for i in range(5)]

    prunes = [rig.spawn("prune", "--json")]
    sends = [
        rig.spawn("send", "--stdin-json", "--json", "--source", "s", stdin=send_body(f"sent{i}"))
        for i in range(5)
    ]
    flushes = [rig.spawn("flush", "--json")]
    prunes.append(rig.spawn("prune", "--json"))
    results = [p.communicate(timeout=180) for p in prunes + sends + flushes]

    assert [p.returncode for p in prunes] == [0, 0], results
    assert [p.returncode for p in sends] == [0] * 5, results
    assert flushes[0].returncode in (0, 22), results
    assert not any(b"locked" in err or b"busy" in err for _, err in results), results
    texts = [json.loads(r["text"])["message"] for r in n.events("stdin")]
    assert sorted(texts) == sorted(expected)  # every notification once

    outs = [json.loads(results[i][0])["deleted"] for i in (0, 1)]  # the two prunes
    assert sum(o["history"] for o in outs) == 12_000  # each row went exactly once
    assert sum(o["event_ids"] for o in outs) == 2_500
    assert sum(o["queue"] for o in outs) == 1_500
    state = counts(rig)
    assert state["queue"] == 0 and state["event_ids"] == 0
    assert rig.query("SELECT COUNT(*) FROM deliveries WHERE outcome = 'sent'") == [(17,)]
    assert rig.query("SELECT COUNT(*) FROM deliveries WHERE outcome = 'expired'") == [(1_500,)]


def test_a_live_claim_survives_a_prune_and_a_flush_running_together(rig: Rig) -> None:
    n = rig.notifier()
    rig.configure({"chat": {"notifier": n}})
    t = real_now()
    did, qid = seed(
        rig, now=t - 3 * DAY, created_at=t - 3 * DAY, claim="holder", claim_expires=t + 20
    )
    procs = [rig.spawn("prune", "--json"), rig.spawn("flush", "--json"), rig.spawn("prune", "--json")]
    for p in procs:
        p.communicate(timeout=120)
    assert [p.returncode for p in procs][0] == 0
    assert rig.query("SELECT claimed_by, claim_expires FROM queue WHERE id = ?", (qid,)) == [
        ("holder", t + 20)
    ]
    assert rig.query("SELECT outcome FROM deliveries WHERE id = ?", (did,)) == [("deferred",)]
    assert n.events("invoke") == []
