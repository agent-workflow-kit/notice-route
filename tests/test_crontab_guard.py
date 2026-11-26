"""The suite's guard against the real ``crontab``: it refuses before anything starts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from notice_route.gateway import Gateway
from tests.crontab_guard import SYSTEM_CRONTABS, Guard, RealCrontabAttempt
from tests.fakecron import FakeCrontab


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch, crontab_guard_fixture: Guard) -> list[object]:
    """Replace the base ``Popen.__init__`` (what the guard calls once it lets a call through)
    with a recorder that starts nothing: a refused call must never reach it."""
    seen: list[object] = []

    def record(self: object, args: object, *rest: object, **kwargs: object) -> None:
        seen.append(args)
        raise OSError("not started (test recorder)")

    monkeypatch.setattr(crontab_guard_fixture.base_popen, "__init__", record)
    return seen


def test_the_real_crontab_is_refused_by_absolute_path(
    crontab_guard_fixture: Guard, spawned: list[object]
) -> None:
    crontab_guard_fixture.expected = True
    for path in sorted(SYSTEM_CRONTABS | {"/usr/bin/crontab", "/bin/crontab"}):
        with pytest.raises(RealCrontabAttempt):
            Gateway().run([path, "-l"], env={"PATH": "/usr/bin:/bin"}, timeout=5)
    assert spawned == []
    assert "/usr/bin/crontab" in crontab_guard_fixture.attempts


def test_the_real_crontab_is_refused_by_name_through_the_system_path(
    crontab_guard_fixture: Guard, spawned: list[object]
) -> None:
    crontab_guard_fixture.expected = True
    with pytest.raises(RealCrontabAttempt):
        Gateway().run(["crontab", "-l"], env={"PATH": "/usr/bin:/bin"}, timeout=5)
    assert spawned == []


def test_a_symlink_or_dotdot_path_to_the_real_crontab_is_refused(
    crontab_guard_fixture: Guard, spawned: list[object], tmp_path: Path
) -> None:
    crontab_guard_fixture.expected = True
    real = next(iter(sorted(SYSTEM_CRONTABS)))
    link = tmp_path / "innocent"
    link.symlink_to(real)
    with pytest.raises(RealCrontabAttempt):
        Gateway().run([str(link), "-l"], env={"PATH": "/usr/bin"}, timeout=5)
    with pytest.raises(RealCrontabAttempt):
        Gateway().run(["/usr/bin/../bin/crontab", "-l"], env={"PATH": "/usr/bin"}, timeout=5)
    assert spawned == []


def test_other_ways_to_start_a_program_are_guarded(
    crontab_guard_fixture: Guard, spawned: list[object]
) -> None:
    crontab_guard_fixture.expected = True
    with pytest.raises(RealCrontabAttempt):
        subprocess.run(["/usr/bin/crontab", "-l"], check=False)
    with pytest.raises(RealCrontabAttempt):
        subprocess.run("/usr/bin/crontab -l", shell=True, check=False)
    with pytest.raises(RealCrontabAttempt):
        os.execv("/usr/bin/crontab", ["crontab", "-l"])
    with pytest.raises(RealCrontabAttempt):
        os.system("/usr/bin/crontab -l")
    with pytest.raises(RealCrontabAttempt):
        os.posix_spawn("/usr/bin/crontab", ["crontab", "-l"], {})
    assert spawned == []


def test_a_swallowed_attempt_still_fails_the_test(crontab_guard_fixture: Guard) -> None:
    """Without ``expected`` the teardown would fail the test: show the check by hand."""
    try:
        crontab_guard_fixture.check("/usr/bin/crontab")
    except RealCrontabAttempt:
        pass
    assert crontab_guard_fixture.attempts == ["/usr/bin/crontab"]
    crontab_guard_fixture.attempts.clear()  # so that this test's own teardown passes


def test_the_fake_is_the_crontab_on_path_and_runs(fake_crontab: FakeCrontab) -> None:
    result = Gateway().run(
        ["crontab", "-l"], env={"PATH": os.environ["PATH"], "LC_ALL": "C"}, timeout=10
    )
    assert result.returncode == 1
    assert result.stderr == b"no crontab for fakeuser\n"
    assert fake_crontab.kinds() == ["list"]
