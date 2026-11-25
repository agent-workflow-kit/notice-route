"""Each scripted behavior of the fake notifier does what its name says.

Tests that start processes record the PIDs through the fake's own log and signal exactly those
(PID plus start time, see ``Notifier.kill_tracked``); the fixtures clean up at teardown.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests import sibling_schemas as ss
from tests.fakes import (
    CANONICAL,
    Notifier,
    alive,
    feed,
    last_json,
    proc_start,
    run,
    scrubbed_env,
    start,
)

Make = Callable[..., Notifier]


def wait_gone(pid: int, start_time: str | None, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid, start_time):
            return True
        time.sleep(0.02)
    return False


# exit codes and envelopes -------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["slack-notice", "ha-notice"])
@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        ("usage", 2),
        ("config_invalid", 2),
        ("internal", 1),
        ("refused", 20),
        ("host_rejected", 12),
        ("auth_required", 12),
        ("credential_invalid", 12),
        ("rate_limited", 12),
        ("upstream_error", 12),
        ("network_error", 12),
        ("outcome_unknown", 12),
        ("wrong_host_response", 12),
        ("unavailable", 12),
    ],
)
def test_an_error_code_exits_with_the_documented_code_and_a_schema_valid_envelope(
    make_notifier: Make, home: Path, tool: str, code: str, exit_code: int
) -> None:
    error = {"code": code}
    if code == "refused":
        error["reason"] = "empty_message"
    n = make_notifier(tool, tool=tool, error=error, validate_input=False)
    result = run(n, home)
    assert result.returncode == exit_code
    ss.validate(last_json(result), tool, "cli/error.json")
    assert last_json(result)["error"]["code"] == code


def test_an_error_carries_the_flags_and_details_it_was_given(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(
        error={
            "code": "rate_limited",
            "retryable": True,
            "unknown_outcome": False,
            "retry_after_s": 4,
            "details": {"http_status": 429},
        }
    )
    body = last_json(run(n, home))["error"]
    assert (body["retryable"], body["unknown_outcome"], body["retry_after_s"]) == (True, False, 4)
    assert body["details"] == {"http_status": 429}


@pytest.mark.parametrize("code", [0, 1, 3, 13, 22, 99, 143, 255])
def test_any_exit_code_with_no_output(make_notifier: Make, home: Path, code: int) -> None:
    n = make_notifier(exit=code, no_output=True)
    result = run(n, home)
    assert (result.returncode, result.stdout) == (code, b"")


def test_any_stdout_is_printed_verbatim_and_the_exit_code_is_independent(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(stdout="garbage {not json", exit=20, stderr="oops\n")
    result = run(n, home)
    assert (result.returncode, result.stdout, result.stderr) == (
        20,
        b"garbage {not json",
        b"oops\n",
    )


def test_raw_bytes_that_are_not_utf8_can_be_printed(make_notifier: Make, home: Path) -> None:
    n = make_notifier(stdout_b64="/w==", exit=0)
    assert run(n, home).stdout == b"\xff"


def test_a_success_result_can_be_edited_field_by_field(make_notifier: Make, home: Path) -> None:
    n = make_notifier(result={"http_status": 204, "extra": True})
    body = last_json(run(n, home))
    assert (body["http_status"], body["extra"]) == (204, True)
    assert ss.errors(body, "slack-notice", "cli/send.json")  # now outside the pinned schema


def test_a_nonzero_exit_over_the_default_success_body_is_possible(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(exit=12)
    result = run(n, home)
    assert (result.returncode, last_json(result)["outcome"]) == (12, "sent")


# time ---------------------------------------------------------------------------------------


def test_sleep_delays_the_answer(make_notifier: Make, home: Path) -> None:
    n = make_notifier(sleep=0.4)
    began = time.monotonic()
    result = run(n, home)
    assert time.monotonic() - began >= 0.4
    assert result.returncode == 0


def test_linger_keeps_the_process_alive_after_the_output_is_written(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(linger=30)
    proc = start(n, home)
    assert proc.stdin is not None and proc.stdout is not None
    feed(proc, b'{"message": "m"}')
    line = proc.stdout.readline()  # the result is already out
    assert b'"outcome":"sent"' in line
    assert proc.poll() is None
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)


def test_hang_waits_until_killed_and_the_kill_is_a_signal_death(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(hang=True)
    proc = start(n, home)
    assert proc.stdin is not None
    feed(proc, b'{"message": "m"}')
    n.wait_for("hang_started")
    assert proc.poll() is None
    os.killpg(proc.pid, signal.SIGTERM)
    assert proc.wait(timeout=5) == -signal.SIGTERM
    assert n.events("exit") == []  # no exit record: it never got to write one


def test_a_hang_never_outlives_max_seconds(make_notifier: Make, home: Path) -> None:
    n = make_notifier(hang=True, max_seconds=0.3)
    began = time.monotonic()
    result = run(n, home)
    assert result.returncode == 124
    assert time.monotonic() - began < 5


def test_a_hang_ends_when_its_parent_is_gone(
    make_notifier: Make, home: Path, tmp_path: Path
) -> None:
    n = make_notifier(hang=True, max_seconds=60, validate_input=False)
    # A throwaway parent starts the fake, waits for its first log record and exits; the fake
    # must notice that its parent changed and stop.
    parent = subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            f"{n.stub} {' '.join(CANONICAL)} </dev/null >/dev/null 2>&1 & i=0; "
            f"while [ ! -s {n.log_path} ] && [ $i -lt 200 ]; do sleep 0.05; i=$((i+1)); done",
        ],
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")},
        start_new_session=True,
    )
    parent.wait(timeout=5)
    record = n.wait_for("invoke")
    assert wait_gone(record["pid"], record["start"], timeout=10)
    n.wait_for("exit")
    assert n.events("exit")[0]["code"] == 124


def test_sleep_before_read_leaves_stdin_unread_for_a_while(make_notifier: Make, home: Path) -> None:
    n = make_notifier(hang=True, hang_before_read=True)
    proc = start(n, home)
    assert proc.stdin is not None
    n.wait_for("hang_started")
    assert n.events("stdin") == []
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)


# signals ------------------------------------------------------------------------------------


def test_ignore_sigterm_survives_the_signal_until_sigkill(make_notifier: Make, home: Path) -> None:
    n = make_notifier(hang=True, signals={"TERM": "ignore"})
    proc = start(n, home)
    assert proc.stdin is not None
    feed(proc, b'{"message": "m"}')
    n.wait_for("hang_started")
    os.killpg(proc.pid, signal.SIGTERM)
    n.wait_for("signal")
    time.sleep(0.3)
    assert proc.poll() is None
    assert n.events("signal")[0]["name"] == "TERM"
    os.killpg(proc.pid, signal.SIGKILL)
    assert proc.wait(timeout=5) == -signal.SIGKILL


def test_a_signal_can_end_the_fake_with_exit_128_plus_n(make_notifier: Make, home: Path) -> None:
    n = make_notifier(hang=True, signals={"TERM": "exit:143", "HUP": "exit:129"})
    proc = start(n, home)
    assert proc.stdin is not None
    feed(proc, b'{"message": "m"}')
    n.wait_for("hang_started")
    os.killpg(proc.pid, signal.SIGTERM)
    out, _ = proc.communicate(timeout=5)
    assert (proc.returncode, out) == (143, b"")
    assert n.events("exit")[-1]["code"] == 143


@pytest.mark.parametrize(
    ("name", "signum"), [("KILL", 9), ("TERM", 15), ("SEGV", 11), ("ABRT", 6), ("HUP", 1)]
)
def test_the_fake_can_be_killed_by_a_signal_after_its_output(
    make_notifier: Make, home: Path, name: str, signum: int
) -> None:
    n = make_notifier(die_by_signal=name)
    result = run(n, home)
    assert result.returncode == -signum
    assert b'"outcome":"sent"' in result.stdout  # output written first, as a late kill would be


# descendants --------------------------------------------------------------------------------


def test_a_plain_grandchild_shares_the_process_group_and_dies_with_it(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(linger=30, grandchild={"lifetime": 30})
    proc = start(n, home)
    assert proc.stdin is not None
    feed(proc, b'{"message": "m"}')
    g = n.wait_for("grandchild")
    assert g["pgid"] == proc.pid and g["sid"] == proc.pid
    assert alive(g["pid"], g["start"])
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)
    assert wait_gone(g["pid"], g["start"])


def test_a_setsid_grandchild_leaves_the_group_and_survives_its_kill(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(linger=30, grandchild={"setsid": True, "lifetime": 30})
    proc = start(n, home)
    assert proc.stdin is not None
    feed(proc, b'{"message": "m"}')
    g = n.wait_for("grandchild")
    assert g["pgid"] != proc.pid and g["sid"] != proc.pid
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)
    assert alive(g["pid"], g["start"])  # the group kill did not reach it
    n.kill_tracked()
    assert wait_gone(g["pid"], g["start"])


def test_a_grandchild_holding_stdout_keeps_the_pipe_open_after_the_fake_exits(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(grandchild={"setsid": True, "hold_stdout": True, "lifetime": 30})
    proc = start(n, home)
    assert proc.stdin is not None and proc.stdout is not None
    feed(proc, b'{"message": "m"}')
    assert proc.wait(timeout=5) == 0
    g = n.wait_for("grandchild")
    assert g["hold_stdout"] is True
    with pytest.raises(subprocess.TimeoutExpired):
        proc.communicate(timeout=0.5)  # no EOF: the grandchild holds the write end
    n.kill_tracked()
    out, _ = proc.communicate(timeout=5)  # EOF now
    assert b'"outcome":"sent"' in out


def test_a_grandchild_that_does_not_hold_stdout_lets_the_pipe_close(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(grandchild={"setsid": True, "lifetime": 30})
    result = run(n, home, timeout=5)
    assert result.returncode == 0
    g = n.events("grandchild")[0]
    assert g["hold_stdout"] is False
    n.kill_tracked()
    assert wait_gone(g["pid"], g["start"])


def test_a_double_fork_grandchild_is_orphaned_and_in_its_own_session(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(
        grandchild={"double_fork": True, "setsid": True, "hold_stdout": True, "lifetime": 30}
    )
    proc = start(n, home)
    assert proc.stdin is not None
    feed(proc, b'{"message": "m"}')
    assert proc.wait(timeout=5) == 0
    g = n.wait_for("grandchild")
    assert g["double_fork"] is True
    assert g["sid"] == g["pid"] != proc.pid
    assert alive(g["pid"], g["start"])
    with pytest.raises(subprocess.TimeoutExpired):
        proc.communicate(timeout=0.5)
    n.kill_tracked()
    proc.communicate(timeout=5)
    assert wait_gone(g["pid"], g["start"])


def test_a_grandchild_never_outlives_its_lifetime(make_notifier: Make, home: Path) -> None:
    n = make_notifier(grandchild={"setsid": True, "lifetime": 0.3})
    run(n, home, timeout=5)
    g = n.events("grandchild")[0]
    assert wait_gone(g["pid"], g["start"], timeout=5)


# stdin and output volume --------------------------------------------------------------------


def test_close_stdin_exits_without_reading_and_the_writer_sees_epipe(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(close_stdin=True, linger=0.5)
    proc = start(n, home)
    assert proc.stdin is not None
    n.wait_for("stdin_closed")
    chunk = b"x" * 65536
    with pytest.raises(BrokenPipeError):
        for _ in range(64):
            proc.stdin.write(chunk)
            proc.stdin.flush()
    assert proc.stdout is not None
    out = proc.stdout.read()
    assert proc.wait(timeout=5) == 0
    assert b'"outcome":"sent"' in out
    assert n.events("stdin") == []


@pytest.mark.parametrize("size", [65536, 65537, 1_000_000])
def test_oversized_stdout_is_written_in_full(make_notifier: Make, home: Path, size: int) -> None:
    n = make_notifier(stdout_bytes=size)
    result = run(n, home)
    assert len(result.stdout) == size
    assert result.returncode == 0


def test_oversized_stderr_is_written_in_full(make_notifier: Make, home: Path) -> None:
    n = make_notifier(stderr_bytes=20000)
    result = run(n, home)
    assert len(result.stderr) == 20000
    assert result.returncode == 0


def test_an_oversized_writer_survives_a_reader_that_goes_away(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(stdout_bytes=5_000_000, exit=0)
    proc = start(n, home)
    assert proc.stdin is not None and proc.stdout is not None
    feed(proc, b'{"message": "m"}')
    proc.stdout.read(10)
    proc.stdout.close()  # the reader leaves: the fake must not hang or traceback
    assert proc.wait(timeout=10) == 0
    assert b"Traceback" not in (proc.stderr.read() if proc.stderr else b"")


# scripting across calls ---------------------------------------------------------------------


def test_a_sequence_answers_call_by_call_and_repeats_its_last_entry(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(
        sequence=[{"error": {"code": "network_error", "retryable": True}}, {"exit": 22}, {}]
    )
    codes = [run(n, home).returncode for _ in range(5)]
    assert codes == [12, 22, 0, 0, 0]
    assert [r["n"] for r in n.events("invoke")] == [0, 1, 2, 3, 4]


def test_sequence_numbers_are_unique_under_eight_parallel_calls(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(sequence=[{}])
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: run(n, home), range(8)))
    assert sorted(r["n"] for r in n.events("invoke")) == list(range(8))
    assert len({r["pid"] for r in n.events("invoke")}) == 8


def test_behavior_can_be_changed_between_calls(make_notifier: Make, home: Path) -> None:
    n = make_notifier()
    assert run(n, home).returncode == 0
    n.set(exit=7, no_output=True)
    assert run(n, home).returncode == 7


def test_two_notifiers_for_two_destinations_log_separately(make_notifier: Make, home: Path) -> None:
    a = make_notifier("one", tool="slack-notice")
    b = make_notifier("two", tool="ha-notice")
    run(a, home)
    run(b, home)
    assert (len(a.events("invoke")), len(b.events("invoke"))) == (1, 1)
    assert last_json(run(b, home))["tool"] == "ha-notice"


# the fake's own safety ----------------------------------------------------------------------


def test_a_broken_behavior_file_is_one_line_and_exit_70(make_notifier: Make, home: Path) -> None:
    n = make_notifier()
    n.behavior_path.write_text("{not json", "utf-8")
    result = run(n, home)
    assert result.returncode == 70
    assert result.stderr.startswith(b"fakebin: ") and result.stderr.count(b"\n") == 1
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize(
    "bad",
    [
        {"log": "relative.log"},
        {"exit": "seven"},
        {"sleep": -1},
        {"signals": {"TERM": "explode"}},
        {"die_by_signal": "NOPE"},
        {"grandchild": {"lifetime": 9999}},
        {"tool": "nope-notice"},
        {"surprise": True},
    ],
)
def test_a_broken_setup_is_exit_70_never_a_traceback(
    make_notifier: Make, home: Path, bad: dict[str, object]
) -> None:
    n = make_notifier()
    n.behavior_path.write_text(json.dumps({"log": str(n.log_path), **bad}), "utf-8")
    result = run(n, home)
    assert result.returncode == 70, result.stderr
    assert result.stderr.startswith(b"fakebin: ") and b"Traceback" not in result.stderr


def test_the_fake_needs_no_home_config_or_network(make_notifier: Make, tmp_path: Path) -> None:
    n = make_notifier()
    result = run(n, tmp_path, env=scrubbed_env(tmp_path / "private"))
    assert result.returncode == 0


def test_proc_start_tells_two_processes_with_one_pid_apart() -> None:
    mine = proc_start(os.getpid())
    assert mine is not None
    assert alive(os.getpid(), mine)
    assert not alive(os.getpid(), "1")


# review round 1 -----------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["stdout", "stdout_b64", "stdout_bytes", "no_output"])
def test_an_error_together_with_scripted_stdout_is_a_setup_error(
    make_notifier: Make, home: Path, key: str
) -> None:
    value: object = {"stdout": "x", "stdout_b64": "eA==", "stdout_bytes": 3, "no_output": True}[key]
    n = make_notifier()
    n.behavior_path.write_text(
        json.dumps({"log": str(n.log_path), "error": {"code": "internal"}, key: value}), "utf-8"
    )
    result = run(n, home)
    assert result.returncode == 70
    assert result.stderr.startswith(b"fakebin: ")


@pytest.mark.parametrize(("name", "signum"), [("SEGV", 11), ("ABRT", 6)])
def test_dying_by_a_core_signal_first_forbids_core_dumps(
    make_notifier: Make, home: Path, name: str, signum: int
) -> None:
    n = make_notifier(die_by_signal=name)
    assert run(n, home).returncode == -signum
    assert n.events("die")[0]["core_limit"] == [0, 0]


def test_a_grandchild_can_hold_stderr_open(make_notifier: Make, home: Path) -> None:
    n = make_notifier(grandchild={"setsid": True, "hold_stderr": True, "lifetime": 30})
    proc = start(n, home)
    feed(proc, b'{"message": "m"}')
    assert proc.wait(timeout=5) == 0
    assert n.wait_for("grandchild")["hold_stderr"] is True
    with pytest.raises(subprocess.TimeoutExpired):
        proc.communicate(timeout=0.5)
    n.kill_tracked()
    proc.communicate(timeout=5)


@pytest.mark.parametrize(("name", "signum", "code"), [("INT", 2, 130), ("HUP", 1, 129)])
def test_int_and_hup_can_end_the_fake_with_a_scripted_exit_code(
    make_notifier: Make, home: Path, name: str, signum: int, code: int
) -> None:
    n = make_notifier(hang=True, signals={name: f"exit:{code}"})
    proc = start(n, home)
    feed(proc, b'{"message": "m"}')
    n.wait_for("hang_started")
    os.killpg(proc.pid, signum)
    proc.communicate(timeout=5)
    assert proc.returncode == code
    assert n.events("exit")[-1]["code"] == code


def test_a_writer_that_keeps_stdin_open_does_not_hold_the_fake_past_max_seconds(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(max_seconds=0.5)
    proc = start(n, home)
    began = time.monotonic()
    assert proc.wait(timeout=5) == 124  # stdin is never closed
    assert time.monotonic() - began < 4
    assert proc.stdin is not None
    proc.stdin.close()


def test_a_reader_that_never_reads_does_not_hold_the_fake_past_max_seconds(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(stdout_bytes=5_000_000, max_seconds=0.5)
    proc = start(n, home)
    feed(proc, b'{"message": "m"}')
    assert proc.wait(timeout=5) == 124  # stdout is never read
    assert proc.stdout is not None
    proc.stdout.close()


def test_close_stdin_gives_epipe_to_a_writer_with_a_large_payload(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(close_stdin=True, linger=0.5)
    proc = start(n, home)
    assert proc.stdin is not None
    n.wait_for("stdin_closed")
    with pytest.raises(BrokenPipeError):
        proc.stdin.write(b"x" * 300_000)  # more than the pipe holds: the write cannot be buffered
        proc.stdin.flush()
    proc.wait(timeout=5)


def test_a_surrogate_in_argv_is_a_usage_error_not_a_traceback(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier()
    result = run(n, home, args=["send", "--stdin-json", "--json", "--bogus\udcff"])
    assert result.returncode == 2
    assert b"Traceback" not in result.stderr
    assert n.events("invoke")


def test_a_surrogate_in_scripted_text_does_not_traceback(make_notifier: Make, home: Path) -> None:
    n = make_notifier(stderr="a\ud800b")
    result = run(n, home)
    assert result.returncode == 0
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize(
    "payload",
    [
        '{"message": 5, "title": [1], "level": {"a": 1}}',
        '{"message": "m\\ud800", "title": "t\\ud800"}',
        "[" * 100_000,
        '{"message": "' + "x" * 10 + '", "title": null}',
    ],
)
def test_odd_payloads_with_validation_off_never_traceback(
    make_notifier: Make, home: Path, payload: str
) -> None:
    for tool in ("slack-notice", "ha-notice"):
        n = make_notifier(tool, tool=tool, validate_input=False)
        result = run(n, home, payload)
        assert b"Traceback" not in result.stderr, result.stderr
        assert result.returncode == 0


def test_a_grandchild_records_itself_before_it_reports(make_notifier: Make, home: Path) -> None:
    n = make_notifier(grandchild={"setsid": True, "lifetime": 30})
    run(n, home, timeout=5)
    events = [r["event"] for r in n.records()]
    assert events.index("grandchild_spawn") < events.index("grandchild_started")
    assert events.index("grandchild_started") < events.index("grandchild")
    full = n.events("grandchild")[0]
    assert n.events("grandchild_started")[0]["pid"] == full["pid"]
    n.kill_tracked()


def test_run_returns_promptly_on_timeout_even_when_a_setsid_grandchild_holds_stdout(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(hang=True, grandchild={"setsid": True, "hold_stdout": True, "lifetime": 30})
    began = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run(n, home, timeout=1.5)
    assert time.monotonic() - began < 10
    g = n.events("grandchild")[0]
    assert wait_gone(g["pid"], g["start"])


def test_kill_tracked_also_reaches_a_process_start_recorded_without_a_log(
    make_notifier: Make, home: Path
) -> None:
    n = make_notifier(hang_before_read=True, hang=True)
    proc = start(n, home)
    assert any(t.pid == proc.pid and t.role == "popen" for t in n.tracked())
    n.wait_for("hang_started")
    n.kill_tracked()
    assert proc.wait(timeout=5) == -signal.SIGKILL
