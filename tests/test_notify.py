"""The notifier call (#19): argv, scrubbed environment, stdin payload and the spawn.

Real children are the fake notifier of ``tests/fakebin``, which logs its argv, its environment
and its stdin. Untrusted text appears only in the payload; canary strings prove it never reaches
argv or the environment. Credential-like values use neutral canary names.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from notice_route import notify
from notice_route.gateway import STDERR_LIMIT, STDOUT_LIMIT, FakeClock, Gateway
from notice_route.notify import (
    DEADLINE_EXTRA,
    FIXED_NAMES,
    CallResult,
    build_argv,
    build_env,
    call_deadline,
    call_notifier,
    notification_json,
)
from notice_route.report import worst_case_s
from tests import sibling_schemas as ss
from tests.decide_support import dest, make_config
from tests.fakes import CANONICAL, Notifier

Make = Callable[..., Notifier]
DIGEST = hashlib.sha256(b"x").hexdigest()
CANARY = "CANARY_7f3a9c"
HOSTILE_ENV = {
    "HOME": "/home/CANARY_home",
    "XDG_CONFIG_HOME": "/home/CANARY_home/.config",
    "SSH_AUTH_SOCK": "/tmp/CANARY_ssh",
    "SSH_CONNECTION": "CANARY_ssh_conn",
    "GITHUB_TOKEN": CANARY,
    "AWS_SECRET_ACCESS_KEY": CANARY,
    "HTTP_PROXY": "http://CANARY_proxy",
    "HTTPS_PROXY": "http://CANARY_proxy",
    "http_proxy": "http://CANARY_proxy",
    "NO_PROXY": "CANARY",
    "LD_PRELOAD": "/CANARY/evil.so",
    "LD_LIBRARY_PATH": "/CANARY",
    "PYTHONPATH": "/CANARY",
    "PYTHONSTARTUP": "/CANARY/x.py",
    "PYTHONHOME": "/CANARY",
    "NOTICE_ROUTE_HOOK": "1",
    "TMPDIR": "/CANARY",
    "SHELL": "/bin/CANARY",
    "USER": "CANARY_user",
    "LOGNAME": "CANARY_user",
    "LC_MESSAGES": "CANARY",
    "LC_CTYPE": "CANARY",
    "BASH_ENV": "/CANARY",
    "IFS": "CANARY",
}


def gateway_for(base: Path, environ: Mapping[str, str] | None = None, **kw: Any) -> Gateway:
    """A gateway whose environment is ``environ`` and whose exec check stops at ``base``."""
    base.chmod(0o755)  # the umask of the test run may leave it group-writable
    env = {"PATH": str(base), **HOSTILE_ENV, **(environ or {})}
    return Gateway(environ=lambda: env, exec_check_from=str(base), **kw)


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """The test process's own HOME and XDG point at a private directory."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


def go(
    n: Notifier,
    *,
    environ: Mapping[str, str] | None = None,
    kind: str = "slack-notice",
    env: tuple[str, ...] = (),
    title: str = "t",
    message: str = "m",
    level: str = "info",
    link: str = "",
    key: str | None = None,
    timeout: float = 10.0,
    cmd_extra: tuple[str, ...] = (),
    gw: Gateway | None = None,
) -> CallResult:
    destination = dest("d", kind=kind, cmd=(*n.cmd, *cmd_extra), env=env, timeout=timeout)
    return call_notifier(
        gw or gateway_for(n.stub.parent, environ),
        destination,
        title=title,
        message=message,
        level=level,
        link=link,
        dedupe_key=key,
    )


def invoke(n: Notifier) -> dict[str, Any]:
    (record,) = n.events("invoke")
    return record


def sent_payload(n: Notifier) -> dict[str, Any]:
    (record,) = n.events("stdin")
    return json.loads(record["text"])


# --- the notification JSON -------------------------------------------------------------------


def test_json_holds_only_the_present_fields() -> None:
    body = json.loads(notification_json(title="", message="m", level="warn", link=""))
    assert body == {"message": "m", "level": "warn"}


def test_json_holds_all_four_fields() -> None:
    text = notification_json(title="T", message="M", level="error", link="https://h.example/x")
    assert json.loads(text) == {
        "title": "T",
        "message": "M",
        "level": "error",
        "link": "https://h.example/x",
    }


def test_json_never_writes_null() -> None:
    assert "null" not in notification_json(title="", message="", level="info", link="")


@pytest.mark.parametrize("level", ["", "loud", "INFO", "info\n", None, 3])
def test_json_refuses_a_level_outside_the_four(level: Any) -> None:
    with pytest.raises(ValueError):
        notification_json(title="t", message="m", level=level, link="")


@pytest.mark.parametrize("bad", [None, 3, b"x", ["a"]])
def test_json_refuses_a_non_string_field(bad: Any) -> None:
    for field in ("title", "message", "link"):
        fields: dict[str, Any] = {"title": "t", "message": "m", "level": "info", "link": ""}
        fields[field] = bad
        with pytest.raises(ValueError):
            notification_json(**fields)


HOSTILE_TEXTS = [
    "line one\nline two\r\nthree",
    "nul\x00inside",
    'quote " backslash \\ and \\u0041',
    "{{title}} ${HOME} $(id) `id` ; rm -rf / --flag -n",
    "snowman ☃ astral \U0001f600 rtl ‮ zwj ‍",
    "\ud800 lone surrogate",
    "   separators",
    "x" * 100_000,
    "",
]


@pytest.mark.parametrize("text", HOSTILE_TEXTS)
def test_json_round_trips_hostile_text_exactly(text: str) -> None:
    out = notification_json(title=text, message=text + "!", level="info", link="")
    assert out.isascii()
    body = json.loads(out)
    assert body.get("title", "") == text
    assert body["message"] == text + "!"
    assert "\n" not in out


def test_json_output_is_a_single_line_of_ascii() -> None:
    out = notification_json(title="a\nb", message="c\x00d☃", level="info", link="")
    assert out.isascii() and "\n" not in out and "\x00" not in out


# --- argv ------------------------------------------------------------------------------------


def test_argv_is_the_executable_cmd_rest_and_the_fixed_flags() -> None:
    argv = build_argv("/opt/bin/real", ("/opt/link", "--config", "/etc/n.json"), None)
    assert argv == ["/opt/bin/real", "--config", "/etc/n.json", *CANONICAL]


def test_argv_adds_the_dedupe_key_in_the_equals_form() -> None:
    argv = build_argv("/x", ("/x",), DIGEST)
    assert argv[-1] == f"--dedupe-key={DIGEST}"
    assert argv[:-1] == ["/x", *CANONICAL]


@pytest.mark.parametrize(
    "key",
    ["", "ZZ", "A" * 64, DIGEST + "0", "ab cd", DIGEST[:63], "--x", "ab\n", "0" * 129, 5, b"ab"],
)
def test_argv_refuses_a_dedupe_key_that_is_not_lowercase_hex(key: Any) -> None:
    with pytest.raises(ValueError):
        build_argv("/x", ("/x",), key)


def test_argv_refuses_an_empty_cmd() -> None:
    with pytest.raises(ValueError):
        build_argv("/x", (), None)


def test_argv_has_no_room_for_payload_text() -> None:
    """``build_argv`` takes no title, message, level or link."""
    import inspect

    assert list(inspect.signature(build_argv).parameters) == ["executable", "cmd", "dedupe_key"]


# --- the environment -------------------------------------------------------------------------


def _clean_dir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    return path


def test_env_is_exactly_the_fixed_set_for_a_plain_destination(tmp_path: Path) -> None:
    base = _clean_dir(tmp_path / "b")
    built = build_env(gateway_for(base), dest("d"))
    assert built.missing == ()
    assert built.env == {"PATH": str(base), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def test_env_never_carries_anything_from_the_callers_environment(tmp_path: Path) -> None:
    base = _clean_dir(tmp_path / "b")
    built = build_env(gateway_for(base), dest("d", kind="wsl-notice"))
    assert set(built.env) <= set(FIXED_NAMES)
    assert CANARY not in "".join(built.env.values())
    assert "CANARY" not in "".join(built.env.values())


def test_env_adds_the_named_variables_with_the_routers_values(tmp_path: Path) -> None:
    base = _clean_dir(tmp_path / "b")
    gw = gateway_for(base, {"CRED_ONE": "v1", "CRED_TWO": "v 2 = x"})
    built = build_env(gw, dest("d", env=("CRED_ONE", "CRED_TWO")))
    assert built.env["CRED_ONE"] == "v1" and built.env["CRED_TWO"] == "v 2 = x"
    assert set(built.env) == {"PATH", "LANG", "LC_ALL", "CRED_ONE", "CRED_TWO"}


def test_env_reports_unset_and_empty_names_and_does_not_pass_them(tmp_path: Path) -> None:
    base = _clean_dir(tmp_path / "b")
    gw = gateway_for(base, {"CRED_ONE": "", "CRED_THREE": "v"})
    built = build_env(gw, dest("d", env=("CRED_ONE", "CRED_TWO", "CRED_THREE")))
    assert built.missing == ("CRED_ONE", "CRED_TWO")
    assert "CRED_ONE" not in built.env and "CRED_TWO" not in built.env


def test_a_value_holding_nul_counts_as_unset(tmp_path: Path) -> None:
    base = _clean_dir(tmp_path / "b")
    gw = gateway_for(base, {"CRED_ONE": "a\x00b"})
    built = build_env(gw, dest("d", env=("CRED_ONE",)))
    assert built.missing == ("CRED_ONE",)


@pytest.mark.parametrize(
    "name", [*FIXED_NAMES, "", "1A", "A-B", "A=B", "A B", "A\x00", "é", "A" * 129]
)
def test_env_refuses_a_name_outside_the_grammar_or_in_the_fixed_set(
    tmp_path: Path, name: str
) -> None:
    base = _clean_dir(tmp_path / "b")
    with pytest.raises(ValueError):
        build_env(gateway_for(base, {name: "v"}), dest("d", env=(name,)))


def test_env_error_text_does_not_hold_values(tmp_path: Path) -> None:
    base = _clean_dir(tmp_path / "b")
    with pytest.raises(ValueError) as caught:
        build_env(gateway_for(base, {"PATH": CANARY}), dest("d", env=("PATH",)))
    assert CANARY not in str(caught.value)


def test_wsl_variables_pass_only_for_wsl_notice(tmp_path: Path) -> None:
    base = _clean_dir(tmp_path / "b")
    gw = gateway_for(base, {"WSL_INTEROP": "/run/WSL/1_interop", "WSL_DISTRO_NAME": "Deb"})
    wsl = build_env(gw, dest("d", kind="wsl-notice")).env
    assert wsl["WSL_INTEROP"] == "/run/WSL/1_interop" and wsl["WSL_DISTRO_NAME"] == "Deb"
    for kind in ("slack-notice", "ha-notice", "generic"):
        other = build_env(gw, dest("d", kind=kind)).env
        assert "WSL_INTEROP" not in other and "WSL_DISTRO_NAME" not in other


def test_wsl_variables_are_skipped_when_unset_empty_or_hold_nul(tmp_path: Path) -> None:
    base = _clean_dir(tmp_path / "b")
    for value in ("", "a\x00b"):
        gw = gateway_for(base, {"WSL_INTEROP": value})
        env = build_env(gw, dest("d", kind="wsl-notice")).env
        assert "WSL_INTEROP" not in env and "WSL_DISTRO_NAME" not in env
    assert build_env(gateway_for(base), dest("d", kind="wsl-notice")).missing == ()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("en_US.UTF-8", "en_US.UTF-8"),
        ("C.UTF-8", "C.UTF-8"),
        ("de_DE.utf8", "de_DE.utf8"),
        ("de_DE.UTF-8@euro", "de_DE.UTF-8@euro"),
        ("C", "C.UTF-8"),
        ("POSIX", "C.UTF-8"),
        ("en_US.ISO-8859-1", "C.UTF-8"),
        ("", "C.UTF-8"),
        ("en_US.UTF-8\n", "C.UTF-8"),
        ("en_US.UTF-8 ", "C.UTF-8"),
        ("../../x.UTF-8", "C.UTF-8"),
        ("en_US.UTF-8/../x", "C.UTF-8"),
        ("x" * 200 + ".UTF-8", "C.UTF-8"),
        ("\x00.UTF-8", "C.UTF-8"),
        ("en_US.UTF-16", "C.UTF-8"),
    ],
)
def test_locale_is_kept_only_when_it_names_a_utf8_locale(
    tmp_path: Path, value: str, expected: str
) -> None:
    base = _clean_dir(tmp_path / "b")
    env = build_env(gateway_for(base, {"LANG": value, "LC_ALL": value}), dest("d")).env
    assert env["LANG"] == expected and env["LC_ALL"] == expected


def test_lang_and_lc_all_are_judged_separately(tmp_path: Path) -> None:
    base = _clean_dir(tmp_path / "b")
    gw = gateway_for(base, {"LANG": "en_US.UTF-8", "LC_ALL": "C"})
    env = build_env(gw, dest("d")).env
    assert env["LANG"] == "en_US.UTF-8" and env["LC_ALL"] == "C.UTF-8"


def test_locale_is_set_even_when_the_router_has_none(tmp_path: Path) -> None:
    base = _clean_dir(tmp_path / "b")
    gw = Gateway(environ=lambda: {"PATH": str(base)}, exec_check_from=str(base))
    env = build_env(gw, dest("d")).env
    assert env["LANG"] == "C.UTF-8" and env["LC_ALL"] == "C.UTF-8"


def test_path_is_cleaned_and_falls_back_to_the_system_default(tmp_path: Path) -> None:
    good = _clean_dir(tmp_path / "good")
    wide = _clean_dir(tmp_path / "wide", 0o777)
    path = f":{wide}:rel/bin::{good}:{tmp_path}/nope"
    gw = Gateway(environ=lambda: {"PATH": path}, exec_check_from=str(tmp_path))
    assert build_env(gw, dest("d")).env["PATH"] == str(good)
    for value in ("", f"{wide}:rel", None):
        env = {} if value is None else {"PATH": value}
        gw = Gateway(environ=lambda env=env: env, exec_check_from=str(tmp_path))
        assert build_env(gw, dest("d")).env["PATH"] == "/usr/bin:/bin"
    wide.chmod(0o700)


# --- the deadline and the worst case ---------------------------------------------------------


@pytest.mark.parametrize("timeout", [1.0, 10.0, 59.5, 120.0])
def test_deadline_is_the_destination_timeout_plus_ten(timeout: float) -> None:
    assert call_deadline(dest("d", timeout=timeout)) == timeout + 10.0
    assert DEADLINE_EXTRA == 10.0


@pytest.mark.parametrize(
    ("lock_wait", "timeouts"),
    [(2.0, (10.0,)), (1.0, (1.0, 3.0)), (60.0, (120.0, 5.0)), (2.0, ())],
)
def test_worst_case_is_lock_wait_plus_largest_timeout_plus_seventeen(
    lock_wait: float, timeouts: tuple[float, ...]
) -> None:
    from dataclasses import replace

    from notice_route.gateway import FINAL_READ, TERM_GRACE

    config = replace(
        make_config(*(dest(f"d{i}", timeout=t) for i, t in enumerate(timeouts))),
        lock_wait=lock_wait,
    )
    parts = DEADLINE_EXTRA + TERM_GRACE + FINAL_READ + 1.0  # the last is the database
    assert parts == 17.0
    assert worst_case_s(config) == lock_wait + max(timeouts, default=0.0) + 17.0


def test_the_deadline_reaches_the_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_run(self: Gateway, argv: Any, *, env: Any, timeout: float, stdin: Any = None) -> Any:
        seen.update(argv=list(argv), env=dict(env), timeout=timeout, stdin=stdin)
        raise ValueError("stop")

    base = _clean_dir(tmp_path / "b")
    exe = base / "tool"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o700)
    monkeypatch.setattr(Gateway, "run", fake_run)
    result = call_notifier(
        gateway_for(base),
        dest("d", cmd=(str(exe),), timeout=7.0),
        title="t",
        message="m",
        level="info",
        link="",
        dedupe_key=None,
    )
    assert seen["timeout"] == 17.0
    assert result.status == "not_started"
    assert isinstance(seen["stdin"], str) and json.loads(seen["stdin"])["message"] == "m"


# --- the call against the fake notifier ------------------------------------------------------


def test_the_child_sees_the_documented_argv(make_notifier: Make, isolated: Path) -> None:
    n = make_notifier()
    result = go(n, key=DIGEST, cmd_extra=("--config", "/etc/n.json"))
    assert result.status == "ran" and result.run is not None
    argv = invoke(n)["argv"]
    assert argv == ["--config", "/etc/n.json", *CANONICAL, f"--dedupe-key={DIGEST}"]


def test_the_child_sees_no_dedupe_key_for_a_digest(make_notifier: Make, isolated: Path) -> None:
    n = make_notifier()
    go(n)
    assert "--dedupe-key" not in " ".join(invoke(n)["argv"])


def test_the_child_environment_is_exactly_the_allow_list(
    make_notifier: Make, isolated: Path
) -> None:
    n = make_notifier()
    result = go(n, environ={"CRED_ONE": "one-value", "LANG": "en_US.UTF-8"}, env=("CRED_ONE",))
    assert result.status == "ran"
    env = invoke(n)["env"]
    assert set(env) == {"PATH", "LANG", "LC_ALL", "CRED_ONE"}
    assert env["CRED_ONE"] == "one-value" and env["LANG"] == "en_US.UTF-8"
    assert "CANARY" not in json.dumps(env)


def test_a_wsl_notice_child_gets_the_wsl_variables(make_notifier: Make, isolated: Path) -> None:
    n = make_notifier("wsl-notice")
    go(n, kind="wsl-notice", environ={"WSL_INTEROP": "/run/WSL/9_interop", "WSL_DISTRO_NAME": "D"})
    assert set(invoke(n)["env"]) == {"PATH", "LANG", "LC_ALL", "WSL_INTEROP", "WSL_DISTRO_NAME"}


def test_the_child_starts_in_its_own_session_in_root_with_closed_descriptors(
    make_notifier: Make, isolated: Path
) -> None:
    n = make_notifier()
    go(n)
    rec = invoke(n)
    assert rec["cwd"] == "/" and rec["sid"] == rec["pid"] == rec["pgid"]
    assert rec["pid"] != os.getpid() and rec["sid"] != os.getsid(0)
    assert sorted(rec["open_fds"]) == [0, 1, 2]


def test_no_payload_text_reaches_argv_or_environment(make_notifier: Make, isolated: Path) -> None:
    n = make_notifier()
    canary = "PAYLOAD_CANARY_91d2"
    go(
        n,
        title=f"{canary} t",
        message=f"{canary} $(id) --flag\n{canary}",
        link=f"https://h.example/{canary}",
        level="warn",
        key=DIGEST,
        environ={"CRED_ONE": "v"},
        env=("CRED_ONE",),
    )
    rec = invoke(n)
    assert canary not in json.dumps(rec["argv"]) and canary not in json.dumps(rec["env"])
    assert canary in sent_payload(n)["message"]


@pytest.mark.parametrize("tool", ["slack-notice", "ha-notice"])
def test_the_payload_validates_against_each_pinned_notification_schema(
    make_notifier: Make, isolated: Path, tool: str
) -> None:
    n = make_notifier(tool, tool=tool)
    result = go(
        n,
        kind=tool,
        title="Build done",
        message="all green\nsecond line",
        level="action",
        link="https://h.example/pull/3",
        key=DIGEST,
    )
    assert result.status == "ran" and result.run is not None and result.run.returncode == 0
    body = sent_payload(n)
    ss.validate(body, tool, "notification.json")
    assert body == {
        "title": "Build done",
        "message": "all green\nsecond line",
        "level": "action",
        "link": "https://h.example/pull/3",
    }
    envelope = json.loads(result.run.stdout)
    ss.validate(envelope, tool, "cli/send.json")


@pytest.mark.parametrize("tool", ["slack-notice", "ha-notice"])
def test_a_minimal_payload_omits_absent_fields(
    make_notifier: Make, isolated: Path, tool: str
) -> None:
    n = make_notifier(tool, tool=tool)
    go(n, kind=tool, title="", link="", message="only")
    body = sent_payload(n)
    assert body == {"message": "only", "level": "info"}
    ss.validate(body, tool, "notification.json")


@pytest.mark.parametrize("text", [t for t in HOSTILE_TEXTS if t and len(t) < 1000])
def test_hostile_text_round_trips_through_stdin(
    make_notifier: Make, isolated: Path, text: str
) -> None:
    n = make_notifier()
    go(n, title="t", message=text or "m")
    assert sent_payload(n)["message"] == (text or "m")
    rec = invoke(n)
    assert text not in json.dumps(rec["argv"]) or text == ""


def test_a_huge_message_is_written_whole_and_the_call_ends(
    make_notifier: Make, isolated: Path
) -> None:
    n = make_notifier()
    result = go(n, title="", message="y" * 300_000)
    stdin = n.events("stdin")[0]
    expected = notification_json(title="", message="y" * 300_000, level="info", link="")
    assert stdin["size"] == len(expected.encode())
    assert result.status == "ran" and result.run is not None


def test_a_destination_with_an_unset_variable_is_not_called(
    make_notifier: Make, isolated: Path
) -> None:
    n = make_notifier()
    result = go(n, env=("CRED_ONE", "CRED_TWO"), environ={"CRED_TWO": ""})
    assert result.status == "env_missing" and result.run is None
    assert result.missing == ("CRED_ONE", "CRED_TWO")
    assert n.events("invoke") == []


# --- cmd[0] -----------------------------------------------------------------------------------


def test_the_resolved_path_is_executed_not_the_link(make_notifier: Make, isolated: Path) -> None:
    n = make_notifier()
    link = n.stub.parent / "via-link"
    link.symlink_to(n.stub)
    destination = dest("d", cmd=(str(link),))
    result = call_notifier(
        gateway_for(n.stub.parent),
        destination,
        title="t",
        message="m",
        level="info",
        link="",
        dedupe_key=None,
    )
    assert result.status == "ran"
    assert n.events("invoke")


def test_a_symlink_to_a_group_writable_file_ends_exec_failed(
    make_notifier: Make, isolated: Path
) -> None:
    n = make_notifier()
    n.stub.chmod(0o775)
    link = n.stub.parent / "via-link"
    link.symlink_to(n.stub)
    result = go(n, gw=gateway_for(n.stub.parent), cmd_extra=())
    assert result.status == "exec_failed" and result.run is None
    assert n.events("invoke") == []
    linked = call_notifier(
        gateway_for(n.stub.parent),
        dest("d", cmd=(str(link),)),
        title="t",
        message="m",
        level="info",
        link="",
        dedupe_key=None,
    )
    assert linked.status == "exec_failed"


@pytest.mark.parametrize("mode", [0o4755, 0o2755, 0o757])
def test_a_setuid_setgid_or_world_writable_file_ends_exec_failed(
    make_notifier: Make, isolated: Path, mode: int
) -> None:
    n = make_notifier()
    n.stub.chmod(mode)
    assert go(n).status == "exec_failed"
    assert n.events("invoke") == []


def test_a_file_in_a_group_writable_directory_ends_exec_failed(
    make_notifier: Make, isolated: Path
) -> None:
    n = make_notifier()
    sub = n.stub.parent / "sub"
    sub.mkdir()
    moved = sub / "tool"
    moved.write_bytes(n.stub.read_bytes())
    moved.chmod(0o755)
    sub.chmod(0o775)
    result = call_notifier(
        gateway_for(n.stub.parent),
        dest("d", cmd=(str(moved),)),
        title="t",
        message="m",
        level="info",
        link="",
        dedupe_key=None,
    )
    assert result.status == "exec_failed"
    sub.chmod(0o755)


def test_a_missing_command_ends_exec_failed(tmp_path: Path, isolated: Path) -> None:
    base = _clean_dir(tmp_path / "b")
    result = call_notifier(
        gateway_for(base),
        dest("d", cmd=(str(base / "missing"),)),
        title="t",
        message="m",
        level="info",
        link="",
        dedupe_key=None,
    )
    assert result.status == "exec_failed"


def test_a_file_that_cannot_be_executed_ends_exec_failed(tmp_path: Path, isolated: Path) -> None:
    base = _clean_dir(tmp_path / "b")
    plain = base / "plain"
    plain.write_text("not a program")
    plain.chmod(0o600)  # passes the safety check, has no execute bit
    result = call_notifier(
        gateway_for(base),
        dest("d", cmd=(str(plain),)),
        title="t",
        message="m",
        level="info",
        link="",
        dedupe_key=None,
    )
    assert result.status == "exec_failed" and result.run is None


def test_the_exec_failed_result_carries_no_exception_text(tmp_path: Path, isolated: Path) -> None:
    base = _clean_dir(tmp_path / "b")
    plain = base / "plain_CANARY_name"
    plain.write_text("x")
    plain.chmod(0o600)
    result = call_notifier(
        gateway_for(base),
        dest("d", cmd=(str(plain),)),
        title="t",
        message="m",
        level="info",
        link="",
        dedupe_key=None,
    )
    assert "CANARY" not in repr(result)


# --- every Gateway.run result and exception ---------------------------------------------------


def test_the_exit_code_is_reported_for_an_exit(make_notifier: Make, isolated: Path) -> None:
    n = make_notifier(exit=12, error={"code": "unavailable", "retryable": True})
    result = go(n)
    assert result.status == "ran" and result.run is not None
    assert result.run.status == "exited" and result.run.returncode == 12


def test_a_signal_death_is_reported(make_notifier: Make, isolated: Path) -> None:
    n = make_notifier(die_by_signal="KILL")
    result = go(n)
    assert result.run is not None and result.run.status == "signaled"
    assert result.run.signal == signal.SIGKILL


def test_a_timeout_kills_the_call_at_the_deadline(make_notifier: Make, isolated: Path) -> None:
    n = make_notifier(hang=True)
    clock = FakeClock()
    gw = gateway_for(n.stub.parent, clock=clock)
    import threading
    import time

    stop = threading.Event()

    def tick() -> None:
        while not stop.is_set():
            clock.advance_monotonic(1.0)
            time.sleep(0.01)

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()
    try:
        result = go(n, gw=gw, timeout=1.0)
    finally:
        stop.set()
        thread.join(5)
    assert result.run is not None and result.run.status == "timeout"


def test_an_output_overflow_is_reported_for_each_stream(
    make_notifier: Make, isolated: Path
) -> None:
    n = make_notifier(stdout_bytes=STDOUT_LIMIT + 10)
    first = go(n)
    assert first.run is not None
    assert first.run.status == "overflow" and first.run.overflow == "stdout"
    assert len(first.run.stdout) <= STDOUT_LIMIT
    n.set(stderr_bytes=STDERR_LIMIT + 10)
    second = go(n)
    assert second.run is not None
    assert second.run.status == "overflow" and second.run.overflow == "stderr"
    assert len(second.run.stderr) <= STDERR_LIMIT


def test_a_run_result_never_carries_the_spawn_error_text(
    monkeypatch: pytest.MonkeyPatch, make_notifier: Make, isolated: Path
) -> None:
    import subprocess

    def boom(*_a: object, **_k: object) -> None:
        raise PermissionError(13, f"denied {CANARY}")

    monkeypatch.setattr(subprocess.Popen, "__init__", boom)
    n = make_notifier()
    result = go(n)
    assert result.status == "exec_failed" and result.run is None
    assert CANARY not in repr(result)


def test_value_errors_from_run_map_to_not_started_without_text(
    monkeypatch: pytest.MonkeyPatch, make_notifier: Make, isolated: Path
) -> None:
    n = make_notifier()
    result = go(n, cmd_extra=("bad\x00arg",))
    assert result.status == "not_started" and result.run is None
    assert n.events("invoke") == []
    assert "bad" not in repr(result)


def test_an_ignored_sigchld_maps_to_not_started(make_notifier: Make, isolated: Path) -> None:
    n = make_notifier()
    old = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        result = go(n)
    finally:
        signal.signal(signal.SIGCHLD, old)
    assert result.status == "not_started" and n.events("invoke") == []


def test_a_lost_child_maps_to_lost(
    monkeypatch: pytest.MonkeyPatch, make_notifier: Make, isolated: Path
) -> None:
    from notice_route import gateway as gw_mod

    def gone(*_a: object) -> None:
        raise ChildProcessError

    monkeypatch.setattr(gw_mod.os, "waitid", gone)
    n = make_notifier(sleep=0.2)
    result = go(n)
    assert result.status == "lost" and result.run is None
    assert "ChildProcess" not in repr(result)


def test_a_dedupe_key_that_is_not_hex_is_not_started(make_notifier: Make, isolated: Path) -> None:
    n = make_notifier()
    assert go(n, key="--evil").status == "not_started"
    assert n.events("invoke") == []


def test_a_bad_level_is_not_started(make_notifier: Make, isolated: Path) -> None:
    n = make_notifier()
    assert go(n, level="loud").status == "not_started"
    assert n.events("invoke") == []


def test_the_module_reads_no_global_environment(
    make_notifier: Make, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child's environment comes from ``gateway.environ()`` only."""
    monkeypatch.setenv("CRED_ONE", "from-the-process")
    n = make_notifier()
    result = go(n, env=("CRED_ONE",), environ={})
    assert result.status == "env_missing"


def test_the_module_exports_what_later_issues_need() -> None:
    assert {"call_notifier", "CallResult", "build_argv", "build_env"} <= set(notify.__all__)
    assert sys.version_info >= (3, 12)
