from __future__ import annotations

import builtins
import io
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from notice_route.gateway import DirHandle, Gateway
from tests import crontab_guard
from tests.fakecron import FakeCrontab, build_fake_crontab
from tests.fakes import Hook, Notifier, build_hook, build_notifier
from tests.send_support import Rig


@pytest.fixture
def opened_names(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every file name opened through ``open``, ``io.open``, ``os.open`` (also the ``Path``
    read and open methods, which call ``io.open``) and ``Gateway.open_file`` while the test runs.
    ``lstat`` and ``stat`` are not recorded. A name is the path as given (maybe relative to a
    directory descriptor), so tests compare base names."""
    seen: list[str] = []

    def record(target: object) -> None:
        if isinstance(target, (str, bytes, os.PathLike)):
            seen.append(os.fsdecode(target))

    def wrap(original: Callable[..., Any]) -> Callable[..., Any]:
        def inner(target: object, *args: Any, **kwargs: Any) -> Any:
            record(target)
            return original(target, *args, **kwargs)

        return inner

    monkeypatch.setattr(builtins, "open", wrap(builtins.open))
    monkeypatch.setattr(io, "open", wrap(io.open))
    monkeypatch.setattr(os, "open", wrap(os.open))
    original_open_file = Gateway.open_file

    def open_file(self: Gateway, parent: DirHandle, name: str, **kwargs: Any) -> Any:
        record(name)
        return original_open_file(self, parent, name, **kwargs)

    monkeypatch.setattr(Gateway, "open_file", open_file)
    return seen


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A private HOME for every process a test starts; XDG_CONFIG_HOME lives under it."""
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def make_notifier(tmp_path: Path) -> Iterator[Callable[..., Notifier]]:
    """Build fake notifiers; at teardown signal exactly the processes they recorded."""
    built: list[Notifier] = []

    def make(name: str = "slack-notice", **behavior: Any) -> Notifier:
        notifier = build_notifier(tmp_path / "bin", name, **behavior)
        built.append(notifier)
        return notifier

    yield make
    for notifier in built:
        notifier.kill_tracked()


@pytest.fixture
def make_hook(tmp_path: Path) -> Iterator[Callable[..., Hook]]:
    """Build fake hook commands; at teardown signal exactly the processes they recorded."""
    built: list[Hook] = []

    def make(name: str = "hook", **behavior: Any) -> Hook:
        h = build_hook(tmp_path / "hooks", name, **behavior)
        built.append(h)
        return h

    yield make
    for h in built:
        h.kill_tracked()


@pytest.fixture
def send_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A private environment for the tests of ``send``: nothing of the user's leaks in."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.delenv("NOTICE_ROUTE_HOOK", raising=False)


@pytest.fixture
def rig(
    home: Path,
    tmp_path: Path,
    make_notifier: Callable[..., Notifier],
    capsys: pytest.CaptureFixture[str],
) -> Iterator[Rig]:
    r = Rig(home, tmp_path, make_notifier, capsys)
    yield r
    r.finish()


@pytest.fixture(autouse=True)
def crontab_guard_fixture(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[crontab_guard.Guard]:
    """Every test runs with a private ``PATH`` whose ``crontab`` is a fake, and with a guard that
    refuses (before anything starts) any attempt to run another ``crontab``, by name or by
    path. A refused attempt fails the test at teardown even when the code under test caught it
    (unless the test says it expects one: ``guard.expected``)."""
    guard = crontab_guard.Guard()
    fake = build_fake_crontab(tmp_path_factory.mktemp("fakecron"))
    guard.allow(fake.stub)
    crontab_guard.install(monkeypatch, guard)
    monkeypatch.setenv("PATH", crontab_guard.private_path(fake.bin_dir, os.environ.get("PATH", "")))
    request.node.fake_crontab = fake
    yield guard
    if not guard.expected:
        assert guard.attempts == [], f"a test tried to run the real crontab: {guard.attempts}"


@pytest.fixture
def fake_crontab(request: pytest.FixtureRequest) -> FakeCrontab:
    """The fake crontab of this test (its table starts absent: ``no crontab for``)."""
    return request.node.fake_crontab  # type: ignore[no-any-return]
