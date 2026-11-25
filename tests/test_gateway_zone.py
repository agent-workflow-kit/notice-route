"""Gateway.load_zone: time zones come only from the system database, never from the environment."""

from __future__ import annotations

import zoneinfo
from pathlib import Path

import pytest

from notice_route.gateway import SYSTEM_TZ_DIRS, Gateway, ZoneError

_SYSTEM = next((Path(d) for d in SYSTEM_TZ_DIRS if (Path(d) / "UTC").is_file()), None)
needs_system_zones = pytest.mark.skipif(_SYSTEM is None, reason="no system time zone database")


def _utc_bytes() -> bytes:
    assert _SYSTEM is not None
    return (_SYSTEM / "UTC").read_bytes()


@pytest.fixture
def zones(tmp_path: Path) -> Path:
    base = tmp_path / "zones"
    (base / "Area").mkdir(parents=True)
    return base


@needs_system_zones
def test_a_zone_loads_from_a_given_directory(zones: Path) -> None:
    (zones / "Area" / "Town").write_bytes(_utc_bytes())
    zone = Gateway(tz_dirs=(str(zones),)).load_zone("Area/Town")
    assert zone.key == "Area/Town"


@needs_system_zones
def test_a_symlinked_zone_file_is_followed(zones: Path) -> None:
    (zones / "Real").write_bytes(_utc_bytes())
    (zones / "Link").symlink_to("Real")
    assert Gateway(tz_dirs=(str(zones),)).load_zone("Link").key == "Link"


@needs_system_zones
def test_the_first_directory_that_has_the_zone_wins(tmp_path: Path, zones: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "Z").write_bytes(b"not a zone file")
    (zones / "Z").write_bytes(_utc_bytes())
    assert Gateway(tz_dirs=(str(zones), str(other))).load_zone("Z").key == "Z"
    with pytest.raises(ZoneError):
        Gateway(tz_dirs=(str(other), str(zones))).load_zone("Z")


@needs_system_zones
def test_the_default_search_path_is_the_system_database() -> None:
    assert Gateway().tz_dirs == SYSTEM_TZ_DIRS
    assert Gateway().load_zone("UTC").key == "UTC"


@needs_system_zones
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
    ],
)
def test_bad_keys_are_refused_before_any_file_is_touched(zones: Path, key: str) -> None:
    (zones / "localtime").write_bytes(b"x")
    with pytest.raises(ZoneError):
        Gateway(tz_dirs=(str(zones),)).load_zone(key)


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
