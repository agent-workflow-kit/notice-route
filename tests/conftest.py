from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import Notifier, build_notifier


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
