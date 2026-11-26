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
    TOOLS,
    LiveRouter,
    Protected,
    Report,
    opted_in,
    tool_config,
    window_around_now,
    wsl_ready,
)

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "tests" / "live"
LIVE_FILES = sorted(p for p in LIVE.glob("*.py"))
CANARY = "canary-value-9f3b2c71"


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


def imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            found |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            found.add(n.module.split(".")[0])
    return found


def test_no_live_module_imports_a_network_library() -> None:
    banned = {
        "socket",
        "ssl",
        "http",
        "urllib",
        "urllib3",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
    }
    for path in LIVE_FILES:
        assert not imported_modules(ast.parse(path.read_text())) & banned, path.name


def test_the_live_suite_never_names_crontab_and_has_no_guard_bypass() -> None:
    for path in LIVE_FILES:
        tree = ast.parse(path.read_text())
        assert "crontab_guard" not in imported_modules(tree) | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        }, path.name
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
