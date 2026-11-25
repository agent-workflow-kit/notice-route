"""Flag combinations, the refusal before reading, and reading through the gateway (send 1-3)."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from notice_route import payload
from notice_route.gateway import FakeClock, Gateway
from notice_route.payload import InputFlags, PayloadError, UsageError, load_notification

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"
HOOK = b'{"tool": "agent-spawn", "hook": "post-merge", "event_id": 17}'
NOTICE = b'{"message": "hello", "level": "warn"}'


def _validator() -> Draft202012Validator:
    registry: Registry[Any] = Registry()
    for path in SCHEMAS.rglob("*.json"):
        doc = json.loads(path.read_text())
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
    schema = json.loads((SCHEMAS / "cli" / "error.json").read_text())
    return Draft202012Validator(schema, registry=registry)


class PipeStdin:
    """A pipe standing in for stdin; closes every descriptor at the end."""

    def __init__(self, data: bytes | None) -> None:
        self.r, self.w = os.pipe()
        if data is not None:
            os.write(self.w, data)
            os.close(self.w)
            self.w = -1

    def gateway(self, **kwargs: Any) -> Gateway:
        return Gateway(stdin_fd=self.r, environ=lambda: {}, **kwargs)

    def close(self) -> None:
        for fd in (self.r, self.w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


@pytest.fixture
def stdin() -> Iterator[type[PipeStdin]]:
    made: list[PipeStdin] = []

    class Tracked(PipeStdin):
        def __init__(self, data: bytes | None) -> None:
            super().__init__(data)
            made.append(self)

    yield Tracked
    for item in made:
        item.close()


def started(gw: Gateway) -> float:
    return gw.monotonic()


def refused_with(
    flags: InputFlags, gw: Gateway, at: float | None = None
) -> PayloadError | UsageError:
    with pytest.raises((PayloadError, UsageError)) as info:
        load_notification(gw, flags, gw.monotonic() if at is None else at)
    return info.value


# --- flag combinations (exit 2) -------------------------------------------------------------


@pytest.mark.parametrize(
    "flags",
    [
        InputFlags(stdin_json=True, file="x.json"),
        InputFlags(stdin_json=True, file="-"),
        InputFlags(stdin_json=True, title="t"),
        InputFlags(stdin_json=True, message="m"),
        InputFlags(stdin_json=True, title="t", message="m"),
        InputFlags(file="x.json", title="t"),
        InputFlags(file="x.json", message="m"),
        InputFlags(file="-", message="m"),
        InputFlags(title="t"),
        InputFlags(title=""),
        InputFlags(),
        InputFlags(source="direct"),
        InputFlags(labels=("a=1",)),
        InputFlags(message="m", labels=tuple(f"a=v{i}" for i in range(33))),
        InputFlags(stdin_json=True, labels=tuple(f"a=v{i}" for i in range(33))),
    ],
)
def test_flag_combinations_that_are_exit_2(flags: InputFlags) -> None:
    gw = Gateway(environ=lambda: {})
    error = refused_with(flags, gw)
    assert isinstance(error, UsageError)
    assert error.exit_code == 2
    assert error.to_error()["error"]["code"] == "usage"


def test_usage_errors_come_before_hook_recursion_and_before_reading() -> None:
    class Boom(Gateway):
        def read_stdin(self, limit: int, deadline: float) -> Any:
            raise AssertionError("read")

    gw = Boom(environ=lambda: {"NOTICE_ROUTE_HOOK": "1"})
    assert isinstance(refused_with(InputFlags(title="t"), gw), UsageError)


def test_thirty_two_labels_are_allowed() -> None:
    gw = Gateway(environ=lambda: {})
    flags = InputFlags(message="m", labels=tuple(f"a=v{i}" for i in range(32)))
    assert len(load_notification(gw, flags, gw.monotonic()).labels) == 32


def test_message_alone_title_and_message_and_empty_message_are_valid_combinations() -> None:
    gw = Gateway(environ=lambda: {})
    assert load_notification(gw, InputFlags(message="m"), 0.0).message == "m"
    note = load_notification(gw, InputFlags(title="t", message="m", source="s"), 0.0)
    assert (note.title, note.source) == ("t", "s")
    # An empty --message is given, so the combination is fine; the field rule refuses it.
    error = refused_with(InputFlags(message=""), gw)
    assert isinstance(error, PayloadError) and error.pointer == "/message"


def test_terminal_on_stdin_is_exit_2_for_stdin_json_and_file_dash() -> None:
    master, slave = os.openpty()
    try:
        gw = Gateway(stdin_fd=slave, environ=lambda: {})
        for flags in (InputFlags(stdin_json=True), InputFlags(file="-")):
            error = refused_with(flags, gw)
            assert isinstance(error, UsageError) and error.exit_code == 2
    finally:
        os.close(master)
        os.close(slave)


def test_terminal_on_stdin_is_fine_for_flags_and_for_a_file(tmp_path: Path) -> None:
    path = tmp_path / "in.json"
    path.write_bytes(NOTICE)
    master, slave = os.openpty()
    try:
        gw = Gateway(stdin_fd=slave, environ=lambda: {})
        assert load_notification(gw, InputFlags(message="m"), 0.0).message == "m"
        assert load_notification(gw, InputFlags(file=str(path)), 0.0).message == "hello"
    finally:
        os.close(master)
        os.close(slave)


def test_usage_error_messages_are_fixed_and_carry_no_flag_value() -> None:
    gw = Gateway(environ=lambda: {})
    marker = "marker_xyz"
    error = refused_with(InputFlags(stdin_json=True, file=marker, title=marker), gw)
    assert marker not in str(error) and marker not in repr(error.to_error())


# --- hook_recursion -------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "", "post-fail"])
def test_hook_recursion_is_refused_before_anything_is_read(value: str) -> None:
    calls: list[str] = []

    class Spy(Gateway):
        def read_stdin(self, limit: int, deadline: float) -> Any:
            calls.append("stdin")
            raise AssertionError

        def read_input_file(self, path: str, limit: int) -> Any:
            calls.append("file")
            raise AssertionError

    gw = Spy(environ=lambda: {"NOTICE_ROUTE_HOOK": value})
    for flags in (InputFlags(stdin_json=True), InputFlags(file="-"), InputFlags(file="/x")):
        error = refused_with(flags, gw)
        assert isinstance(error, PayloadError)
        assert (error.reason, error.pointer, error.exit_code) == ("hook_recursion", None, 20)
    assert calls == []


def test_hook_recursion_applies_to_flag_notices_too() -> None:
    gw = Gateway(environ=lambda: {"NOTICE_ROUTE_HOOK": "1"})
    error = refused_with(InputFlags(message="m"), gw)
    assert isinstance(error, PayloadError) and error.reason == "hook_recursion"


def test_other_variables_do_not_trigger_the_guard() -> None:
    gw = Gateway(environ=lambda: {"NOTICE_ROUTE_ID": "1", "NOTICE_ROUTE_HOOK_X": "1"})
    assert load_notification(gw, InputFlags(message="m"), 0.0).message == "m"


# --- reading stdin --------------------------------------------------------------------------


def test_stdin_json_hook_payload(stdin: type[PipeStdin]) -> None:
    gw = stdin(HOOK).gateway()
    note = load_notification(gw, InputFlags(stdin_json=True), gw.monotonic())
    assert (note.shape, note.source, note.event_id) == ("hook", "agent-spawn", 17)


def test_file_dash_is_stdin(stdin: type[PipeStdin]) -> None:
    gw = stdin(NOTICE).gateway()
    note = load_notification(gw, InputFlags(file="-"), gw.monotonic())
    assert (note.shape, note.level) == ("notice", "warn")


def test_stdin_json_with_source_and_labels(stdin: type[PipeStdin]) -> None:
    gw = stdin(NOTICE).gateway()
    flags = InputFlags(stdin_json=True, source="my-script", labels=("a=1",))
    note = load_notification(gw, flags, gw.monotonic())
    assert (note.source, note.labels) == ("my-script", (("a", "1"),))


def test_stdin_deadline_is_five_seconds_from_the_start_of_the_command() -> None:
    assert payload.INPUT_DEADLINE_S == 5.0


def test_stalled_stdin_is_payload_invalid_at_the_whole_input(
    stdin: type[PipeStdin], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(payload, "INPUT_DEADLINE_S", 0.3)
    gw = stdin(None).gateway()  # a writer that never writes and never closes
    error = refused_with(InputFlags(stdin_json=True), gw)
    assert isinstance(error, PayloadError)
    assert (error.reason, error.pointer, error.exit_code) == ("payload_invalid", "", 20)


def test_partial_input_then_a_stall_is_payload_invalid(
    stdin: type[PipeStdin], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(payload, "INPUT_DEADLINE_S", 0.3)
    pipe = stdin(None)
    os.write(pipe.w, b'{"message": "he')
    error = refused_with(InputFlags(stdin_json=True), pipe.gateway())
    assert isinstance(error, PayloadError) and error.pointer == ""


def test_the_deadline_counts_from_command_start_not_from_the_read(stdin: type[PipeStdin]) -> None:
    clock = FakeClock(monotonic=100.0)
    gw = stdin(NOTICE).gateway(clock=clock)
    clock.advance_monotonic(6.0)  # the command started 6 s ago (slow config load, say)
    error = refused_with(InputFlags(stdin_json=True), gw, at=100.0)
    assert isinstance(error, PayloadError) and error.pointer == ""
    # The same input within the 5 seconds is read.
    clock2 = FakeClock(monotonic=100.0)
    gw2 = stdin(NOTICE).gateway(clock=clock2)
    clock2.advance_monotonic(4.9)
    assert load_notification(gw2, InputFlags(stdin_json=True), 100.0).message == "hello"


def test_slow_but_timely_stdin_is_read(stdin: type[PipeStdin]) -> None:
    pipe = stdin(None)

    def late() -> None:
        threading.Event().wait(0.2)
        os.write(pipe.w, NOTICE)
        os.close(pipe.w)
        pipe.w = -1

    thread = threading.Thread(target=late)
    thread.start()
    gw = pipe.gateway()
    note = load_notification(gw, InputFlags(stdin_json=True), gw.monotonic())
    thread.join(timeout=5)
    assert note.message == "hello"


def test_closed_stdin_is_payload_invalid() -> None:
    r, w = os.pipe()
    os.close(r)
    os.close(w)
    gw = Gateway(stdin_fd=r, environ=lambda: {})
    error = refused_with(InputFlags(stdin_json=True), gw)
    assert isinstance(error, PayloadError) and (error.reason, error.pointer) == (
        "payload_invalid",
        "",
    )


def test_empty_stdin_is_payload_invalid(stdin: type[PipeStdin]) -> None:
    error = refused_with(InputFlags(stdin_json=True), stdin(b"").gateway())
    assert isinstance(error, PayloadError) and error.pointer == ""


def test_huge_stdin_is_payload_invalid_without_reading_it_all(stdin: type[PipeStdin]) -> None:
    pipe = stdin(None)
    stop = threading.Event()

    def feed() -> None:
        try:
            while not stop.is_set():
                os.write(pipe.w, b"x" * 65536)
        except OSError:
            pass

    thread = threading.Thread(target=feed, daemon=True)
    thread.start()
    try:
        error = refused_with(InputFlags(stdin_json=True), pipe.gateway())
    finally:
        stop.set()
        os.close(pipe.r)
        thread.join(timeout=5)
        pipe.r = -1
    assert isinstance(error, PayloadError) and (error.reason, error.pointer) == (
        "payload_invalid",
        "",
    )


@pytest.mark.parametrize(
    "data",
    [
        b'{"a": 1, "a": 2}',
        b'{"message": NaN}',
        b'{"message": 1e999}',
        b'{"message": 9223372036854775808}',
        b'{"a":' * 17 + b"{}" + b"}" * 17,
        b"[]",
        b"\xef\xbb\xbf" + NOTICE,
        b'{"message": "\xff"}',
        NOTICE + b" trailing",
        b"x" * 1048577,
    ],
)
def test_every_reader_failure_is_payload_invalid_at_the_whole_input(
    stdin: type[PipeStdin], data: bytes, tmp_path: Path
) -> None:
    path = tmp_path / "in.json"
    path.write_bytes(data)
    gw = Gateway(environ=lambda: {})
    for flags in (InputFlags(file=str(path)),):
        error = refused_with(flags, gw)
        assert isinstance(error, PayloadError)
        assert (error.reason, error.pointer, error.exit_code) == ("payload_invalid", "", 20)
    if len(data) <= 65536:
        error = refused_with(InputFlags(stdin_json=True), stdin(data).gateway())
        assert isinstance(error, PayloadError) and error.pointer == ""


def test_reader_errors_carry_no_input_and_no_chain(stdin: type[PipeStdin]) -> None:
    error = refused_with(
        InputFlags(stdin_json=True), stdin(b'{"marker_xyz": 1, "marker_xyz": 2}').gateway()
    )
    assert "marker_xyz" not in str(error) and "marker_xyz" not in repr(error.to_error())
    assert error.__cause__ is None and error.__context__ is None


def test_lone_surrogate_in_the_input_is_replaced_through_the_whole_path(
    stdin: type[PipeStdin],
) -> None:
    gw = stdin(b'{"message": "a\\ud800b"}').gateway()
    assert load_notification(gw, InputFlags(stdin_json=True), gw.monotonic()).message == "a�b"


# --- reading --file -------------------------------------------------------------------------


def test_file_reads_a_regular_file_and_follows_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_bytes(HOOK)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    gw = Gateway(environ=lambda: {})
    for path in (real, link):
        note = load_notification(gw, InputFlags(file=str(path)), gw.monotonic())
        assert note.source == "agent-spawn"


def test_file_is_not_subject_to_the_stdin_deadline(tmp_path: Path) -> None:
    path = tmp_path / "in.json"
    path.write_bytes(NOTICE)
    clock = FakeClock(monotonic=1000.0)
    gw = Gateway(clock=clock, environ=lambda: {})
    assert load_notification(gw, InputFlags(file=str(path)), 0.0).message == "hello"


def test_file_that_is_not_regular_or_missing_is_exit_2(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    (tmp_path / "d").mkdir()
    gw = Gateway(environ=lambda: {})
    for target in (
        fifo,
        tmp_path / "d",
        tmp_path / "missing",
        Path("/dev/null"),
        Path("/dev/zero"),
    ):
        error = refused_with(InputFlags(file=str(target)), gw)
        assert isinstance(error, UsageError), target
        assert error.exit_code == 2
        assert str(tmp_path) not in str(error) and str(tmp_path) not in repr(error.to_error())


def test_file_error_does_not_chain_the_path(tmp_path: Path) -> None:
    error = refused_with(InputFlags(file=str(tmp_path / "marker_xyz")), Gateway(environ=lambda: {}))
    assert error.__cause__ is None and error.__context__ is None
    assert "marker_xyz" not in str(error)


# --- the error envelope ---------------------------------------------------------------------


def test_refusals_validate_against_the_error_schema() -> None:
    validator = _validator()
    gw = Gateway(environ=lambda: {})
    cases: list[PayloadError | UsageError] = [
        UsageError("flags"),
        PayloadError("payload_unrecognized"),
        PayloadError("source_mismatch"),
        PayloadError("hook_recursion"),
        PayloadError("payload_invalid", ""),
        PayloadError("payload_invalid", "/title"),
        PayloadError("payload_invalid", "--source"),
        PayloadError("payload_invalid", "--label"),
        refused_with(InputFlags(), gw),
    ]
    for case in cases:
        validator.validate(case.to_error())


def test_payload_invalid_requires_a_pointer_and_others_forbid_unknown_reasons() -> None:
    with pytest.raises(ValueError):
        PayloadError("payload_invalid")
    with pytest.raises(ValueError):
        PayloadError("bogus")
    with pytest.raises(ValueError):
        PayloadError("payload_invalid", "/not a pointer")
    with pytest.raises(ValueError):
        PayloadError("source_mismatch", "/title")
