"""The public log writer (#22): schema, rotation, lock, bad files."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from notice_route import log
from notice_route.gateway import FakeClock, Gateway
from tests.send_support import log_errors

ROOT = Path(__file__).resolve().parent.parent


def gw(home: Path) -> Gateway:
    return Gateway(passwd_home=lambda: str(home), clock=FakeClock(wall=1_700_000_000.0))


def delivery(**kw: object) -> dict[str, object]:
    fields: dict[str, object] = dict(
        source="s", hook=None, rule="r", destination="d", level="info", outcome="sent",
        reason=None, code=None, attempts=1, title_sha256=log.sha16("t"),
        message_sha256=log.sha16("m"), event_id=None, dedupe_key_sha256=None, duration_ms=5,
    )  # fmt: skip
    fields.update(kw)
    return log.record(
        "delivery", wall=1_700_000_000.123, actor="auto:x", decision_id=None, **fields
    )


def test_record_matches_schema_and_timestamp(home: Path) -> None:
    rec = delivery()
    assert rec["ts"] == "2023-11-14T22:13:20.123Z"
    assert log_errors(rec) == []


def test_flush_record_shape_matches_schema() -> None:
    rec = log.record(
        "flush", wall=1.0, actor="auto:x", decision_id="id1",
        due=1, sent=1, queued_retry=0, failed=0, unknown=0, dropped=0, expired=0, duration_ms=3,
    )  # fmt: skip
    assert log_errors(rec) == []


def test_write_appends_ascii_lines_and_creates_private_file(home: Path) -> None:
    g = gw(home)
    with g.open_state_dir() as state:
        w = log.LogWriter(g, state)
        assert w.write(delivery(source="é"))
        assert w.write(delivery())
    path = home / ".local/state/notice-route/log.jsonl"
    data = path.read_bytes()
    data.decode("ascii")
    assert len(data.splitlines()) == 2
    assert path.stat().st_mode & 0o777 == 0o600


def test_disabled_writer_writes_nothing(home: Path) -> None:
    w = log.LogWriter(gw(home), None)
    assert w.write(delivery()) and w.problems == 0


def test_rotation_keeps_three(home: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    g = gw(home)
    monkeypatch.setattr(log, "MAX_BYTES", 200)
    with g.open_state_dir() as state:
        w = log.LogWriter(g, state)
        for _ in range(40):
            assert w.write(delivery())
    names = sorted(p.name for p in (home / ".local/state/notice-route").iterdir())
    assert names == ["log.1", "log.2", "log.3", "log.jsonl", "log.lock"]
    for n in ("log.1", "log.2", "log.3"):
        assert (home / ".local/state/notice-route" / n).stat().st_size >= 200


def test_symlinked_log_is_skipped_with_a_problem(home: Path, tmp_path: Path) -> None:
    g = gw(home)
    with g.open_state_dir() as state:
        target = tmp_path / "elsewhere"
        target.write_text("")
        (home / ".local/state/notice-route/log.jsonl").symlink_to(target)
        w = log.LogWriter(g, state)
        assert w.write(delivery()) is False
        assert w.problems == 1
    assert target.read_text() == ""


def test_a_held_lock_skips_the_line_after_the_wait(home: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    g = gw(home)
    monkeypatch.setattr(log, "LOCK_WAIT_S", 0.0)
    with g.open_state_dir() as state:
        held = g.open_file(state, log.LOCK_NAME, mode="a", create=True)
        assert g.try_lock(held, 0)
        w = log.LogWriter(g, state)
        assert w.write(delivery()) is False and w.problems == 1
        g.unlock(held)
        held.close()
        assert w.write(delivery())


def test_concurrent_writers_lose_no_line(home: Path) -> None:
    g = Gateway(passwd_home=lambda: str(home))
    with g.open_state_dir() as state:

        def work() -> None:
            w = log.LogWriter(g, state)
            for _ in range(25):
                assert w.write(delivery())

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    lines = (home / ".local/state/notice-route/log.jsonl").read_text().splitlines()
    assert len(lines) == 100 and all(json.loads(x)["tool"] == "notice-route" for x in lines)


def test_unknown_field_is_refused() -> None:
    import pytest

    with pytest.raises(ValueError):
        log.record("delivery", wall=1.0, actor="auto:x", decision_id=None, title="no")


class _ShortWrites:
    """A log file whose write puts half of the bytes down and then fails (a full disk)."""

    def __init__(self, real: object) -> None:
        self._real = real

    def write(self, data: memoryview) -> int:
        half = len(data) // 2
        self._real.write(data[:half])  # type: ignore[attr-defined]
        raise OSError(28, "no space left on device")

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


def test_a_short_write_leaves_no_partial_line_for_the_next_record(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    g = gw(home)
    with g.open_state_dir() as state:
        w = log.LogWriter(g, state)
        assert w.write(delivery(source="first"))
        real_open = Gateway.open_file
        failing = {"on": True}

        def open_file(self: Gateway, parent: object, name: str, **kw: object) -> object:
            f = real_open(self, parent, name, **kw)  # type: ignore[arg-type]
            return _ShortWrites(f) if failing["on"] and name == log.LOG_NAME else f

        monkeypatch.setattr(Gateway, "open_file", open_file)
        assert w.write(delivery(source="lost")) is False and w.problems == 1
        failing["on"] = False
        assert w.write(delivery(source="last"))
    lines = (home / ".local" / "state" / "notice-route" / log.LOG_NAME).read_text().splitlines()
    assert [json.loads(x)["source"] for x in lines] == ["first", "last"]


def _hook_record() -> dict[str, object]:
    return log.record(
        "hook", wall=1.0, actor="auto:x", decision_id=None, hook="post-fail", destination="d",
        mode="sync", result="ok", duration_ms=1,
    )  # fmt: skip


def test_a_1_0_line_without_the_override_key_still_validates() -> None:
    flush_rec = log.record(
        "flush", wall=1.0, actor="auto:x", decision_id="id1",
        due=1, sent=1, queued_retry=0, failed=0, unknown=0, dropped=0, expired=0, duration_ms=3,
    )  # fmt: skip
    for rec in (delivery(), flush_rec, _hook_record()):
        assert rec["override"] is None  # new code still writes the null
        old = {k: v for k, v in rec.items() if k != "override"}
        assert log_errors(old) == [], rec["record"]


def test_an_override_record_still_requires_its_override() -> None:
    rec = log.record(
        "override", wall=1.0, actor="auto:x", decision_id=None, destination="d",
        override="disabled", duration_ms=1,
    )  # fmt: skip
    assert log_errors(rec) == []
    assert log_errors({k: v for k, v in rec.items() if k != "override"}) != []
