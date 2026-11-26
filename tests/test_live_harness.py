"""Offline proof that the live suite's plumbing is sound (#31). No network, no real notifier, no
real crontab: these tests run in ``make verify``.

They show: the ``live`` marker is deselected by default and the suite cannot be collected into a
normal run; the opt-in is required (``make live`` refuses, a live test skips); a missing
prerequisite is a skip with a reason that holds no value; a credential never reaches argv, the
report or an error; no live module does anything at import time or imports a network library;
the suite never names ``crontab`` and has no bypass of the autouse guard; the docs and
FIDELITY.md name the variables and tests that exist. A local plumbing check drives the router
child (private home, clock offset) against a script destination instead of a real notifier.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.live import plan, support
from tests.live.support import (
    ENV_VARS,
    OPT_IN_VAR,
    PLANNED_MESSAGES,
    TOOLS,
    WORST_CASE_MESSAGES,
    LiveRouter,
    Protected,
    Report,
    communicate_bounded,
    found_in,
    opted_in,
    tool_config,
    wait_group_gone,
    window_around_now,
    wsl_ready,
)

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "tests" / "live"
LIVE_FILES = sorted(p for p in LIVE.glob("*.py"))
CANARY = "canary-value-9f3b2c71"
ODD = 'q"uo\\te-\u00e9-\u4e2d-value-5d1e'  # a quote, a backslash and non-ASCII characters


def clean_env(tmp: Path, **extra: str) -> dict[str, str]:
    """The environment of a child: no inherited variable, so no credential and no opt-in."""
    bare = tmp / "emptybin"
    bare.mkdir(exist_ok=True)
    env = {"PATH": str(bare), "HOME": str(tmp), "LANG": "C.UTF-8"}
    env.update(extra)
    return env


def pytest_child(tmp: Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=ROOT,
        env=clean_env(tmp, **extra),
        capture_output=True,
        text=True,
        timeout=240,
    )


# --- deselection -----------------------------------------------------------------------------


def test_live_marker_is_registered_and_deselected_by_default() -> None:
    text = (ROOT / "pyproject.toml").read_text()
    assert '"live:' in text and "live_local:" in text
    assert "-m 'not live'" in text


def test_a_normal_run_collects_no_live_test(tmp_path: Path) -> None:
    done = pytest_child(tmp_path, "--collect-only", "-q", "tests/live")
    assert done.returncode == 5, done.stdout  # nothing collected
    assert "deselected" in done.stdout
    assert "::" not in done.stdout.split("deselected")[0]


def test_marker_run_collects_every_live_test_and_marks_each(tmp_path: Path) -> None:
    done = pytest_child(tmp_path, "--collect-only", "-q", "-m", "live", "tests/live")
    ids = [line for line in done.stdout.splitlines() if line.startswith("tests/live/")]
    assert done.returncode == 0, done.stdout[-500:]
    files = {i.split("::")[0] for i in ids}
    assert files == {f"tests/live/{p.name}" for p in LIVE_FILES if p.name.startswith("test_")}
    assert len(ids) >= 20


def test_without_the_opt_in_every_live_test_skips_before_doing_anything(tmp_path: Path) -> None:
    done = pytest_child(tmp_path, "-m", "live", "-q", "-rs", "tests/live")
    assert done.returncode == 0, done.stdout[-800:]
    summary = done.stdout.strip().splitlines()[-1]
    assert re.fullmatch(r"\d+ skipped.*", summary), summary
    assert "passed" not in summary and "failed" not in summary
    assert f"set {OPT_IN_VAR}=1" in done.stdout


def test_opted_in_without_prerequisites_skips_never_fails(tmp_path: Path) -> None:
    done = pytest_child(
        tmp_path, "-m", "live and not live_local", "-q", "-rs", "tests/live", **{OPT_IN_VAR: "1"}
    )
    assert done.returncode == 0, done.stdout[-800:]
    summary = done.stdout.strip().splitlines()[-1]
    assert re.fullmatch(r"\d+ skipped.*", summary), summary
    assert "is not on PATH" in done.stdout


# --- the opt-in --------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", "2", " 1", "1 "])
def test_only_exactly_one_opts_in(value: str | None) -> None:
    env = {} if value is None else {OPT_IN_VAR: value}
    assert opted_in(env) is False


def test_one_opts_in() -> None:
    assert opted_in({OPT_IN_VAR: "1"}) is True


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_make_live_refuses_without_the_opt_in(tmp_path: Path) -> None:
    env = clean_env(tmp_path)
    env["PATH"] = os.environ["PATH"]  # make needs a shell; uv is never reached
    done = subprocess.run(
        ["make", "live"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 2
    assert f"{OPT_IN_VAR}=1" in done.stderr
    assert "pytest" not in done.stdout  # the recipe stopped before the pytest line was shown


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_make_live_runs_only_live_tests_and_prints_its_plan() -> None:
    env = {"PATH": os.environ["PATH"], OPT_IN_VAR: "1"}
    done = subprocess.run(
        ["make", "-n", "live"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30
    )  # -n: print, run nothing
    assert done.returncode == 0
    assert "tests.live.plan" in done.stdout
    assert "pytest -m live tests/live" in done.stdout


def test_make_verify_does_not_depend_on_live() -> None:
    makefile = (ROOT / "Makefile").read_text()
    verify = next(line for line in makefile.splitlines() if line.startswith("verify:"))
    assert "live" not in verify


def test_plan_names_variables_and_never_values(tmp_path: Path) -> None:
    exe = tmp_path / "ha-notice"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o700)
    env = {"HA_CRED": CANARY, TOOLS["ha-notice"].cred_var: "HA_CRED"}
    text = plan.describe(env, which=lambda n: str(exe) if n == "ha-notice" else None)
    assert "ha-notice: will run" in text and "HA_CRED" in text
    assert CANARY not in text
    assert "slack-notice: its tests will be skipped" in text
    assert f"{PLANNED_MESSAGES} planned" in text and f"at most {WORST_CASE_MESSAGES}" in text
    assert plan.describe({}, which=lambda n: None).count("will be skipped") == 3


# --- prerequisites -----------------------------------------------------------------------------


def make_exe(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path


def test_missing_binary_is_a_reason_not_an_error() -> None:
    cfg, reason = tool_config("slack-notice", {}, need_credential=True, which=lambda n: None)
    assert cfg is None and "slack-notice is not on PATH" in reason
    assert TOOLS["slack-notice"].bin_var in reason


def test_prerequisite_reasons_never_hold_a_value(tmp_path: Path) -> None:
    exe = make_exe(tmp_path, "ha-notice")
    which = lambda n: str(exe)  # noqa: E731
    spec = TOOLS["ha-notice"]
    cases: list[dict[str, str]] = [
        {},  # no variable naming the credential
        {spec.cred_var: "not a name; rm"},  # a bad name
        {spec.cred_var: "HA_CRED"},  # the named variable is unset
        {spec.cred_var: "HA_CRED", "HA_CRED": "   "},  # whitespace only
        {spec.cred_var: "HA_CRED", "HA_CRED": CANARY, spec.config_var: "relative.yaml"},
        {spec.cred_var: "HA_CRED", "HA_CRED": CANARY, spec.config_var: str(tmp_path / "no.yaml")},
    ]
    for env in cases:
        cfg, reason = tool_config("ha-notice", env, need_credential=True, which=which)
        assert cfg is None and reason, env
        assert CANARY not in reason


def test_a_designated_tool_is_ready_without_a_credential_when_none_is_needed(
    tmp_path: Path,
) -> None:
    exe = make_exe(tmp_path, "slack-notice")
    cfg, reason = tool_config("slack-notice", {}, need_credential=False, which=lambda n: str(exe))
    assert reason == "" and cfg is not None
    assert cfg.cmd == [os.path.realpath(exe)]
    assert cfg.cred_env is None


def test_a_designated_tool_with_a_credential_and_config(tmp_path: Path) -> None:
    exe = make_exe(tmp_path, "ha-notice")
    conf = tmp_path / "ha.yaml"
    conf.write_text("x: 1\n")
    spec = TOOLS["ha-notice"]
    env = {spec.cred_var: "HA_CRED", "HA_CRED": CANARY, spec.config_var: str(conf)}
    cfg, reason = tool_config("ha-notice", env, need_credential=True, which=lambda n: str(exe))
    assert reason == "" and cfg is not None
    assert cfg.cred_env == "HA_CRED"
    assert cfg.cmd == [os.path.realpath(exe), "--config", str(conf)]
    assert CANARY not in repr(cfg) and CANARY not in " ".join(cfg.cmd)


def test_wsl_needs_both_variables() -> None:
    assert "WSL_DISTRO_NAME" in wsl_ready({})
    assert "WSL_INTEROP" in wsl_ready({"WSL_DISTRO_NAME": "x"})
    assert wsl_ready({"WSL_DISTRO_NAME": "x", "WSL_INTEROP": "/run/x"}) == ""


@pytest.mark.parametrize(
    "name",
    ["HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "WSL_INTEROP", "NOTICE_ROUTE_LIVE",
     "XDG_CONFIG_HOME", "TMPDIR"],
)  # fmt: skip
def test_a_reserved_variable_name_is_refused_for_the_credential(tmp_path: Path, name: str) -> None:
    exe = make_exe(tmp_path, "ha-notice")
    env = {TOOLS["ha-notice"].cred_var: name, name: CANARY}
    cfg, reason = tool_config("ha-notice", env, need_credential=True, which=lambda n: str(exe))
    assert cfg is None and "reserved" in reason
    assert CANARY not in reason


# --- credentials never leak ----------------------------------------------------------------------


def test_credential_reaches_a_child_only_through_its_environment(tmp_path: Path) -> None:
    spec = TOOLS["slack-notice"]
    exe = make_exe(tmp_path, "slack-notice")
    env = {spec.cred_var: "SLACK_CRED", "SLACK_CRED": CANARY, "PATH": "/usr/bin"}
    cfg, _ = tool_config("slack-notice", env, need_credential=True, which=lambda n: str(exe))
    assert cfg is not None
    child = support.tool_env(cfg, env, credential=True)
    assert child["SLACK_CRED"] == CANARY
    assert CANARY not in support.tool_env(cfg, env, credential=False).values()
    assert set(child) <= {"PATH", "LANG", "SLACK_CRED"}
    router = LiveRouter(tmp_path, str(tmp_path), env, pass_names=("SLACK_CRED",))
    argv = router.argv("send", "--stdin-json")
    assert not any(CANARY in a for a in argv)
    assert router.env()["SLACK_CRED"] == CANARY
    assert not any(k.endswith("CRED_ENV") for k in router.env())  # the suite's own variables stay


def test_report_redacts_writes_private_and_stays_empty_when_unused(tmp_path: Path) -> None:
    report = Report()
    assert report.write({support.REPORT_DIR_VAR: str(tmp_path / "r")}) is None
    report.protected.add(CANARY)
    report.record("x", stdout=f"before {CANARY} after", nested={"k": [CANARY]})
    path = report.write({support.REPORT_DIR_VAR: str(tmp_path / "r")})
    assert path is not None
    text = path.read_text()
    assert CANARY not in text and "<redacted>" in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(text)["observations"]["x"]["stdout"] == "before <redacted> after"


def test_report_default_directory_is_a_new_private_temp_directory() -> None:
    report = Report()
    report.record("x", a=1)
    path = report.write({})
    assert path is not None
    try:
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert path.parent.name.startswith("notice-route-live-")
    finally:
        shutil.rmtree(path.parent)


def test_report_redacts_values_with_quotes_backslashes_and_non_ascii(tmp_path: Path) -> None:
    report = Report()
    report.protected.add(ODD)
    escaped = json.dumps(ODD)[1:-1]  # how a JSON output would carry the value
    report.record("x", out=f"a {ODD} b", enc=f"a {escaped} b", nested={ODD: [ODD, {"k": ODD}]},
                  obj=ODD.encode())  # fmt: skip
    path = report.write({support.REPORT_DIR_VAR: str(tmp_path / "r")})
    assert path is not None
    raw = path.read_bytes().decode("utf-8")
    for form in (ODD, escaped, json.dumps(ODD, ensure_ascii=False)[1:-1]):
        assert form not in raw
    assert not report.protected.leaks(raw)
    assert json.loads(raw)["observations"]["x"]["out"] == "a <redacted> b"


def test_clip_redacts_before_it_cuts() -> None:
    p = Protected()
    p.add(CANARY)
    text = "x" * 10 + CANARY
    assert CANARY[:6] in text[:16]  # a cut first would leave a fragment of the value
    clipped = p.clip(text, 16)
    assert CANARY[:6] not in clipped and clipped.startswith("xxxxxxxxxx<")


def test_found_in_and_leaks_look_at_text_and_bytes() -> None:
    p = Protected()
    p.add(CANARY)
    assert found_in("some text", "a log with some text in it", b"x")
    assert not found_in("some text", "nothing", b"here")
    assert not p.leaks("no value here", CANARY[:5])  # a fragment is not a leak
    assert p.leaks(CANARY.encode() + b"\xff")  # bytes: found even beside invalid UTF-8


LEAKING_TEST = """
import os

from tests.live.support import Protected, found_in

CANARY = os.environ["LEAK_CANARY"]  # not written in this file, so only a leak can show it


def test_leak_in_output():
    p = Protected()
    p.add(CANARY)
    leaked = p.leaks("stdout holds " + CANARY + " here")
    assert not leaked, "credential value found in the router output (redacted)"


def test_message_in_log():
    present = found_in(CANARY, "log " + CANARY)
    assert not present, "text that must not be present was found in the log"
"""


def test_a_forced_leak_prints_no_value_in_the_failure_output(tmp_path: Path) -> None:
    """The failure of a leak check, as pytest prints it (assertion rewriting, `-vv`, long
    tracebacks; `make live` shows no locals)."""
    test_file = tmp_path / "test_forced_leak.py"
    test_file.write_text(LEAKING_TEST)
    done = pytest_child(
        tmp_path, "-c", os.devnull, "--rootdir", str(tmp_path), "-vv", "--tb=long", "-rA",
        str(test_file), LEAK_CANARY=CANARY,
    )  # fmt: skip
    shown = done.stdout + done.stderr
    assert done.returncode == 1 and "2 failed" in shown, shown[-600:]
    assert CANARY not in shown
    assert "credential value found in the router output (redacted)" in shown


def assert_problems(source: str) -> list[str]:
    """Assertions of a live test that can print an output or a value: a ``leaks`` call inside the
    test of an ``assert``, a membership test of an output or log text, or a message that holds a
    raw ``out`` or ``err`` (it must pass through ``clip``)."""
    problems: list[str] = []

    def raw_output(node: ast.AST, under_clip: bool = False) -> bool:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            under_clip = under_clip or node.func.attr in {"clip", "redact"}
        if isinstance(node, ast.Attribute) and node.attr in {"out", "err"} and not under_clip:
            return True
        return any(raw_output(c, under_clip) for c in ast.iter_child_nodes(node))

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assert):
            continue
        for sub in ast.walk(node.test):
            if isinstance(sub, ast.Call):
                func = sub.func
                called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if called in {"leaks", "found_in"}:
                    problems.append(f"line {node.lineno}: {called}() inside an assert")
            if isinstance(sub, ast.Compare) and any(
                isinstance(o, (ast.In, ast.NotIn)) for o in sub.ops
            ):
                names = {n.id for n in ast.walk(sub) if isinstance(n, ast.Name)}
                attrs = {n.attr for n in ast.walk(sub) if isinstance(n, ast.Attribute)}
                if names & {"log_text", "db_bytes"} or attrs & {"out", "err"}:
                    problems.append(f"line {node.lineno}: membership test of an output")
        if node.msg is not None and raw_output(node.msg):
            problems.append(f"line {node.lineno}: a message holds a raw output")
    return problems


def test_no_live_assertion_can_print_an_output() -> None:
    for path in LIVE_FILES:
        assert assert_problems(path.read_text()) == [], path.name


@pytest.mark.parametrize(
    "source",
    [
        "assert not report.protected.leaks(r.out)",
        "assert not found_in(msg, r.out)",
        "assert msg not in log_text",
        "assert msg not in r.out",
        "assert r.code == 0, r.err[:300]",
        "assert x, (rows, follow.out[:300])",
    ],
)
def test_the_assertion_check_flags_each_shape_that_leaked(source: str) -> None:
    assert assert_problems(source)


def test_the_assertion_check_accepts_the_safe_shapes() -> None:
    safe = (
        "assert r.code == 0, f'exit {r.code}'\nassert x, report.clip(r.err, 30)\nassert a in (1, 2)"
    )
    assert assert_problems(safe) == []


def test_report_directory_is_private_whatever_the_umask(tmp_path: Path) -> None:
    old = os.umask(0o002)
    try:
        report = Report()
        report.record("x", a=1)
        nested = tmp_path / "a" / "b"
        path = report.write({support.REPORT_DIR_VAR: str(nested)})
        assert path is not None and stat.S_IMODE(nested.stat().st_mode) == 0o700
        loose = tmp_path / "loose"
        loose.mkdir()
        loose.chmod(0o775)  # an existing directory that others can enter
        Report(observations={"x": 1}).write({support.REPORT_DIR_VAR: str(loose)})
        assert stat.S_IMODE(loose.stat().st_mode) == 0o700
        link = tmp_path / "link"
        link.symlink_to(loose)
        with pytest.raises(RuntimeError):
            Report(observations={"x": 1}).write({support.REPORT_DIR_VAR: str(link)})
    finally:
        os.umask(old)


def test_protected_ignores_trivial_values_and_detects_leaks() -> None:
    p = Protected()
    p.add("ab")  # too short to protect: redacting it would mangle text
    p.add(CANARY)
    assert not p.leaks("ab")
    assert p.leaks(f"x{CANARY}") and p.leaks(CANARY.encode())
    assert p.redact(f"a {CANARY} b") == "a <redacted> b"


def test_test_messages_are_clearly_labelled() -> None:
    assert "TEST MESSAGE" in support.message_for("abcd", "x")
    assert "live test" in support.title_for("abcd")


# --- nothing at import time, no network libraries, no crontab ---------------------------


def load_fresh(path: Path) -> None:
    name = f"_live_probe_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while it executes
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)


def test_importing_live_modules_starts_nothing_and_opens_no_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("a live module did real work at import time")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(os, "system", boom)
    monkeypatch.setattr(os, "fork", boom)
    for path in LIVE_FILES:
        load_fresh(path)


def string_constants(tree: ast.AST) -> list[str]:
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def imported_names(tree: ast.AST) -> set[str]:
    """Every dotted name an import statement brings in: ``import a.b`` gives ``a.b``; ``from a
    import b`` gives ``a`` and ``a.b`` (and ``b``, which may itself be a module)."""
    found: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            found |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            module = n.module or ""
            if module:
                found.add(module)
            for a in n.names:
                found.add(a.name)
                if module:
                    found.add(f"{module}.{a.name}")
    return found


def imported_modules(tree: ast.AST) -> set[str]:
    """The top-level packages that are imported."""
    return {name.split(".")[0] for name in imported_names(tree)}


BANNED_NETWORK = {
    "socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "ftplib",
    "smtplib", "imaplib", "poplib", "telnetlib", "xmlrpc", "asyncio", "websockets", "paramiko",
}  # fmt: skip


def banned_import_problems(source: str) -> list[str]:
    tree = ast.parse(source)
    problems = sorted(imported_modules(tree) & BANNED_NETWORK)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            func = n.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if called in {"__import__", "import_module"}:
                problems.append(f"dynamic import at line {n.lineno}")
    return problems


def test_no_live_module_imports_a_network_library() -> None:
    for path in LIVE_FILES:
        assert banned_import_problems(path.read_text()) == [], path.name


@pytest.mark.parametrize(
    "source",
    [
        "import smtplib",
        "from xmlrpc import client",
        "import xmlrpc.client",
        "import asyncio",
        "from ssl import create_default_context",
        "import websockets",
        "x = __import__('socket')",
        "import importlib\nimportlib.import_module('urllib.request')",
        "from importlib import import_module\nimport_module('http.client')",
    ],
)
def test_the_network_ban_flags_each_way_in(source: str) -> None:
    assert banned_import_problems(source)


def guard_mentions(source: str) -> bool:
    tree = ast.parse(source)
    return any("crontab_guard" in n for n in imported_names(tree)) or any(
        isinstance(n, ast.Attribute) and n.attr == "crontab_guard" for n in ast.walk(tree)
    )


@pytest.mark.parametrize(
    "source",
    [
        "from tests import crontab_guard",
        "import tests.crontab_guard",
        "from tests.crontab_guard import expected",
        "import tests.crontab_guard as g",
    ],
)
def test_the_guard_check_catches_each_import_shape(source: str) -> None:
    assert guard_mentions(source)
    assert not guard_mentions("from tests import support\nimport tests.live.support")


def test_the_live_suite_never_names_crontab_and_has_no_guard_bypass() -> None:
    for path in LIVE_FILES:
        tree = ast.parse(path.read_text())
        assert not guard_mentions(path.read_text()), path.name
        for text in string_constants(tree):
            assert not re.search(r"(^|[/\s])crontab(\s+-|$)", text), (path.name, text)
            assert "cron" not in text.split() and "install" not in text.split(), (path.name, text)
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert "crontab_guard_fixture" not in names
        assert not any(
            isinstance(n, ast.Attribute) and n.attr == "expected" for n in ast.walk(tree)
        ), path.name  # the guard's "expect an attempt" switch is for the guard's own test


def test_the_autouse_guard_still_covers_live_tests() -> None:
    """The guard is an autouse fixture of tests/conftest.py, which sits above tests/live; the live
    conftest defines no fixture of the same name that could shadow it."""
    guard = (ROOT / "tests" / "conftest.py").read_text()
    assert "def crontab_guard_fixture" in guard and "autouse=True" in guard
    live_conftest = ast.parse((LIVE / "conftest.py").read_text())
    names = {n.name for n in ast.walk(live_conftest) if isinstance(n, ast.FunctionDef)}
    assert "crontab_guard_fixture" not in names and "fake_crontab" not in names


def test_every_live_test_function_has_a_docstring_naming_what_it_settles() -> None:
    for path in LIVE_FILES:
        if not path.name.startswith("test_"):
            continue
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                assert ast.get_docstring(node), f"{path.name}::{node.name}"


# --- docs and FIDELITY.md ------------------------------------------------------------------------

REF = re.compile(r"tests/live/(test_\w+\.py)::(\w+)")


def live_test_functions() -> set[tuple[str, str]]:
    found = set()
    for path in LIVE_FILES:
        if path.name.startswith("test_"):
            for node in ast.parse(path.read_text()).body:
                if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                    found.add((path.name, node.name))
    return found


def test_the_docs_name_every_variable_the_suite_reads() -> None:
    docs = (ROOT / "docs" / "live-testing.md").read_text()
    for var in ENV_VARS:
        assert f"`{var}`" in docs, var
    for needle in ("make live", "#32", "crontab", "macOS", "Record"):
        assert needle in docs, needle


def test_the_runbook_counts_planned_and_worst_case_messages_and_the_manual_items() -> None:
    docs = (ROOT / "docs" / "live-testing.md").read_text()
    assert f"{PLANNED_MESSAGES} planned" in docs and f"at most {WORST_CASE_MESSAGES}" in docs
    for needle in (
        "A refused notifier path",
        "128+N",
        "SIGSTOP",
        "waiting for the send lock",
        "macOS (#32)",
        "lock_wait",
        "150 s",
    ):
        assert needle in docs, needle
    fidelity = (ROOT / "tests" / "fakebin" / "FIDELITY.md").read_text()
    assert "128+N" in fidelity and "closed stdout" in fidelity


def test_fidelity_links_each_item_to_an_existing_live_test_marked_pending() -> None:
    text = (ROOT / "tests" / "fakebin" / "FIDELITY.md").read_text()
    assert "## Verified live" in text
    section = text.split("## Verified live", 1)[1]
    rows = [line for line in section.splitlines() if line.startswith("| ") and "tests/live" in line]
    assert len(rows) >= 10
    existing = live_test_functions()
    for row in rows:
        assert "verified live: pending" in row, row
        refs = REF.findall(row)
        assert refs and all(r in existing for r in refs), row


def test_every_live_test_is_listed_in_the_runbook() -> None:
    docs = (ROOT / "docs" / "live-testing.md").read_text()
    for _, name in live_test_functions():
        assert f"`{name}`" in docs, name


# --- bounded waits -----------------------------------------------------------------------------

SAFE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


def spawn(argv: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=SAFE_ENV, cwd="/", start_new_session=True,
    )  # fmt: skip


def test_a_run_that_overstays_is_killed_and_reported_as_a_timeout() -> None:
    result = support._run(["sh", "-c", "sleep 30"], b"", SAFE_ENV, timeout=0.3)
    assert result.code == 124 and result.err.startswith("timeout")


def test_communicate_bounded_kills_the_handle_s_group_and_reaps_it() -> None:
    proc = spawn(["sh", "-c", "sleep 30 & wait"])
    _, _, timed_out = communicate_bounded(proc, timeout=0.3, grace=5.0)
    assert timed_out and proc.returncode is not None
    assert wait_group_gone(proc.pid, 5.0)  # the group this call created is gone


def test_communicate_bounded_closes_pipes_a_stray_process_holds_open() -> None:
    holder = (
        "import os, time\nif os.fork() == 0:\n    os.setsid()\n    time.sleep(3)\n    os._exit(0)\n"
    )
    proc = spawn([sys.executable, "-I", "-c", holder])
    _, _, timed_out = communicate_bounded(proc, timeout=0.3, grace=0.3)
    assert timed_out and proc.returncode is not None
    assert proc.stdout is not None and proc.stdout.closed  # not left to block a later read


def test_wait_group_gone_waits_for_the_group_and_is_bounded() -> None:
    proc = spawn(["sh", "-c", "sleep 30"])
    try:
        assert wait_group_gone(proc.pid, 0.2) is False  # still running: bounded, not stuck
    finally:
        os.killpg(proc.pid, signal.SIGKILL)  # the group this test started
        proc.communicate(timeout=10)
    assert wait_group_gone(proc.pid, 5.0) is True


# --- the plumbing, driven locally ----------------------------------------------------


def test_window_around_now_contains_now() -> None:
    now = 1_700_000_000.0  # 22:13 UTC
    assert window_around_now(now) == ("21:13", "00:13")
    start, end = window_around_now(now, before_h=0, after_h=1)
    assert (start, end) == ("22:13", "23:13")


def test_the_router_child_runs_in_a_private_home_with_a_clock_offset(tmp_path: Path) -> None:
    """The live quiet-hours test, with a local script as the destination: a `warn` inside a
    window is deferred; a `flush` whose wall clock is moved past the window end delivers it. The
    same child, home and offset the live tests use; no notifier, no network."""
    scratch = tmp_path / "s"
    scratch.mkdir(mode=0o700)
    home = scratch / "home"
    home.mkdir(mode=0o700)
    got = scratch / "got.json"
    script = scratch / "dest.sh"
    script.write_text(f"#!/bin/sh\ncat > {got}\nexit 0\n")
    script.chmod(0o700)
    router = LiveRouter(home, str(scratch), {"PATH": "/usr/bin:/bin"})
    start, end = window_around_now(time.time())
    router.write_config({
        "destinations": {"local": {"kind": "generic", "cmd": [str(script)], "quiet": "live"}},
        "quiet_hours": {"live": {"timezone": "UTC", "start": start, "end": end}},
        "rules": [{"name": "live", "match": {"source": "live-test", "level_min": "info"},
                   "notify": ["local"]}],
    })  # fmt: skip
    try:
        sent = router.send("TEST MESSAGE local", title="notice-route live test", level="warn")
        assert sent.code == 0, sent.err
        (entry,) = sent.json()["destinations"]
        assert (entry["outcome"], entry["reason"]) == ("deferred", "quiet")
        assert not got.exists()
        offset = max(0.0, entry["deliver_after"] - time.time()) + 90.0
        flushed = router.run("flush", "--json", offset=offset)
        assert flushed.code == 0, flushed.err
        assert json.loads(got.read_text())["message"] == "TEST MESSAGE local"
        listed = router.run("history", "--json")
        assert [r["outcome"] for r in listed.json()] == ["sent"]
    finally:
        router.finish()
    assert (home / ".local/state/notice-route/state.db").exists()
