"""Gateway filesystem helpers: private directories, safe file opens, password-database home."""

from __future__ import annotations

import os
import pwd
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from notice_route import gateway
from notice_route.gateway import Gateway, HomeError, UnsafePathError


@pytest.fixture
def gw() -> Gateway:
    return Gateway()


@pytest.fixture
def umask_022() -> Iterator[None]:
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


# --- home lookup ---------------------------------------------------------------------------


def test_home_comes_from_the_password_database_not_the_environment(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    assert gw.home_dir() == Path(pwd.getpwuid(os.geteuid()).pw_dir)


def test_home_with_no_environment_at_all(gw: Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    assert gw.home_dir().is_absolute()


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "empty"),
        ("relative/dir", "relative"),
        (".", "relative"),
        ("~", "relative"),
        ("~/x", "relative"),
        (" /home/u", "relative"),
        ("/home/u\x00x", "unresolvable"),
    ],
)
def test_bad_home_is_reported_with_its_reason(value: str, reason: str) -> None:
    gw = Gateway(passwd_home=lambda: value)
    with pytest.raises(HomeError) as info:
        gw.home_dir()
    assert info.value.reason == reason


def test_unknown_user_is_unresolvable() -> None:
    def lookup() -> str:
        raise KeyError("getpwuid(): uid not found: 4242")

    with pytest.raises(HomeError) as info:
        Gateway(passwd_home=lookup).home_dir()
    assert info.value.reason == "unresolvable"


def test_non_string_home_is_unresolvable() -> None:
    with pytest.raises(HomeError) as info:
        Gateway(passwd_home=lambda: None).home_dir()  # type: ignore[arg-type,return-value]
    assert info.value.reason == "unresolvable"


def test_good_home_is_returned_as_a_path() -> None:
    assert Gateway(passwd_home=lambda: "/home/u").home_dir() == Path("/home/u")
    assert Gateway(passwd_home=lambda: "/").home_dir() == Path("/")


# --- private directories -------------------------------------------------------------------


def test_private_dir_is_created_0700_and_parents_too(gw: Gateway, tmp_path: Path) -> None:
    target = tmp_path / ".local" / "state" / "notice-route"
    with gw.open_private_dir(target):
        pass
    assert _mode(target) == 0o700
    assert _mode(tmp_path / ".local" / "state") == 0o700


def test_private_dir_creation_ignores_a_permissive_umask(gw: Gateway, tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        with gw.open_private_dir(tmp_path / "a" / "b"):
            pass
        assert os.umask(0) == 0  # restored to the caller's value
    finally:
        os.umask(old)
    assert _mode(tmp_path / "a" / "b") == 0o700
    assert _mode(tmp_path / "a") == 0o700


def test_umask_is_restored_after_a_refusal(gw: Gateway, tmp_path: Path, umask_022: None) -> None:
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    with pytest.raises(UnsafePathError):
        gw.open_private_dir(loose)
    assert os.umask(0o022) == 0o022


def test_existing_private_dir_is_reused_untouched(gw: Gateway, tmp_path: Path) -> None:
    target = tmp_path / "state"
    target.mkdir(mode=0o700)
    (target / "keep").write_text("x")
    with gw.open_private_dir(target):
        pass
    assert (target / "keep").read_text() == "x"


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o705, 0o770, 0o707, 0o777, 0o710, 0o701, 0o702])
def test_dir_with_group_or_world_access_is_refused_and_not_repaired(
    gw: Gateway, tmp_path: Path, mode: int
) -> None:
    target = tmp_path / "state"
    target.mkdir()
    target.chmod(mode)
    with pytest.raises(UnsafePathError) as info:
        gw.open_private_dir(target)
    assert info.value.reason == "mode"
    assert _mode(target) == mode


def test_dir_owned_by_someone_else_is_refused(
    gw: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state"
    target.mkdir(mode=0o700)
    monkeypatch.setattr(gateway.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(UnsafePathError) as info:
        gw.open_private_dir(target)
    assert info.value.reason == "owner"


@pytest.mark.skipif(os.geteuid() == 0, reason="root owns the root directory")
def test_real_foreign_owned_directory_is_refused(gw: Gateway) -> None:
    with pytest.raises(UnsafePathError) as info:
        gw.open_private_dir(Path("/"))
    assert info.value.reason == "owner"


def test_regular_file_in_place_of_the_dir_is_refused(gw: Gateway, tmp_path: Path) -> None:
    target = tmp_path / "state"
    target.write_text("x")
    with pytest.raises(UnsafePathError) as info:
        gw.open_private_dir(target)
    assert info.value.reason == "not_directory"


def test_symlink_to_a_private_dir_is_refused(gw: Gateway, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "state"
    link.symlink_to(real)
    with pytest.raises(UnsafePathError) as info:
        gw.open_private_dir(link)
    assert info.value.reason == "not_directory"


def test_dangling_symlink_is_refused_not_followed(gw: Gateway, tmp_path: Path) -> None:
    link = tmp_path / "state"
    link.symlink_to(tmp_path / "nowhere")
    with pytest.raises(UnsafePathError):
        gw.open_private_dir(link)
    assert not (tmp_path / "nowhere").exists()


@pytest.mark.parametrize("bad", ["relative/dir", "", "state"])
def test_private_dir_needs_an_absolute_path(gw: Gateway, bad: str) -> None:
    with pytest.raises(ValueError):
        gw.open_private_dir(bad)


def test_private_dir_rejects_nul(gw: Gateway, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        gw.open_private_dir(str(tmp_path / "a\x00b"))


def test_refusals_do_not_leak_descriptors(gw: Gateway, tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o755)
    before = _open_fds()
    for _ in range(5):
        with pytest.raises(UnsafePathError):
            gw.open_private_dir(loose)
    assert _open_fds() == before


def test_dir_handle_closes_once_and_context_closes(gw: Gateway, tmp_path: Path) -> None:
    before = _open_fds()
    with gw.open_private_dir(tmp_path / "s") as d:
        assert _open_fds() == before + 1
        assert d.fd >= 0
    assert _open_fds() == before
    d.close()  # a second close is harmless
    assert _open_fds() == before


def test_subdir_is_created_relative_to_the_handle(gw: Gateway, tmp_path: Path) -> None:
    with gw.open_private_dir(tmp_path / "s") as state:
        with gw.open_private_subdir(state, "locks"):
            pass
    assert _mode(tmp_path / "s" / "locks") == 0o700


def test_subdir_checks_apply_to_existing_entries(gw: Gateway, tmp_path: Path) -> None:
    root = tmp_path / "s"
    with gw.open_private_dir(root) as state:
        (root / "loose").mkdir()
        (root / "loose").chmod(0o750)
        with pytest.raises(UnsafePathError) as mode:
            gw.open_private_subdir(state, "loose")
        assert mode.value.reason == "mode"
        (root / "link").symlink_to(root / "loose")
        with pytest.raises(UnsafePathError):
            gw.open_private_subdir(state, "link")
        (root / "file").write_text("x")
        with pytest.raises(UnsafePathError):
            gw.open_private_subdir(state, "file")


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "/abs", "a\x00b"])
def test_subdir_and_file_names_must_be_one_plain_component(
    gw: Gateway, tmp_path: Path, name: str
) -> None:
    with gw.open_private_dir(tmp_path / "s") as state:
        with pytest.raises(ValueError):
            gw.open_private_subdir(state, name)
        with pytest.raises(ValueError):
            gw.open_file(state, name)


def test_closed_dir_handle_is_refused(gw: Gateway, tmp_path: Path) -> None:
    state = gw.open_private_dir(tmp_path / "s")
    state.close()
    with pytest.raises(ValueError):
        gw.open_file(state, "f", create=True, mode="a")


# --- file opens ----------------------------------------------------------------------------


@pytest.fixture
def state(gw: Gateway, tmp_path: Path) -> Iterator[gateway.DirHandle]:
    with gw.open_private_dir(tmp_path / "state") as d:
        yield d


def test_regular_file_is_read(gw: Gateway, state: gateway.DirHandle, tmp_path: Path) -> None:
    (tmp_path / "state" / "f").write_bytes(b"hello")
    with gw.open_file(state, "f") as f:
        assert f.read() == b"hello"


def test_missing_file_is_file_not_found(gw: Gateway, state: gateway.DirHandle) -> None:
    with pytest.raises(FileNotFoundError):
        gw.open_file(state, "absent")


def test_create_makes_a_0600_file_and_keeps_existing_content(
    gw: Gateway, state: gateway.DirHandle, tmp_path: Path, umask_022: None
) -> None:
    with gw.open_file(state, "f", mode="a", create=True) as f:
        f.write(b"one\n")
    assert _mode(tmp_path / "state" / "f") == 0o600
    with gw.open_file(state, "f", mode="a", create=True) as f:
        f.write(b"two\n")
    assert (tmp_path / "state" / "f").read_bytes() == b"one\ntwo\n"


def test_read_write_mode(gw: Gateway, state: gateway.DirHandle, tmp_path: Path) -> None:
    with gw.open_file(state, "f", mode="rw", create=True) as f:
        f.write(b"abc")
        f.seek(0)
        assert f.read() == b"abc"


def test_symlink_to_a_regular_file_is_refused(
    gw: Gateway, state: gateway.DirHandle, tmp_path: Path
) -> None:
    (tmp_path / "secret").write_text("s")
    (tmp_path / "state" / "f").symlink_to(tmp_path / "secret")
    with pytest.raises(UnsafePathError) as info:
        gw.open_file(state, "f")
    assert info.value.reason == "symlink"
    with pytest.raises(UnsafePathError):
        gw.open_file(state, "f", mode="a", create=True)
    assert (tmp_path / "secret").read_text() == "s"


def test_dangling_symlink_is_not_created_through(
    gw: Gateway, state: gateway.DirHandle, tmp_path: Path
) -> None:
    (tmp_path / "state" / "f").symlink_to(tmp_path / "victim")
    with pytest.raises(UnsafePathError):
        gw.open_file(state, "f", mode="a", create=True)
    assert not (tmp_path / "victim").exists()


def test_fifo_is_refused_without_blocking(
    gw: Gateway, state: gateway.DirHandle, tmp_path: Path
) -> None:
    os.mkfifo(tmp_path / "state" / "f")
    before = _open_fds()
    with pytest.raises(UnsafePathError) as info:
        gw.open_file(state, "f")
    assert info.value.reason == "not_regular"
    assert _open_fds() == before


def test_fifo_for_writing_with_no_reader_fails_without_blocking(
    gw: Gateway, state: gateway.DirHandle, tmp_path: Path
) -> None:
    os.mkfifo(tmp_path / "state" / "f")
    with pytest.raises(OSError):
        gw.open_file(state, "f", mode="a")


def test_directory_in_place_of_a_file_is_refused(
    gw: Gateway, state: gateway.DirHandle, tmp_path: Path
) -> None:
    (tmp_path / "state" / "f").mkdir()
    with pytest.raises(UnsafePathError) as info:
        gw.open_file(state, "f")
    assert info.value.reason == "not_regular"


def test_socket_in_place_of_a_file_is_refused(
    gw: Gateway, state: gateway.DirHandle, tmp_path: Path
) -> None:
    import socket

    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(tmp_path / "state" / "f"))
        with pytest.raises(OSError):  # the kernel refuses to open a socket node
            gw.open_file(state, "f")
    finally:
        sock.close()


def test_unknown_mode_is_refused(gw: Gateway, state: gateway.DirHandle) -> None:
    with pytest.raises(ValueError):
        gw.open_file(state, "f", mode="w")  # type: ignore[arg-type]
