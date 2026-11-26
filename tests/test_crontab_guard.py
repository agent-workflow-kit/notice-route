"""The suite's guard against the real ``crontab``: it refuses before anything starts.

Every test here replaces the guard's table of real start functions (``Guard.real``) with
recorders that start nothing, so a regression of the guard cannot reach the real ``crontab``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from notice_route.gateway import Gateway
from tests.crontab_guard import SYSTEM_CRONTABS, Guard, RealCrontabAttempt
from tests.fakecron import FakeCrontab


@pytest.fixture
def spawned(crontab_guard_fixture: Guard) -> list[object]:
    """Replace every real start function the guard lets a call through to with a recorder that
    starts nothing: a refused call must never reach it."""
    seen: list[object] = []

    def recorder(name: str) -> Any:
        def record(*args: object, **kwargs: object) -> Any:
            seen.append((name, args[1:] if name == "Popen.__init__" else args))
            raise OSError("not started (test recorder)")

        return record

    for name in list(crontab_guard_fixture.real):
        crontab_guard_fixture.real[name] = recorder(name)
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


@pytest.mark.parametrize(
    "env",
    [{}, {"LC_ALL": "C"}, {b"PATH": b"/usr/bin"}],
    ids=["empty", "no_path", "bytes_path"],
)
def test_a_child_environment_without_the_fake_on_its_path_is_refused(
    crontab_guard_fixture: Guard, spawned: list[object], env: dict[Any, Any]
) -> None:
    """subprocess searches the child's own ``PATH`` (or the default), never the parent's: with
    the fake first on the parent's ``PATH`` the name must not resolve to the fake."""
    crontab_guard_fixture.expected = True
    with pytest.raises(RealCrontabAttempt):
        subprocess.run(["crontab", "-l"], env=env, check=False)
    assert spawned == []


def test_a_shell_command_with_a_crontab_word_is_refused(
    crontab_guard_fixture: Guard, spawned: list[object]
) -> None:
    crontab_guard_fixture.expected = True
    for command in (
        "true && /usr/bin/crontab -l",
        "true; crontab -l",
        "echo hi | '/usr/bin/crontab' -l",
    ):
        with pytest.raises(RealCrontabAttempt):
            subprocess.run(command, shell=True, env={}, check=False)
    assert spawned == []


def test_an_interpreter_started_with_a_crontab_argument_is_refused(
    crontab_guard_fixture: Guard, spawned: list[object]
) -> None:
    crontab_guard_fixture.expected = True
    for argv in (
        ["sh", "-c", "/usr/bin/crontab -l"],
        ["/bin/sh", "-c", "true && crontab -l"],
        ["env", "crontab", "-l"],
        ["python3", "-c", "import os; os.execv('/usr/bin/crontab', [])"],
    ):
        with pytest.raises(RealCrontabAttempt):
            subprocess.run(argv, env={"PATH": "/usr/bin:/bin"}, check=False)
    assert spawned == []


def test_a_harmless_shell_command_is_not_refused(
    crontab_guard_fixture: Guard, spawned: list[object]
) -> None:
    with pytest.raises(OSError, match="test recorder"):
        subprocess.run("echo hi && true", shell=True, check=False)
    assert len(spawned) == 1
    assert crontab_guard_fixture.attempts == []


@pytest.mark.parametrize(
    "name", ["spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe"]
)
def test_the_spawn_family_is_guarded(
    crontab_guard_fixture: Guard, spawned: list[object], name: str
) -> None:
    crontab_guard_fixture.expected = True
    call = getattr(os, name)
    program = "/usr/bin/crontab"
    if name.startswith("spawnv"):
        extra = ({},) if name.endswith("e") else ()
        args: tuple[Any, ...] = (os.P_WAIT, program, [program, "-l"], *extra)
    else:
        extra = ({},) if name.endswith("e") else ()
        args = (os.P_WAIT, program, program, "-l", *extra)
    with pytest.raises(RealCrontabAttempt):
        call(*args)
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
