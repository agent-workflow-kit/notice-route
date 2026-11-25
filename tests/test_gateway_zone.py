"""Gateway.load_zone: time zones come only from the system database, never from the environment."""

from __future__ import annotations

import os
import struct
import zoneinfo
from pathlib import Path
from typing import Any

import pytest

from notice_route.gateway import SYSTEM_TZ_DIRS, Gateway, ZoneError

_SYSTEM = next((Path(d) for d in SYSTEM_TZ_DIRS if (Path(d) / "UTC").is_file()), None)


def _utc_bytes() -> bytes:
    """A minimal version 1 TZif file for UTC, so these tests need no system database."""
    header = b"TZif" + b"\x00" * 16 + struct.pack(">6l", 0, 0, 0, 0, 1, 4)
    return header + struct.pack(">lBB", 0, 0, 0) + b"UTC\x00"


@pytest.fixture
def zones(tmp_path: Path) -> Path:
    base = tmp_path / "zones"
    (base / "Area").mkdir(parents=True)
    return base


def test_a_zone_loads_from_a_given_directory(zones: Path) -> None:
    (zones / "Area" / "Town").write_bytes(_utc_bytes())
    zone = Gateway(tz_dirs=(str(zones),)).load_zone("Area/Town")
    assert zone.key == "Area/Town"


def test_a_symlinked_zone_file_is_followed(zones: Path) -> None:
    (zones / "Real").write_bytes(_utc_bytes())
    (zones / "Link").symlink_to("Real")
    assert Gateway(tz_dirs=(str(zones),)).load_zone("Link").key == "Link"


def test_the_first_directory_that_has_the_zone_wins(tmp_path: Path, zones: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "Z").write_bytes(b"not a zone file")
    (zones / "Z").write_bytes(_utc_bytes())
    assert Gateway(tz_dirs=(str(zones), str(other))).load_zone("Z").key == "Z"
    with pytest.raises(ZoneError):
        Gateway(tz_dirs=(str(other), str(zones))).load_zone("Z")


def test_the_default_search_path_is_the_system_database() -> None:
    if _SYSTEM is None:
        if os.environ.get("CI"):
            pytest.fail("CI needs a system time zone database")
        pytest.skip("no system time zone database")
    assert Gateway().tz_dirs == SYSTEM_TZ_DIRS
    assert Gateway().load_zone("UTC").key == "UTC"


def test_pythontzpath_and_the_module_search_path_are_ignored(
    zones: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (zones / "Fake").write_bytes(_utc_bytes())
    monkeypatch.setenv("PYTHONTZPATH", str(zones))
    monkeypatch.setattr(zoneinfo, "TZPATH", (str(zones),))
    with pytest.raises(ZoneError):
        Gateway().load_zone("Fake")


@pytest.mark.parametrize(
    "key",
    [
        "",
        "../x",
        "a/../b",
        "/etc/passwd",
        "a//b",
        "a/b/c/d",
        "x" * 65,
        "a b",
        "a\x00b",
        "café",
        "a.b",
        "localtime",
        "LocalTime",
        "posixrules",
        "Factory",
        "factory",
        "posix/Factory",
        "right/Factory",
        "posix/localtime",
        "right/posixrules",
    ],
)
def test_bad_keys_are_refused_before_any_file_is_touched(zones: Path, key: str) -> None:
    (zones / "localtime").write_bytes(b"x")
    for prefix in ("posix", "right"):
        (zones / prefix).mkdir(exist_ok=True)
        for name in ("Factory", "localtime", "posixrules"):
            (zones / prefix / name).write_bytes(_utc_bytes())
    with pytest.raises(ZoneError):
        Gateway(tz_dirs=(str(zones),)).load_zone(key)


def test_a_nested_factory_zone_is_refused_even_though_the_file_exists(zones: Path) -> None:
    (zones / "a" / "b").mkdir(parents=True)
    (zones / "a" / "b" / "FACTORY").write_bytes(_utc_bytes())
    with pytest.raises(ZoneError):
        Gateway(tz_dirs=(str(zones),)).load_zone("a/b/FACTORY")


def test_unknown_zone_is_refused(zones: Path) -> None:
    with pytest.raises(ZoneError):
        Gateway(tz_dirs=(str(zones),)).load_zone("Nowhere")


def test_missing_directories_are_skipped_and_the_zone_is_unknown(tmp_path: Path) -> None:
    with pytest.raises(ZoneError):
        Gateway(tz_dirs=(str(tmp_path / "absent"),)).load_zone("UTC")


def test_a_directory_is_not_a_zone(zones: Path) -> None:
    with pytest.raises(ZoneError):
        Gateway(tz_dirs=(str(zones),)).load_zone("Area")


def test_a_file_that_is_not_tzif_is_refused(zones: Path) -> None:
    (zones / "Junk").write_bytes(b"TZif" + b"\x00" * 10)
    with pytest.raises(ZoneError):
        Gateway(tz_dirs=(str(zones),)).load_zone("Junk")


def test_an_oversized_file_is_refused_without_reading_it_all(zones: Path) -> None:
    (zones / "Big").write_bytes(b"TZif" + b"\x00" * (1024 * 1024))
    with pytest.raises(ZoneError):
        Gateway(tz_dirs=(str(zones),)).load_zone("Big")


def test_the_error_never_names_the_key(zones: Path) -> None:
    with pytest.raises(ZoneError) as info:
        Gateway(tz_dirs=(str(zones),)).load_zone("Secretish")
    assert "Secretish" not in str(info.value)


def test_a_read_error_is_one_zone_error_and_closes_the_descriptor(
    zones: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (zones / "Z").write_bytes(_utc_bytes())
    opened: list[Any] = []
    real_fdopen = os.fdopen

    class Failing:
        def __init__(self, file: Any) -> None:
            self.file = file
            opened.append(file)

        def __enter__(self) -> Failing:
            return self

        def __exit__(self, *exc: object) -> None:
            self.file.close()

        def read(self, size: int) -> bytes:
            raise OSError(5, "boom")

    monkeypatch.setattr(os, "fdopen", lambda *a, **k: Failing(real_fdopen(*a, **k)))
    with pytest.raises(ZoneError) as info:
        Gateway(tz_dirs=(str(zones),)).load_zone("Z")
    assert info.value.__cause__ is None
    assert opened[0].closed
