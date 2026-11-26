"""Live-suite wiring. Every test under ``tests/live`` gets the ``live`` marker here (a new test
cannot forget it); ``pyproject.toml`` deselects that marker by default. Without
``NOTICE_ROUTE_LIVE=1`` every test is skipped before anything else happens (``make live`` also
refuses to start). Nothing here reads a credential or starts a process at import time.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.live.support import (
    OPT_IN_VAR,
    TOOLS,
    LiveRouter,
    Report,
    ToolConfig,
    opted_in,
    tool_config,
    wsl_ready,
)

LIVE_DIR = Path(__file__).resolve().parent


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if LIVE_DIR in item.path.parents:
            item.add_marker(pytest.mark.live)


@pytest.fixture(autouse=True)
def live_opt_in() -> None:
    """The opt-in, checked before any other fixture of a live test does anything."""
    if not opted_in(os.environ):
        pytest.skip(f"live suite is opt-in: set {OPT_IN_VAR}=1 (see docs/live-testing.md)")


@pytest.fixture(scope="session")
def report() -> Iterator[Report]:
    rep = Report()
    yield rep
    path = rep.write(os.environ)
    if path is not None:
        print(f"\nlive observation report: {path}")  # noqa: T201 - the person reads this


@pytest.fixture(scope="session")
def run_id(report: Report) -> str:
    return report.run_id


@pytest.fixture
def scratch() -> Iterator[Path]:
    """A private directory (mode 0700) for one test; removed at the end."""
    path = Path(tempfile.mkdtemp(prefix="notice-route-live-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def make_tool(report: Report) -> Callable[..., ToolConfig]:
    """``make_tool(name, need_credential=...)``: the designated real notifier, or a skip that
    names the missing prerequisite (never a value). A credential value is registered for
    redaction at once."""

    def make(name: str, *, need_credential: bool = False, need_wsl: bool = False) -> ToolConfig:
        assert name in TOOLS
        cfg, reason = tool_config(name, os.environ, need_credential=need_credential)
        if cfg is None:
            pytest.skip(reason)
        if need_wsl and (why := wsl_ready(os.environ)):
            pytest.skip(why)
        if cfg.cred_env:
            report.protected.add(os.environ[cfg.cred_env])
        return cfg

    return make


@pytest.fixture
def router(scratch: Path) -> Iterator[LiveRouter]:
    """A router in a private passwd home. ``floor`` is the scratch directory, so hook and
    generic-script destinations under it pass the executable check; real notifier paths (outside
    it) are checked up to the root as in production."""
    home = scratch / "home"
    home.mkdir()
    home.chmod(0o700)
    r = LiveRouter(home=home, floor=str(scratch), environ=os.environ)
    yield r
    r.finish()
