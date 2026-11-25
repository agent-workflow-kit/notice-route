"""Classifying one call into the outcome table (#20), row by row.

The classification is pure: a ``CallResult`` (built here, or from a real fake-notifier call for a
few rows) goes in, an ``Outcome`` comes out. Real timeouts are never waited for; a ``timeout``
``RunResult`` is built by hand.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from notice_route.gateway import STDOUT_LIMIT, Gateway, RunResult
from notice_route.notify import CallResult, call_notifier
from notice_route.outcome import Outcome, classify
from tests.decide_support import dest
from tests.fakes import Notifier

DIGEST = "a" * 64
KINDS = ("slack-notice", "ha-notice", "wsl-notice")


def ran(
    status: str = "exited",
    returncode: int | None = 0,
    stdout: bytes = b"",
    signal: int | None = None,
    overflow: str | None = None,
) -> CallResult:
    run = RunResult(status, returncode, signal, stdout, b"", overflow, 123, 0.5)  # type: ignore[arg-type]
    return CallResult("ran", run=run)


def env(code: str = "upstream_error", **fields: Any) -> bytes:
    body: dict[str, Any] = {"code": code, "message": "m", **fields}
    return json.dumps({"error": body}).encode()


def flags(retryable: Any, unknown: Any, code: str = "upstream_error") -> bytes:
    return env(code, retryable=retryable, unknown_outcome=unknown)


def refusal(reason: str, code: str = "refused") -> bytes:
    return env(code, reason=reason, retryable=None, unknown_outcome=None)


def cl(result: CallResult, kind: str = "slack-notice", key: bool = True) -> Outcome:
    return classify(result, kind=kind, has_dedupe_key=key)


def shape(o: Outcome) -> tuple[Any, ...]:
    return (o.outcome, o.code, o.refusal)


# --- rows 1 to 4: the call itself ---------------------------------------------------------


def test_row1_exec_failed() -> None:
    assert shape(cl(CallResult("exec_failed"))) == ("failed", "exec_failed", None)
    assert cl(CallResult("exec_failed")).counted


@pytest.mark.parametrize("kind", ["generic", *KINDS])
def test_row1_spawn_failed_status_is_exec_failed(kind: str) -> None:
    result = ran("spawn_failed", None)
    assert shape(cl(result, kind)) == ("failed", "exec_failed", None)


@pytest.mark.parametrize("kind", ["generic", *KINDS])
def test_row2_timeout_is_killed_whatever_it_printed(kind: str) -> None:
    result = ran("timeout", 0, stdout=b'{"outcome":"sent"}')
    assert shape(cl(result, kind)) == ("unknown", "killed", None)
    assert shape(cl(ran("timeout", None, signal=9), kind)) == ("unknown", "killed", None)


@pytest.mark.parametrize("kind", ["generic", *KINDS])
def test_row3_overflow(kind: str) -> None:
    result = ran("overflow", None, signal=9, overflow="stdout")
    assert shape(cl(result, kind)) == ("unknown", "output_overflow", None)
    result = ran("overflow", 0, overflow="stderr")
    assert shape(cl(result, kind)) == ("unknown", "output_overflow", None)


@pytest.mark.parametrize("kind", ["generic", *KINDS])
@pytest.mark.parametrize("sig", [1, 9, 11, 15])
def test_row4_signaled(kind: str, sig: int) -> None:
    assert shape(cl(ran("signaled", None, signal=sig), kind)) == ("unknown", "signal", None)


def test_lost_is_unknown_killed() -> None:
    assert shape(cl(CallResult("lost"))) == ("unknown", "killed", None)
    assert cl(CallResult("lost")).counted


# --- rows 5 and 6: generic --------------------------------------------------------------------


def test_row5_generic_exit0_is_sent_and_stdout_is_not_read() -> None:
    out = cl(ran(stdout=b'{"outcome":"sent"}'), "generic")
    assert shape(out) == ("sent", None, None)
    assert out.notifier_outcome is None


@pytest.mark.parametrize("rc", [1, 2, 12, 20, 22, 124, 126, 127, 143, 255])
def test_row6_generic_any_other_exit_failed(rc: int) -> None:
    out = cl(ran(returncode=rc, stdout=flags(True, False)), "generic")
    assert shape(out) == ("failed", "generic_failed", None)


# --- rows 7 and 8: exit 0 and 22 --------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_row7_exit0_sent_with_notifier_outcome(kind: str) -> None:
    out = cl(ran(stdout=b'{"outcome":"sent","tool":"x"}'), kind)
    assert shape(out) == ("sent", None, None)
    assert out.notifier_outcome == "sent"


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"garbage",
        b"[]",
        b'{"outcome":"sent","outcome":"x"}',  # duplicate key
        b'{"outcome":NaN}',
        b'{"outcome":1}',
        b'{"outcome":"UPPER"}',
        b'{"outcome":"has space"}',
        b'{"outcome":"' + b"a" * 33 + b'"}',
        b'{"outcome":"\\u001b[31m"}',
        b"\xff\xfe",
        b'{"outcome":"sent"} trailing',
        b'{"outcome":"sent\n"}',
        b'{"x":' * 40 + b"1" + b"}" * 40,
        b'{"outcome":null}',
    ],
)
def test_row7_unparseable_stdout_is_still_sent_with_null(stdout: bytes) -> None:
    out = cl(ran(stdout=stdout))
    assert shape(out) == ("sent", None, None)
    assert out.notifier_outcome is None


def test_row7_oversize_stdout_given_directly_is_still_sent_null() -> None:
    # the gateway caps stdout at STDOUT_LIMIT; a hand-built larger one must not parse either
    big = b'{"outcome":"sent","pad":"' + b"a" * STDOUT_LIMIT + b'"}'
    assert cl(ran(stdout=big)).notifier_outcome is None


def test_row7_exit0_with_an_error_envelope_is_sent() -> None:
    out = cl(ran(stdout=flags(False, False)))
    assert shape(out) == ("sent", None, None)
    assert out.notifier_outcome is None


def test_row8_exit22_is_disabled_with_notifier_outcome() -> None:
    out = cl(ran(returncode=22, stdout=b'{"outcome":"disabled"}'))
    assert shape(out) == ("disabled", None, None)
    assert out.notifier_outcome == "disabled"


@pytest.mark.parametrize("stdout", [b"", b"junk", flags(True, True), b'{"outcome":"X"}'])
def test_row8_exit22_with_unreadable_stdout_is_still_disabled_null(stdout: bytes) -> None:
    out = cl(ran(returncode=22, stdout=stdout))
    assert shape(out) == ("disabled", None, None)
    assert out.notifier_outcome is None


# --- row 9: unparseable envelope --------------------------------------------------------------

BAD_ENVELOPES = [
    b"",
    b"not json",
    b"[]",
    b'"error"',
    b"{}",
    b'{"error":null}',
    b'{"error":"x"}',
    b'{"error":[]}',
    b'{"error":{"code":"a","code":"b","message":"m","retryable":false,"unknown_outcome":false}}',
    b'{"error":{"code":"a","retryable":NaN,"unknown_outcome":false}}',
    b'{"error":{"code":"a","retryable":false,"unknown_outcome":false}} x',
    b"\xef\xbb\xbf" + flags(False, False),  # BOM
    b"\xff",
    b'{"error":{"retryable":false,"unknown_outcome":false}}',  # no code
    b'{"error":{"code":7,"retryable":false,"unknown_outcome":false}}',
    b'{"error":{"code":"Bad-Code","retryable":false,"unknown_outcome":false}}',
    b'{"error":{"code":"' + b"a" * 65 + b'","retryable":false,"unknown_outcome":false}}',
    b'{"error":{"code":"a\\nb","retryable":false,"unknown_outcome":false}}',
    b'{"error":{"code":"\\u001b[31mx","retryable":false,"unknown_outcome":false}}',
    b'{"error":{"code":"a\\n","retryable":false,"unknown_outcome":false}}',
]


@pytest.mark.parametrize("stdout", BAD_ENVELOPES)
@pytest.mark.parametrize(
    ("rc", "outcome"), [(12, "unknown"), (1, "unknown"), (20, "failed")], ids=["12", "1", "20"]
)
def test_row9_unparseable_envelope(stdout: bytes, rc: int, outcome: str) -> None:
    out = cl(ran(returncode=rc, stdout=stdout))
    assert shape(out) == (outcome, "envelope_invalid", None)


@pytest.mark.parametrize("rc", [12, 1])
@pytest.mark.parametrize("bad", ["true", 1, 0, None, "yes", [], {}])
def test_flag_that_is_not_a_boolean_makes_the_envelope_unparseable(rc: int, bad: Any) -> None:
    for retryable, unknown in ((bad, False), (False, bad)):
        out = cl(ran(returncode=rc, stdout=flags(retryable, unknown)))
        assert shape(out) == ("unknown", "envelope_invalid", None)


@pytest.mark.parametrize("rc", [12, 1])
def test_missing_flag_makes_the_envelope_unparseable(rc: int) -> None:
    for body in (
        {"code": "a", "message": "m", "retryable": False},
        {"code": "a", "message": "m", "unknown_outcome": False},
    ):
        out = cl(ran(returncode=rc, stdout=json.dumps({"error": body}).encode()))
        assert shape(out) == ("unknown", "envelope_invalid", None)


def test_exit20_missing_or_bad_reason_is_unparseable() -> None:
    for body in (
        {"code": "refused", "message": "m"},
        {"code": "refused", "reason": None},
        {"code": "refused", "reason": 3},
        {"code": "refused", "reason": "Bad Reason"},
        {"code": "refused", "reason": "x" * 65},
    ):
        out = cl(ran(returncode=20, stdout=json.dumps({"error": body}).encode()))
        assert shape(out) == ("failed", "envelope_invalid", None), body


# --- rows 10 to 12: exit 12 -------------------------------------------------------------------


def test_row10_exit12_not_retryable_known_outcome_failed_with_error_code() -> None:
    out = cl(ran(returncode=12, stdout=flags(False, False, "auth_required")))
    assert shape(out) == ("failed", "auth_required", None)


def test_row11_exit12_retryable_is_queued_retry_with_error_code() -> None:
    out = cl(ran(returncode=12, stdout=flags(True, False, "network_error")))
    assert shape(out) == ("queued_retry", "network_error", None)
    assert out.counted


def test_retryable_true_for_a_non_retryable_code_is_believed() -> None:
    out = cl(ran(returncode=12, stdout=flags(True, False, "credential_invalid")))
    assert shape(out) == ("queued_retry", "credential_invalid", None)


def test_retryable_false_for_a_retryable_code_is_believed() -> None:
    out = cl(ran(returncode=12, stdout=flags(False, False, "unavailable")))
    assert shape(out) == ("failed", "unavailable", None)


def test_unknown_error_code_is_used_verbatim_and_the_flags_decide() -> None:
    out = cl(ran(returncode=12, stdout=flags(True, False, "brand_new_code_9")))
    assert shape(out) == ("queued_retry", "brand_new_code_9", None)
    out = cl(ran(returncode=12, stdout=flags(False, False, "brand_new_code_9")))
    assert shape(out) == ("failed", "brand_new_code_9", None)
    out = cl(ran(returncode=12, stdout=flags(False, True, "brand_new_code_9")))
    assert shape(out) == ("unknown", "brand_new_code_9", None)


def test_row12_ha_notice_retryable_unknown_with_dedupe_key_is_queued_retry() -> None:
    result = ran(returncode=12, stdout=flags(True, True, "outcome_unknown"))
    assert shape(cl(result, "ha-notice", True)) == ("queued_retry", "outcome_unknown", None)


def test_row12_ha_notice_without_dedupe_key_is_unknown() -> None:
    result = ran(returncode=12, stdout=flags(True, True, "outcome_unknown"))
    assert shape(cl(result, "ha-notice", False)) == ("unknown", "outcome_unknown", None)


def test_row12_ha_notice_unknown_but_not_retryable_is_unknown() -> None:
    result = ran(returncode=12, stdout=flags(False, True, "outcome_unknown"))
    assert shape(cl(result, "ha-notice", True)) == ("unknown", "outcome_unknown", None)


@pytest.mark.parametrize("kind", ["slack-notice", "wsl-notice"])
def test_row12_other_kinds_are_unknown_even_when_retryable(kind: str) -> None:
    result = ran(returncode=12, stdout=flags(True, True, "outcome_unknown"))
    assert shape(cl(result, kind, True)) == ("unknown", "outcome_unknown", None)


# --- rows 13 to 15: exit 20 -------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["link_invalid", "link_scheme"])
@pytest.mark.parametrize("kind", KINDS)
def test_row13_link_refusal_is_a_link_refused_retry(reason: str, kind: str) -> None:
    out = cl(ran(returncode=20, stdout=refusal(reason)), kind)
    assert shape(out) == ("queued_retry", reason, "link_refused")


def test_row14_command_keyword_on_ha_notice_is_a_keyword_refused_retry() -> None:
    out = cl(ran(returncode=20, stdout=refusal("command_keyword")), "ha-notice")
    assert shape(out) == ("queued_retry", "command_keyword", "keyword_refused")


@pytest.mark.parametrize("kind", ["slack-notice", "wsl-notice"])
def test_row15_command_keyword_on_other_kinds_fails(kind: str) -> None:
    out = cl(ran(returncode=20, stdout=refusal("command_keyword")), kind)
    assert shape(out) == ("failed", "command_keyword", None)


@pytest.mark.parametrize("reason", ["payload_invalid", "empty_message", "never_heard_of_it"])
def test_row15_any_other_reason_fails_with_the_reason(reason: str) -> None:
    out = cl(ran(returncode=20, stdout=refusal(reason)), "ha-notice")
    assert shape(out) == ("failed", reason, None)


def test_exit20_ignores_flags_that_are_present_and_wrong() -> None:
    body = env("refused", reason="link_scheme", retryable="x", unknown_outcome=7)
    out = cl(ran(returncode=20, stdout=body))
    assert shape(out) == ("queued_retry", "link_scheme", "link_refused")


def test_exit20_code_other_than_refused_still_uses_the_reason() -> None:
    out = cl(ran(returncode=20, stdout=refusal("link_invalid", code="whatever")))
    assert shape(out) == ("queued_retry", "link_invalid", "link_refused")


# --- rows 16 and 17: exit 1 -------------------------------------------------------------------


def test_row16_exit1_known_outcome_is_deployment_fault() -> None:
    out = cl(ran(returncode=1, stdout=flags(False, False, "internal")))
    assert shape(out) == ("failed", "deployment_fault", None)


def test_row16_exit1_retryable_true_does_not_retry() -> None:
    out = cl(ran(returncode=1, stdout=flags(True, False, "internal")))
    assert shape(out) == ("failed", "deployment_fault", None)


def test_row17_exit1_unknown_outcome_is_unknown_with_error_code() -> None:
    out = cl(ran(returncode=1, stdout=flags(False, True, "internal")))
    assert shape(out) == ("unknown", "internal", None)


# --- row 18: 128+N ----------------------------------------------------------------------------


@pytest.mark.parametrize("rc", [129, 137, 143, 159])
def test_row18_signal_exit_with_empty_stdout_is_queued_retry(rc: int) -> None:
    assert shape(cl(ran(returncode=rc))) == ("queued_retry", "signal", None)


@pytest.mark.parametrize("rc", [129, 137, 143, 159])
@pytest.mark.parametrize("stdout", [b" ", b"\n", b"x", b'{"outcome":"sent"}', flags(False, False)])
def test_row18_signal_exit_with_any_output_is_unknown(rc: int, stdout: bytes) -> None:
    assert shape(cl(ran(returncode=rc, stdout=stdout))) == ("unknown", "signal", None)


# --- row 19: everything else ------------------------------------------------------------------


@pytest.mark.parametrize("rc", [2, 3, 10, 11, 13, 21, 23, 64, 124, 126, 127, 128, 160, 255])
def test_row19_any_other_exit_is_deployment_fault(rc: int) -> None:
    out = cl(ran(returncode=rc, stdout=flags(True, True)))
    assert shape(out) == ("failed", "deployment_fault", None)


# --- internal faults --------------------------------------------------------------------------


def test_not_started_is_an_internal_fault_with_no_outcome() -> None:
    out = cl(CallResult("not_started"))
    assert out.outcome is None
    assert out.internal
    assert not out.counted


def test_env_missing_is_deferred_with_no_attempt() -> None:
    out = cl(CallResult("env_missing", missing=("X",)))
    assert (out.outcome, out.reason, out.code, out.counted) == (
        "deferred",
        "env_missing",
        None,
        False,
    )


def test_exited_without_a_return_code_is_not_trusted() -> None:
    assert shape(cl(ran("exited", None))) == ("unknown", "signal", None)


# --- hostile text never survives -------------------------------------------------------------


def test_hostile_envelope_text_never_reaches_the_outcome() -> None:
    hostile = "\x1b[31mSECRET-LOOKING\x00" + "x" * 100000
    stdout = json.dumps(
        {
            "error": {
                "code": "upstream_error",
                "message": hostile,
                "details": {"k": hostile},
                "retryable": False,
                "unknown_outcome": False,
            }
        }
    ).encode()[:STDOUT_LIMIT]
    out = cl(ran(returncode=12, stdout=stdout))
    assert shape(out) == ("unknown", "envelope_invalid", None)  # cut mid-string: not parseable
    full = json.dumps(
        {
            "error": {
                "code": "upstream_error",
                "message": hostile[:1000],
                "retryable": False,
                "unknown_outcome": False,
            },
            "extra": hostile[:1000],
        }
    ).encode()
    out = cl(ran(returncode=12, stdout=full))
    assert shape(out) == ("failed", "upstream_error", None)
    for value in (out.code, out.reason, out.notifier_outcome):
        assert value is None or "\x1b" not in value


# --- the same table through a real call of the fake notifier ----------------------------------


@pytest.fixture
def real(
    make_notifier: Callable[..., Notifier], tmp_path: Path
) -> Callable[..., Outcome]:
    def run(kind: str = "slack-notice", key: bool = True, **behavior: Any) -> Outcome:
        note = make_notifier(tool="ha-notice" if kind == "ha-notice" else "slack-notice", **behavior)
        base = Path(note.cmd[0]).parent
        base.chmod(0o755)
        gateway = Gateway(
            environ=lambda: {"PATH": str(base), "LANG": "C.UTF-8"}, exec_check_from=str(base)
        )
        call = call_notifier(
            gateway,
            dest(kind=kind, cmd=tuple(note.cmd)),
            title="t",
            message="m",
            level="info",
            link="",
            dedupe_key=DIGEST if key else None,
        )
        return classify(call, kind=kind, has_dedupe_key=key)

    return run


def test_real_exit0(real: Callable[..., Outcome]) -> None:
    out = real()
    assert (out.outcome, out.notifier_outcome) == ("sent", "sent")


def test_real_exit22(real: Callable[..., Outcome]) -> None:
    out = real(disabled=True)
    assert (out.outcome, out.notifier_outcome) == ("disabled", "disabled")


def test_real_exit12_codes(real: Callable[..., Outcome]) -> None:
    out = real(error={"code": "network_error"})
    assert shape(out) == ("queued_retry", "network_error", None)
    out = real(error={"code": "auth_required"})
    assert shape(out) == ("failed", "auth_required", None)


def test_real_exit20_link(real: Callable[..., Outcome]) -> None:
    out = real(error={"code": "refused", "reason": "link_scheme"})
    assert shape(out) == ("queued_retry", "link_scheme", "link_refused")


def test_real_exit1_and_deployment_exits(real: Callable[..., Outcome]) -> None:
    assert shape(real(error={"code": "internal"})) == ("failed", "deployment_fault", None)
    assert shape(real(exit=2, no_output=True)) == ("failed", "deployment_fault", None)
    assert shape(real(exit=127, no_output=True)) == ("failed", "deployment_fault", None)


def test_real_signal_exit_143_empty_stdout(real: Callable[..., Outcome]) -> None:
    assert shape(real(exit=143, no_output=True)) == ("queued_retry", "signal", None)


def test_real_death_by_signal(real: Callable[..., Outcome]) -> None:
    out = real(die_by_signal="KILL", no_output=True)
    assert shape(out) == ("unknown", "signal", None)


def test_real_stdout_overflow(real: Callable[..., Outcome]) -> None:
    out = real(stdout_bytes=STDOUT_LIMIT + 100, exit=0)
    assert shape(out) == ("unknown", "output_overflow", None)


def test_real_envelope_garbage_on_exit_12(real: Callable[..., Outcome]) -> None:
    out = real(exit=12, stdout="not json")
    assert shape(out) == ("unknown", "envelope_invalid", None)


def test_real_exec_failed(tmp_path: Path) -> None:
    base = tmp_path / "bin"
    base.mkdir()
    base.chmod(0o755)
    gateway = Gateway(environ=lambda: {"PATH": str(base)}, exec_check_from=str(base))
    call = call_notifier(
        gateway,
        dest(kind="slack-notice", cmd=(str(base / "missing"),)),
        title="",
        message="m",
        level="info",
        link="",
        dedupe_key=None,
    )
    assert shape(classify(call, kind="slack-notice", has_dedupe_key=False)) == (
        "failed",
        "exec_failed",
        None,
    )
