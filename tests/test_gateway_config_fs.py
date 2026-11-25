"""Gateway helpers for reading config: existing-directory open, checked file read, listing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from notice_route import gateway
from notice_route.gateway import Gateway, LimitExceededError, UnsafePathError


@pytest.fixture
def gw() -> Gateway:
    return Gateway()


def _dir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    return path


def _file(path: Path, data: bytes = b"x: 1\n", mode: int = 0o600) -> Path:
    path.write_bytes(data)
    path.chmod(mode)
    return path


def _as_other_user(monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.geteuid()
    monkeypatch.setattr(gateway.os, "geteuid", lambda: real + 1)


# --- open_config_dir -----------------------------------------------------------------------


def test_missing_config_dir_is_none_and_is_not_created(gw: Gateway, tmp_path: Path) -> None:
    assert gw.open_config_dir(str(tmp_path / "nope")) is None
    assert not (tmp_path / "nope").exists()


def test_missing_parent_is_none(gw: Gateway, tmp_path: Path) -> None:
    assert gw.open_config_dir(str(tmp_path / "a" / "b" / "c")) is None
    assert not (tmp_path / "a").exists()


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o755, 0o500])
def test_dir_readable_by_others_is_accepted(gw: Gateway, tmp_path: Path, mode: int) -> None:
    target = _dir(tmp_path / "cfg", mode)
    handle = gw.open_config_dir(str(target))
    assert handle is not None
    handle.close()
    target.chmod(0o700)


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777, 0o720, 0o702])
def test_dir_writable_by_group_or_world_is_refused(gw: Gateway, tmp_path: Path, mode: int) -> None:
    target = _dir(tmp_path / "cfg", mode)
    with pytest.raises(UnsafePathError) as info:
        gw.open_config_dir(str(target))
    assert info.value.reason == "mode"


def test_dir_not_owned_by_the_user_is_refused(
    gw: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _dir(tmp_path / "cfg")
    _as_other_user(monkeypatch)
    with pytest.raises(UnsafePathError) as info:
        gw.open_config_dir(str(target))
    assert info.value.reason == "owner"


def test_symlink_to_a_dir_is_refused_not_followed(gw: Gateway, tmp_path: Path) -> None:
    real = _dir(tmp_path / "real")
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(UnsafePathError) as info:
        gw.open_config_dir(str(tmp_path / "link"))
    assert info.value.reason == "not_directory"


def test_dangling_symlink_is_refused(gw: Gateway, tmp_path: Path) -> None:
    (tmp_path / "link").symlink_to(tmp_path / "gone")
    with pytest.raises(UnsafePathError) as info:
        gw.open_config_dir(str(tmp_path / "link"))
    assert info.value.reason == "not_directory"


def test_regular_file_where_a_dir_belongs_is_refused(gw: Gateway, tmp_path: Path) -> None:
    _file(tmp_path / "cfg")
    with pytest.raises(UnsafePathError) as info:
        gw.open_config_dir(str(tmp_path / "cfg"))
    assert info.value.reason == "not_directory"


def test_subdirectory_is_opened_relative_to_its_parent(gw: Gateway, tmp_path: Path) -> None:
    _dir(tmp_path / "cfg")
    _dir(tmp_path / "cfg" / "config.d")
    parent = gw.open_config_dir(str(tmp_path / "cfg"))
    assert parent is not None
    with parent:
        child = gw.open_config_dir("config.d", parent=parent)
        assert child is not None
        assert child.path == f"{tmp_path}/cfg/config.d"
        child.close()
        assert gw.open_config_dir("absent", parent=parent) is None


def test_subdirectory_name_must_be_one_component(gw: Gateway, tmp_path: Path) -> None:
    parent = gw.open_config_dir(str(_dir(tmp_path / "cfg")))
    assert parent is not None
    with parent:
        for bad in ("", ".", "..", "a/b", "a\x00b"):
            with pytest.raises(ValueError):
                gw.open_config_dir(bad, parent=parent)


def test_nul_in_a_path_is_a_value_error(gw: Gateway) -> None:
    with pytest.raises(ValueError):
        gw.open_config_dir("/tmp/a\x00b")


def test_relative_dir_path_is_resolved_against_the_cwd(
    gw: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dir(tmp_path / "cfg")
    monkeypatch.chdir(tmp_path)
    handle = gw.open_config_dir("cfg")
    assert handle is not None
    assert handle.path == "cfg"
    handle.close()


def test_descriptor_is_closed_on_refusal(gw: Gateway, tmp_path: Path) -> None:
    target = _dir(tmp_path / "cfg", 0o777)
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(5):
        with pytest.raises(UnsafePathError):
            gw.open_config_dir(str(target))
    assert len(os.listdir("/proc/self/fd")) == before


# --- read_config_file ----------------------------------------------------------------------


def _handle(gw: Gateway, path: Path) -> gateway.DirHandle:
    handle = gw.open_config_dir(str(path))
    assert handle is not None
    return handle


def test_reads_a_file(gw: Gateway, tmp_path: Path) -> None:
    _file(_dir(tmp_path / "cfg") / "config.yaml", b"a: 1\n")
    with _handle(gw, tmp_path / "cfg") as parent:
        assert gw.read_config_file(parent, "config.yaml", 100) == b"a: 1\n"


def test_missing_file_is_none(gw: Gateway, tmp_path: Path) -> None:
    with _handle(gw, _dir(tmp_path / "cfg")) as parent:
        assert gw.read_config_file(parent, "config.yaml", 100) is None


def test_limit_applies_to_the_bytes_read(gw: Gateway, tmp_path: Path) -> None:
    cfg = _dir(tmp_path / "cfg")
    _file(cfg / "exact.yaml", b"a" * 100)
    _file(cfg / "over.yaml", b"a" * 101)
    _file(cfg / "huge.yaml", b"a" * (5 * 1024 * 1024))
    with _handle(gw, cfg) as parent:
        assert gw.read_config_file(parent, "exact.yaml", 100) == b"a" * 100
        with pytest.raises(LimitExceededError):
            gw.read_config_file(parent, "over.yaml", 100)
        with pytest.raises(LimitExceededError):
            gw.read_config_file(parent, "huge.yaml", 100)


def test_empty_file_is_empty_bytes(gw: Gateway, tmp_path: Path) -> None:
    _file(_dir(tmp_path / "cfg") / "config.yaml", b"")
    with _handle(gw, tmp_path / "cfg") as parent:
        assert gw.read_config_file(parent, "config.yaml", 100) == b""


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o660, 0o666, 0o700 | 0o020])
def test_file_writable_by_group_or_world_is_refused(gw: Gateway, tmp_path: Path, mode: int) -> None:
    _file(_dir(tmp_path / "cfg") / "config.yaml", mode=mode)
    with _handle(gw, tmp_path / "cfg") as parent, pytest.raises(UnsafePathError) as info:
        gw.read_config_file(parent, "config.yaml", 100)
    assert info.value.reason == "mode"


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o400])
def test_file_readable_by_others_is_accepted(gw: Gateway, tmp_path: Path, mode: int) -> None:
    _file(_dir(tmp_path / "cfg") / "config.yaml", mode=mode)
    with _handle(gw, tmp_path / "cfg") as parent:
        assert gw.read_config_file(parent, "config.yaml", 100) == b"x: 1\n"


def test_file_not_owned_by_the_user_is_refused(
    gw: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _file(_dir(tmp_path / "cfg") / "config.yaml")
    with _handle(gw, tmp_path / "cfg") as parent:
        _as_other_user(monkeypatch)
        with pytest.raises(UnsafePathError) as info:
            gw.read_config_file(parent, "config.yaml", 100)
    assert info.value.reason == "owner"


def test_symlink_file_is_refused_even_to_a_good_file(gw: Gateway, tmp_path: Path) -> None:
    cfg = _dir(tmp_path / "cfg")
    _file(tmp_path / "real.yaml")
    (cfg / "config.yaml").symlink_to(tmp_path / "real.yaml")
    with _handle(gw, cfg) as parent, pytest.raises(UnsafePathError) as info:
        gw.read_config_file(parent, "config.yaml", 100)
    assert info.value.reason == "symlink"


def test_dangling_symlink_file_is_refused(gw: Gateway, tmp_path: Path) -> None:
    cfg = _dir(tmp_path / "cfg")
    (cfg / "config.yaml").symlink_to(tmp_path / "gone.yaml")
    with _handle(gw, cfg) as parent, pytest.raises(UnsafePathError) as info:
        gw.read_config_file(parent, "config.yaml", 100)
    assert info.value.reason == "symlink"


def test_fifo_is_refused_without_blocking(gw: Gateway, tmp_path: Path) -> None:
    cfg = _dir(tmp_path / "cfg")
    os.mkfifo(cfg / "config.yaml", 0o600)
    with _handle(gw, cfg) as parent, pytest.raises(UnsafePathError) as info:
        gw.read_config_file(parent, "config.yaml", 100)
    assert info.value.reason == "not_regular"


def test_directory_named_like_a_file_is_refused(gw: Gateway, tmp_path: Path) -> None:
    cfg = _dir(tmp_path / "cfg")
    _dir(cfg / "x.yaml")
    with _handle(gw, cfg) as parent, pytest.raises(UnsafePathError) as info:
        gw.read_config_file(parent, "x.yaml", 100)
    assert info.value.reason == "not_regular"


def test_file_descriptors_are_not_leaked(gw: Gateway, tmp_path: Path) -> None:
    cfg = _dir(tmp_path / "cfg")
    _file(cfg / "bad.yaml", mode=0o666)
    _file(cfg / "big.yaml", b"a" * 200)
    _file(cfg / "ok.yaml")
    with _handle(gw, cfg) as parent:
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            with pytest.raises(UnsafePathError):
                gw.read_config_file(parent, "bad.yaml", 100)
            with pytest.raises(LimitExceededError):
                gw.read_config_file(parent, "big.yaml", 100)
            gw.read_config_file(parent, "ok.yaml", 100)
        assert len(os.listdir("/proc/self/fd")) == before


def test_bad_name_and_limit_are_value_errors(gw: Gateway, tmp_path: Path) -> None:
    with _handle(gw, _dir(tmp_path / "cfg")) as parent:
        with pytest.raises(ValueError):
            gw.read_config_file(parent, "a/b", 100)
        with pytest.raises(ValueError):
            gw.read_config_file(parent, "x.yaml", -1)


# --- list_dir ------------------------------------------------------------------------------


def test_list_dir_names_every_entry_type(gw: Gateway, tmp_path: Path) -> None:
    cfg = _dir(tmp_path / "cfg")
    _file(cfg / "b.yaml")
    _file(cfg / "a.yaml")
    (cfg / "l.yaml").symlink_to(tmp_path)
    _dir(cfg / "d.yaml")
    with _handle(gw, cfg) as parent:
        assert sorted(gw.list_dir(parent, limit=10)) == ["a.yaml", "b.yaml", "d.yaml", "l.yaml"]


def test_list_dir_over_the_limit_is_refused(gw: Gateway, tmp_path: Path) -> None:
    cfg = _dir(tmp_path / "cfg")
    for index in range(5):
        _file(cfg / f"{index}.yaml")
    with _handle(gw, cfg) as parent:
        assert len(gw.list_dir(parent, limit=5)) == 5
        with pytest.raises(LimitExceededError):
            gw.list_dir(parent, limit=4)


def test_list_dir_twice_gives_the_same_names(gw: Gateway, tmp_path: Path) -> None:
    cfg = _dir(tmp_path / "cfg")
    _file(cfg / "a.yaml")
    with _handle(gw, cfg) as parent:
        assert gw.list_dir(parent, limit=10) == gw.list_dir(parent, limit=10) == ["a.yaml"]
