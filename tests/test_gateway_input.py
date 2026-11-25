"""Gateway helpers for input: stdin with a deadline, ``--file`` that follows symlinks, tty, env."""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import pytest

from notice_route.gateway import FakeClock, Gateway, InputFileError

MAX = 1024


def _pipe() -> tuple[int, int]:
    return os.pipe()


def _close(*fds: int) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


# --- stdin ----------------------------------------------------------------------------------


def test_stdin_reads_to_eof() -> None:
    r, w = _pipe()
    try:
        os.write(w, b'{"a": 1}')
        os.close(w)
        gw = Gateway(stdin_fd=r)
        result = gw.read_stdin(MAX, gw.monotonic() + 5)
        assert (result.status, result.data) == ("ok", b'{"a": 1}')
    finally:
        _close(r, w)


def test_stdin_empty_is_ok_and_empty() -> None:
    r, w = _pipe()
    os.close(w)
    try:
        gw = Gateway(stdin_fd=r)
        result = gw.read_stdin(MAX, gw.monotonic() + 5)
        assert (result.status, result.data) == ("ok", b"")
    finally:
        _close(r)


def test_stdin_exactly_the_limit_is_ok_and_one_more_is_too_large() -> None:
    for size, status in ((MAX, "ok"), (MAX + 1, "too_large")):
        r, w = _pipe()
        os.set_blocking(w, False)
        try:
            # Fill from a thread: a pipe holds 64 KiB, the limit here is smaller.
            os.write(w, b"x" * size)
            os.close(w)
            gw = Gateway(stdin_fd=r)
            result = gw.read_stdin(MAX, gw.monotonic() + 5)
            assert result.status == status
            assert len(result.data) <= MAX
        finally:
            _close(r, w)


def test_stdin_huge_input_stops_after_the_limit_without_waiting_for_the_writer() -> None:
    r, w = _pipe()
    stop = threading.Event()

    def feed() -> None:
        chunk = b"x" * 65536
        try:
            while not stop.is_set():
                os.write(w, chunk)
        except OSError:
            pass  # the reader closed its end

    thread = threading.Thread(target=feed, daemon=True)
    thread.start()
    try:
        gw = Gateway(stdin_fd=r)
        result = gw.read_stdin(1_048_576, gw.monotonic() + 5)
        assert result.status == "too_large"
        assert len(result.data) == 1_048_576
    finally:
        stop.set()
        _close(r)
        thread.join(timeout=5)
        _close(w)


def test_stdin_stalled_writer_times_out_with_partial_data() -> None:
    r, w = _pipe()
    try:
        os.write(w, b'{"a"')  # never closed, never finished
        gw = Gateway(stdin_fd=r)
        result = gw.read_stdin(MAX, gw.monotonic() + 0.25)
        assert result.status == "timeout"
    finally:
        _close(r, w)


def test_stdin_deadline_already_passed_is_timeout_even_with_data_ready() -> None:
    r, w = _pipe()
    try:
        os.write(w, b"{}")
        os.close(w)
        clock = FakeClock(monotonic=100.0)
        gw = Gateway(clock=clock, stdin_fd=r)
        assert gw.read_stdin(MAX, 99.0).status == "timeout"
    finally:
        _close(r, w)


def test_stdin_data_that_completes_before_the_deadline_is_ok() -> None:
    r, w = _pipe()

    def late() -> None:
        threading.Event().wait(0.15)
        os.write(w, b"{}")
        os.close(w)

    thread = threading.Thread(target=late)
    thread.start()
    try:
        gw = Gateway(stdin_fd=r)
        result = gw.read_stdin(MAX, gw.monotonic() + 5)
        assert (result.status, result.data) == ("ok", b"{}")
    finally:
        thread.join(timeout=5)
        _close(r, w)


def test_closed_stdin_descriptor_is_unreadable_not_an_exception() -> None:
    r, w = _pipe()
    _close(r, w)
    gw = Gateway(stdin_fd=r)
    result = gw.read_stdin(MAX, gw.monotonic() + 5)
    assert (result.status, result.data) == ("unreadable", b"")


def test_stdin_that_is_a_directory_is_unreadable(tmp_path: Path) -> None:
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        gw = Gateway(stdin_fd=fd)
        assert gw.read_stdin(MAX, gw.monotonic() + 5).status == "unreadable"
    finally:
        _close(fd)


def test_stdin_that_is_write_only_is_unreadable(tmp_path: Path) -> None:
    fd = os.open(tmp_path / "w", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        gw = Gateway(stdin_fd=fd)
        assert gw.read_stdin(MAX, gw.monotonic() + 5).status == "unreadable"
    finally:
        _close(fd)


def test_stdin_that_is_a_regular_file_reads(tmp_path: Path) -> None:
    path = tmp_path / "in"
    path.write_bytes(b"{}")
    fd = os.open(path, os.O_RDONLY)
    try:
        gw = Gateway(stdin_fd=fd)
        result = gw.read_stdin(MAX, gw.monotonic() + 5)
        assert (result.status, result.data) == ("ok", b"{}")
    finally:
        _close(fd)


def test_stdin_that_is_a_socket_reads_to_eof() -> None:
    a, b = socket.socketpair()
    try:
        b.sendall(b"{}")
        b.close()
        gw = Gateway(stdin_fd=a.fileno())
        result = gw.read_stdin(MAX, gw.monotonic() + 5)
        assert (result.status, result.data) == ("ok", b"{}")
    finally:
        a.close()


def test_stdin_non_blocking_pipe_is_waited_for() -> None:
    r, w = _pipe()
    os.set_blocking(r, False)
    try:
        os.close(w)
        gw = Gateway(stdin_fd=r)
        assert gw.read_stdin(MAX, gw.monotonic() + 5).status == "ok"
    finally:
        _close(r, w)


def test_stdin_is_tty_false_for_a_pipe_and_a_closed_descriptor() -> None:
    r, w = _pipe()
    try:
        assert Gateway(stdin_fd=r).stdin_is_tty() is False
    finally:
        _close(r, w)
    assert Gateway(stdin_fd=r).stdin_is_tty() is False


def test_stdin_is_tty_true_for_a_pty() -> None:
    master, slave = os.openpty()
    try:
        assert Gateway(stdin_fd=slave).stdin_is_tty() is True
    finally:
        _close(master, slave)


# --- --file ---------------------------------------------------------------------------------


def test_file_reads_a_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "in.json"
    path.write_bytes(b'{"a": 1}')
    result = Gateway().read_input_file(str(path), MAX)
    assert (result.status, result.data) == ("ok", b'{"a": 1}')


def test_file_follows_a_symlink_to_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_bytes(b"{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    assert Gateway().read_input_file(str(link), MAX).data == b"{}"


def test_file_follows_a_symlink_chain_and_a_symlinked_directory(tmp_path: Path) -> None:
    real = tmp_path / "d"
    real.mkdir()
    (real / "in.json").write_bytes(b"{}")
    (tmp_path / "dl").symlink_to(real)
    (tmp_path / "l1").symlink_to(tmp_path / "dl" / "in.json")
    (tmp_path / "l2").symlink_to(tmp_path / "l1")
    assert Gateway().read_input_file(str(tmp_path / "l2"), MAX).data == b"{}"


def test_file_symlink_to_a_directory_is_not_regular(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "l").symlink_to(tmp_path / "d")
    with pytest.raises(InputFileError) as info:
        Gateway().read_input_file(str(tmp_path / "l"), MAX)
    assert info.value.reason == "not_regular"


def test_file_dangling_symlink_and_loop_and_missing_are_unreadable(tmp_path: Path) -> None:
    (tmp_path / "dangling").symlink_to(tmp_path / "nope")
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    for name in ("dangling", "a", "missing"):
        with pytest.raises(InputFileError) as info:
            Gateway().read_input_file(str(tmp_path / name), MAX)
        assert info.value.reason == "unreadable"


def test_file_directory_is_not_regular(tmp_path: Path) -> None:
    with pytest.raises(InputFileError) as info:
        Gateway().read_input_file(str(tmp_path), MAX)
    assert info.value.reason == "not_regular"


def test_file_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)  # no writer: a blocking open would hang
    with pytest.raises(InputFileError) as info:
        Gateway().read_input_file(str(fifo), MAX)
    assert info.value.reason == "not_regular"


@pytest.mark.parametrize("device", ["/dev/null", "/dev/zero", "/dev/urandom"])
def test_file_device_is_refused_before_reading(device: str) -> None:
    if not os.path.exists(device):
        pytest.skip("no such device")
    with pytest.raises(InputFileError) as info:
        Gateway().read_input_file(device, MAX)
    assert info.value.reason == "not_regular"


def test_file_socket_is_refused(tmp_path: Path) -> None:
    path = str(tmp_path / "s")
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(path)
        with pytest.raises(InputFileError) as info:
            Gateway().read_input_file(path, MAX)
        assert info.value.reason in {"not_regular", "unreadable"}
    finally:
        server.close()


def test_file_unreadable_mode_is_unreadable(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads anything")
    path = tmp_path / "locked"
    path.write_bytes(b"{}")
    path.chmod(0)
    with pytest.raises(InputFileError) as info:
        Gateway().read_input_file(str(path), MAX)
    assert info.value.reason == "unreadable"


@pytest.mark.parametrize("path", ["", "a\0b", "\udc80bad"])
def test_file_hostile_paths_are_unreadable_and_never_echoed(path: str) -> None:
    with pytest.raises(InputFileError) as info:
        Gateway().read_input_file(path, MAX)
    assert info.value.reason == "unreadable"
    assert "bad" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_file_limit_boundary(tmp_path: Path) -> None:
    path = tmp_path / "in"
    path.write_bytes(b"x" * MAX)
    assert Gateway().read_input_file(str(path), MAX).status == "ok"
    path.write_bytes(b"x" * (MAX + 1))
    result = Gateway().read_input_file(str(path), MAX)
    assert result.status == "too_large"
    assert len(result.data) == MAX


def test_file_empty_is_ok_and_empty(tmp_path: Path) -> None:
    path = tmp_path / "in"
    path.write_bytes(b"")
    result = Gateway().read_input_file(str(path), MAX)
    assert (result.status, result.data) == ("ok", b"")


def test_file_has_no_deadline(tmp_path: Path) -> None:
    path = tmp_path / "in"
    path.write_bytes(b"{}")
    clock = FakeClock(monotonic=10_000.0)
    assert Gateway(clock=clock).read_input_file(str(path), MAX).status == "ok"


def test_file_descriptor_is_closed_after_a_read(tmp_path: Path) -> None:
    path = tmp_path / "in"
    path.write_bytes(b"{}")
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(20):
        Gateway().read_input_file(str(path), MAX)
        with pytest.raises(InputFileError):
            Gateway().read_input_file(str(tmp_path), MAX)
    assert len(os.listdir("/proc/self/fd")) <= before + 1


# --- environment ----------------------------------------------------------------------------


def test_env_present_uses_the_injected_environment() -> None:
    gw = Gateway(environ=lambda: {"NOTICE_ROUTE_HOOK": "", "OTHER": "x"})
    assert gw.env_present("NOTICE_ROUTE_HOOK") is True
    assert gw.env_present("MISSING") is False


def test_env_present_defaults_to_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTICE_ROUTE_TEST_PRESENT", "")
    monkeypatch.delenv("NOTICE_ROUTE_TEST_ABSENT", raising=False)
    gw = Gateway()
    assert gw.env_present("NOTICE_ROUTE_TEST_PRESENT") is True
    assert gw.env_present("NOTICE_ROUTE_TEST_ABSENT") is False
