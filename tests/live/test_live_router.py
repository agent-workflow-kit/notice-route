"""The router against the real notifiers: a marked message per behavior, to the destinations the
person's own notifier configs name. See docs/live-testing.md. Few messages: one per test.

Nothing here asserts on elapsed time (wall-clock assertions are out of the live suite); the
quiet-hours test moves only the router's own wall clock (a child-process offset), never the
notifier's.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.live.support import (
    LiveRouter,
    Report,
    ToolConfig,
    communicate_bounded,
    found_in,
    message_for,
    schema_errors,
    title_for,
    wait_group_gone,
    window_around_now,
)

REAL = ["slack-notice", "ha-notice", "wsl-notice"]
BAD_VALUE = "notice-route-live-invalid-value"  # a value no service accepts


def destination(cfg: ToolConfig, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"kind": cfg.spec.kind, "cmd": cfg.cmd, "timeout": "20s", **extra}
    if cfg.cred_env:
        d["env"] = [cfg.cred_env]
    return d


def configure(router: LiveRouter, cfg: ToolConfig, **top: Any) -> str:
    """One destination named after the tool, one pass-through rule for source `live-test`."""
    dest = cfg.name.split("-")[0]
    router.pass_names = (cfg.cred_env,) if cfg.cred_env else ()
    qh = top.pop("quiet_hours", None)
    dconf = destination(cfg, **({"quiet": "live"} if qh else {}))
    config: dict[str, Any] = {
        "destinations": {dest: dconf},
        "rules": [{"name": "live", "match": {"source": "live-test", "level_min": "info"},
                   "notify": [dest]}],
        **top,
    }  # fmt: skip
    if qh:
        config["quiet_hours"] = {"live": qh}
    router.write_config(config)
    return dest


def history(router: LiveRouter, *args: str) -> list[dict[str, Any]]:
    r = router.run("history", "--json", *args)
    assert r.code == 0, f"history failed (exit code {r.code})"
    out = r.json()
    assert isinstance(out, list)
    return out


def skip_if_path_refused(entry: dict[str, Any]) -> None:
    """The router refuses a notifier whose path has a directory writable by others (`exec_failed`,
    the `cmd_ok` check): a missing prerequisite, not a failure of the behavior under test."""
    if entry.get("code") == "exec_failed":
        pytest.skip(
            "the router refused the notifier path (exec_failed); see 'A refused notifier path' "
            "in docs/live-testing.md"
        )


def state_files(router: LiveRouter) -> set[str]:
    return {p.name for p in router.state.iterdir()} if router.state.is_dir() else set()


@pytest.mark.parametrize("name", REAL)
def test_send_reaches_the_real_notifier(name: str, make_tool: Callable[..., ToolConfig],
                                        router: LiveRouter, report: Report,
                                        run_id: str) -> None:  # fmt: skip
    """ACCEPT: a live `send` reaches each real notifier and is recorded as `sent`. Sends ONE
    marked test message. Also: neither the router's output nor its log or database holds the
    credential value; the message text may remain in the database after delivery (recorded)."""
    cfg = make_tool(name, need_credential=True, need_wsl=(name == "wsl-notice"))
    dest = configure(router, cfg)
    msg = message_for(run_id, f"send via {name}")
    r = router.send(msg, title=title_for(run_id))
    report.record(f"send/{name}", code=r.code, stdout=report.clip(r.out, 1500),
                  stderr=report.clip(r.err, 300))  # fmt: skip
    doc = r.json()
    skip_if_path_refused(doc["destinations"][0])
    assert r.code == 0, f"send exit code {r.code}"
    assert doc["outcome"] == "ok"
    (entry,) = doc["destinations"]
    assert entry["name"] == dest
    assert entry["outcome"] == "sent"
    assert entry["attempts"] == 1 and entry["code"] is None
    rows = history(router, "--destination", dest)
    assert [x["outcome"] for x in rows] == ["sent"]
    bad_rows = [schema_errors([x], "history.json") for x in rows]  # each row against its schema
    report.record(f"history_schema/{name}", problems=[b for b in bad_rows if b])
    rows_valid = not any(bad_rows)
    assert rows_valid, "a history row does not match schemas/cli/history.json"
    log_text = (router.state / "log.jsonl").read_text("utf-8")
    db_bytes = b"".join(
        p.read_bytes() for p in sorted(router.state.glob("state.db*")) if p.is_file()
    )
    leaked = report.protected.leaks(r.out, r.err, log_text, db_bytes)
    assert not leaked, (
        "credential value found in the router output, log or state database (redacted)"
    )
    present = found_in(msg, log_text, r.out)
    assert not present, "text that must not be present was found in the router output or log"
    report.record(
        f"state_db/{name}", bytes=len(db_bytes), message_text_remains=msg.encode() in db_bytes
    )
    before = state_files(router)
    rows_before = len(history(router))
    pr = router.run("prune", "--dry-run", "--json")
    report.record(f"prune_dry_run/{name}", code=pr.code, stdout=report.clip(pr.out, 800))
    assert pr.code == 0, f"prune --dry-run exit code {pr.code}"
    prune_errors = schema_errors(pr.json(), "prune.json")
    report.record(f"prune_schema/{name}", problems=prune_errors)
    prune_valid = not prune_errors
    assert prune_valid, "prune output does not match its schema"
    assert pr.json()["dry_run"] is True
    assert len(history(router)) == rows_before
    assert state_files(router) - {"state.db-wal", "state.db-shm"} == before - {
        "state.db-wal", "state.db-shm"
    }  # fmt: skip


@pytest.mark.parametrize("name", REAL)
def test_quiet_deferral_then_flush_delivers(name: str, make_tool: Callable[..., ToolConfig],
                                            router: LiveRouter, report: Report,
                                            run_id: str) -> None:  # fmt: skip
    """ACCEPT: a deferred row is delivered by `flush` outside the window. The first `send` is
    `warn` inside a quiet window that contains now (nothing is sent); `flush` then runs with the
    router's own wall clock moved past the window end, and delivers ONE marked test message."""
    cfg = make_tool(name, need_credential=True, need_wsl=(name == "wsl-notice"))
    start, end = window_around_now(time.time())
    dest = configure(router, cfg, quiet_hours={"timezone": "UTC", "start": start, "end": end})
    msg = message_for(run_id, f"deferred then flushed via {name}")
    r = router.send(msg, title=title_for(run_id), level="warn")
    assert r.code == 0, f"send exit code {r.code}"
    (entry,) = r.json()["destinations"]
    assert (entry["outcome"], entry["reason"]) == ("deferred", "quiet")
    deliver_after = entry["deliver_after"]
    assert isinstance(deliver_after, int)
    offset = max(0.0, deliver_after - time.time()) + 90.0
    f = router.run("flush", "--json", offset=offset)
    report.record(f"flush_deferred/{name}", code=f.code, stdout=report.clip(f.out, 1500),
                  stderr=report.clip(f.err, 300))  # fmt: skip
    rows = history(router, "--destination", dest)
    skip_if_path_refused(rows[0] if rows else {})
    assert f.code == 0, f"flush exit code {f.code}"
    (state,) = f.json()["destinations"]
    assert state["state"] == "done" and state["sent"] == 1
    assert [x["outcome"] for x in rows][0] == "sent"
    leaked = report.protected.leaks(f.out, f.err)
    assert not leaked, "credential value found in the flush output (redacted)"


def alert_capture(scratch: Path) -> Path:
    """A `generic` destination: a script that stores its stdin and exits 0 (no real channel)."""
    script = scratch / "capture.sh"
    script.write_text(f"#!/bin/sh\ncat > {scratch}/alert.json\nexit 0\n")
    script.chmod(0o700)
    return script


@pytest.mark.parametrize("name", ["slack-notice", "ha-notice"])
def test_bad_credential_fails_surfaces_and_alerts(name: str, make_tool: Callable[..., ToolConfig],
                                                  router: LiveRouter, report: Report, scratch: Path,
                                                  run_id: str) -> None:  # fmt: skip
    """ACCEPT: the failure path surfaces and routes an alert for a bad credential. The named
    variable carries an invalid dummy value, so no message can be delivered. The alert goes to a
    local capture script (never a real channel), which also shows (note 4 of #27) that an alert
    carries only the rule's title and message, and the hook sees only its documented
    environment. For ha-notice the one failed call reaches the person's own instance."""
    cfg = make_tool(name, need_credential=True)
    assert cfg.cred_env
    dest = configure(router, cfg)
    capture = alert_capture(scratch)
    hook_out = scratch / "hook-env.txt"
    hook = scratch / "hook.sh"
    hook.write_text(f"#!/bin/sh\nenv | sort > {hook_out}\ncat >> {hook_out}\n")
    hook.chmod(0o700)
    config = json.loads((router.home / ".config/notice-route/config.yaml").read_text())
    config["destinations"]["capture"] = {"kind": "generic", "cmd": [str(capture)]}
    config["rules"].insert(
        0,
        {
            "name": "alerts",
            "match": {"source": "notice-route", "hook": "post-fail"},
            "notify": ["capture"],
            "level": "error",
            "title": "notice-route live test alert",
            "message": "TEST ALERT from the live suite: delivery to {{object_id}} failed.",
        },
    )
    config["hooks"] = {"post-fail": [{"cmd": str(hook), "timeout": "10s"}]}
    router.write_config(config)
    router.overrides[cfg.cred_env] = BAD_VALUE
    msg = message_for(run_id, "bad credential, must not be delivered")
    r = router.send(msg, title=title_for(run_id))
    report.record(f"bad_credential/{name}", code=r.code, stdout=report.clip(r.out, 1500),
                  stderr=report.clip(r.err, 300))  # fmt: skip
    doc = r.json()
    (entry,) = doc["destinations"]
    skip_if_path_refused(entry)
    assert entry["outcome"] in ("failed", "unknown", "queued_retry"), entry["outcome"]
    if entry["outcome"] == "queued_retry":
        pytest.skip(f"the notifier answered retryable ({entry['code']}); no failure to surface")
    assert r.code == 12 and doc["outcome"] == "partial", f"send exit code {r.code}"
    assert entry["code"]
    f = router.run("flush", "--json")
    report.record(f"bad_credential_flush/{name}", code=f.code, stdout=report.clip(f.out, 1500),
                  stderr=report.clip(f.err, 300))  # fmt: skip
    assert (scratch / "alert.json").exists(), "the failure alert did not reach its destination"
    alert = json.loads((scratch / "alert.json").read_text())
    alert_shape_ok = set(alert) <= {"title", "message", "level"}
    assert alert_shape_ok, "the alert holds a field other than title, message and level"
    present = found_in(msg, json.dumps(alert))
    assert not present, "text that must not be present was found in the alert"
    present = found_in(BAD_VALUE, json.dumps(alert))
    assert not present, "text that must not be present was found in the alert"
    env_lines = hook_out.read_text().splitlines()
    names = {line.split("=", 1)[0] for line in env_lines if "=" in line and line.isascii()}
    report.record(f"hook_env/{name}", names=sorted(n for n in names if n.isidentifier()))
    rows = history(router, "--destination", dest)
    assert any(x["outcome"] in ("failed", "unknown") for x in rows)
    leaked = report.protected.leaks(r.out, r.err, f.out, f.err)
    assert not leaked, "credential value found in the send or flush output (redacted)"


def test_wsl_interop_failed_is_retried_three_times(make_tool: Callable[..., ToolConfig],
                                                   router: LiveRouter, report: Report,
                                                   run_id: str) -> None:  # fmt: skip
    """#25: `interop_failed` is retryable for wsl-notice (cap 3, steps 60 s, 300 s). A stale
    WSL_INTEROP (a path outside `/run/WSL/<digits>_interop`) is provoked on purpose, but the real
    tool ignores such a candidate and falls back to the newest valid interop socket, so on a
    working WSL host a labelled test toast MAY be delivered (counted in the runbook as a possible
    extra message). When the real tool answers another code the test skips (the claim cannot be
    checked), it never fails for that."""
    cfg = make_tool("wsl-notice", need_wsl=True)
    dest = configure(router, cfg)
    router.overrides["WSL_INTEROP"] = str(router.home / "stale-interop-socket")
    msg = message_for(run_id, "stale interop, must not appear")
    r1 = router.send(msg, title=title_for(run_id))
    report.record("wsl_interop/first", code=r1.code, stdout=report.clip(r1.out, 1500),
                  stderr=report.clip(r1.err, 300))  # fmt: skip
    (entry,) = r1.json()["destinations"]
    skip_if_path_refused(entry)
    if entry["code"] != "interop_failed":
        pytest.skip(
            f"the real tool answered {entry['outcome']}/{entry['code']}, not interop_failed; "
            "a labelled test toast may have been delivered through the fallback socket"
        )
    assert entry["outcome"] == "queued_retry"
    f2 = router.run("flush", "--json", offset=120.0)
    f3 = router.run("flush", "--json", offset=120.0 + 600.0)
    report.record("wsl_interop/flushes", second=report.clip(f2.out, 800),
                  third=report.clip(f3.out, 800))  # fmt: skip
    last = history(router, "--destination", dest)[0]
    assert last["outcome"] == "failed" and last["code"] == "retries_exhausted"
    assert last["attempts"] == 3


def due_row_without_credential(router: LiveRouter, cfg: ToolConfig, run_id: str, what: str) -> None:
    """Queue a due `single` row without any notifier call: `send` with the credential variable
    absent from its environment queues it (`deferred`, `env_missing`)."""
    assert cfg.cred_env
    keep = router.pass_names
    router.pass_names = ()
    router.overrides.pop(cfg.cred_env, None)
    r = router.send(message_for(run_id, what), title=title_for(run_id))
    router.pass_names = keep
    assert r.code == 0, f"send exit code {r.code}"
    (entry,) = r.json()["destinations"]
    assert (entry["outcome"], entry["reason"]) == ("deferred", "env_missing")


def flush_lock_held(state: Path, dest: str) -> bool:
    path = state / "locks" / f"{dest}.flush"
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def wait_for_flush_lock(proc: Any, state: Path, dest: str, seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and proc.poll() is None:
        if flush_lock_held(state, dest):
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("sig", ["SIGSTOP", "SIGTERM"])
def test_flusher_signals_with_a_real_notifier(sig: str, make_tool: Callable[..., ToolConfig],
                                              router: LiveRouter, report: Report,
                                              run_id: str) -> None:  # fmt: skip
    """#23 notes (1) and (2), observed with a real notifier (one marked message per signal):
    a stopped flusher keeps the flush lock, so a second `flush` reports `already_running` and
    makes no call; a TERMed flusher either finishes its call or makes none, and the row ends
    `sent` exactly once after the next `flush`. Skips when the lock window is not seen. Worst
    case for SIGTERM: the notifier child delivers after the router is gone and the follow-up
    `flush` sends again (a duplicate, counted in the runbook); the follow-up waits until the
    router (and its process group) is reaped."""
    cfg = None
    for n in ("slack-notice", "ha-notice"):
        try:
            cfg = make_tool(n, need_credential=True)
            break
        except pytest.skip.Exception:
            continue
    if cfg is None:
        pytest.skip("neither slack-notice nor ha-notice is designated")
    dest = configure(router, cfg)
    due_row_without_credential(router, cfg, run_id, f"flusher {sig}")
    router.pass_names = (cfg.cred_env,) if cfg.cred_env else ()
    proc = router.start("flush", "--json")
    seen = wait_for_flush_lock(proc, router.state, dest)
    report.record(f"flusher/{sig}", lock_seen=seen)
    if not seen:
        communicate_bounded(proc, timeout=60.0)
        pytest.skip("the flush lock was not observed held (the call was too quick)")
    proc.send_signal(getattr(signal, sig))
    if sig == "SIGSTOP":
        second = router.run("flush", "--json")
        report.record("flusher/SIGSTOP/second", code=second.code,
                      stdout=report.clip(second.out, 800))  # fmt: skip
        states = [d["state"] for d in second.json()["destinations"]]
        only_running = states == ["already_running"]
        assert only_running, "the second flush did not report already_running"
        proc.send_signal(signal.SIGCONT)
    out, err, timed_out = communicate_bounded(proc, timeout=120.0)
    group_gone = wait_group_gone(proc.pid)
    report.record(f"flusher/{sig}/first", code=proc.returncode, timed_out=timed_out,
                  group_gone=group_gone,
                  stdout=report.clip(out.decode("utf-8", "replace"), 800))  # fmt: skip
    follow = router.run("flush", "--json")
    rows = history(router, "--destination", dest)
    report.record(f"flusher/{sig}/follow", code=follow.code, stdout=report.clip(follow.out, 800))
    sent = [x["outcome"] for x in rows].count("sent")
    assert sent == 1, f"the row ended `sent` {sent} times, expected once"
    leaked = report.protected.leaks(out, err, follow.out, follow.err)
    assert not leaked, "credential value found in the flusher output (redacted)"
