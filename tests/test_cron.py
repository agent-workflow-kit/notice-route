"""``cron install`` and ``cron uninstall`` (#28), through the real CLI with the fake crontab."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from notice_route import gateway as gateway_module
from tests.cron_support import Rig
from tests.fakecron import FakeCrontab
from tests.test_cli_rules_validate import schema_errors

CANARY = "canary-crontab-text-5e2b"
START = "# notice-route managed v1 flush=1m"
END = "# notice-route managed end"
FOREIGN = (
    b"# my own jobs\r\n"
    b"MAILTO=me@example.invalid\n"
    b"PATH=/opt/foo/bin\n"
    b"\n"
    b"0 4 * * * /usr/local/bin/backup --all # nightly\n"
    b"@reboot echo \xff\xfe not utf-8\r\n"
)


@pytest.fixture
def rig(
    home: Path,
    tmp_path: Path,
    fake_crontab: FakeCrontab,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> Rig:
    monkeypatch.setenv("HOME", str(home))
    prog = tmp_path / "prog"
    prog.mkdir()
    prog.chmod(0o755)
    exe = prog / "notice-route"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return Rig(home, tmp_path, exe, fake_crontab, capsys)


def block_of(rig: Rig, env_file: Path | None = None) -> bytes:
    return rig.expected_block(env_file).encode()


# --- install ---------------------------------------------------------------------------------


def test_install_on_an_absent_crontab_writes_the_golden_block(rig: Rig) -> None:
    code, doc = rig.run_json("install")
    assert code == 0
    assert doc == {
        "action": "installed",
        "block": rig.expected_block(),
        "dry_run": False,
        "env_file": None,
    }
    assert rig.crontab.table() == block_of(rig)
    assert schema_errors(doc, "cron-install.json") == []


def test_install_with_an_env_file_writes_the_golden_block(rig: Rig) -> None:
    env_file = rig.env_file()
    rig.config({"cron": {"env_file": str(env_file)}})
    code, doc = rig.run_json("install")
    assert code == 0 and doc["env_file"] == str(env_file)
    assert rig.crontab.table() == block_of(rig, env_file)
    assert schema_errors(doc, "cron-install.json") == []


def test_install_reads_twice_then_writes_once_through_crontab_dash(rig: Rig) -> None:
    assert rig.run("install")[0] == 0
    assert rig.crontab.kinds() == ["list", "list", "write"]
    assert [c["argv"] for c in rig.crontab.calls()] == [["-l"], ["-l"], ["-"]]
    assert len(rig.crontab.writes()) == 1


def test_crontab_runs_with_a_scrubbed_environment(rig: Rig) -> None:
    gw = rig.gateway(environ={"NOTICE_ROUTE_HOOK": "1", "CANARY_VAR": CANARY, "HOME": "/nope"})
    assert rig.run("install", gateway=gw)[0] == 0
    for call in rig.crontab.calls():
        env = call["env"]
        assert env["LC_ALL"] == "C"
        assert env["PATH"].startswith(str(rig.crontab.bin_dir))
        assert not {"NOTICE_ROUTE_HOOK", "CANARY_VAR", "HOME"} & set(env)
    assert rig.crontab.table() is not None
    assert b"NOTICE_ROUTE_HOOK" not in rig.crontab.table()  # type: ignore[operator]


def test_install_keeps_foreign_lines_byte_for_byte_and_appends(rig: Rig) -> None:
    rig.crontab.set_table(FOREIGN)
    assert rig.run("install")[0] == 0
    assert rig.crontab.table() == FOREIGN + block_of(rig)


def test_install_adds_a_newline_when_the_last_foreign_line_has_none(rig: Rig) -> None:
    rig.crontab.set_table(b"0 1 * * * a-job")
    assert rig.run("install")[0] == 0
    assert rig.crontab.table() == b"0 1 * * * a-job\n" + block_of(rig)


def test_install_into_an_empty_crontab(rig: Rig) -> None:
    rig.crontab.set_table(b"")
    code, doc = rig.run_json("install")
    assert code == 0 and doc["action"] == "installed"
    assert rig.crontab.table() == block_of(rig)


def test_install_twice_is_unchanged_and_writes_nothing(rig: Rig) -> None:
    rig.crontab.set_table(FOREIGN)
    assert rig.run("install")[0] == 0
    before = rig.crontab.table()
    writes = len(rig.crontab.writes())
    rig.crontab.state.joinpath("calls.jsonl").unlink()
    code, doc = rig.run_json("install")
    assert code == 0 and doc["action"] == "unchanged"
    assert doc["block"] == rig.expected_block()
    assert rig.crontab.table() == before
    assert len(rig.crontab.writes()) == writes
    assert rig.crontab.kinds() == ["list"]


def test_a_changed_config_replaces_the_block_in_place(rig: Rig) -> None:
    before = b"0 1 * * * first\n"
    after = b"# trailing comment\n0 2 * * * last\n"
    rig.crontab.set_table(before + after)
    assert rig.run("install")[0] == 0
    # the block is appended; move foreign lines around it to see an in-place replacement
    rig.crontab.set_table(before + block_of(rig) + after)
    env_file = rig.env_file()
    rig.config({"cron": {"env_file": str(env_file)}})
    code, doc = rig.run_json("install")
    assert code == 0 and doc["action"] == "updated"
    assert rig.crontab.table() == before + block_of(rig, env_file) + after
    # and back: dropping the env file updates it again
    rig.config({})
    code, doc = rig.run_json("install")
    assert doc["action"] == "updated" and doc["env_file"] is None
    assert rig.crontab.table() == before + block_of(rig) + after


def test_a_tampered_block_body_is_restored(rig: Rig) -> None:
    lines = block_of(rig).decode().split("\n")
    lines[2] = "* * * * * /bin/evil"
    rig.crontab.set_table("0 0 * * * x\n" + "\n".join(lines))
    code, doc = rig.run_json("install")
    assert code == 0 and doc["action"] == "updated"
    assert rig.crontab.table() == b"0 0 * * * x\n" + block_of(rig)


def test_any_version_of_the_start_line_is_replaced(rig: Rig) -> None:
    old = "# notice-route managed v0 old\n* * * * * old\n" + END + "\n"
    rig.crontab.set_table("a\n" + old + "b\n")
    code, doc = rig.run_json("install")
    assert code == 0 and doc["action"] == "updated"
    assert rig.crontab.table() == b"a\n" + block_of(rig) + b"b\n"


def test_crlf_and_trailing_blanks_on_marker_lines_still_match(rig: Rig) -> None:
    table = f"x\n{START} \t\r\n* * * * * old\r\n{END}\t\r\ny\n"
    rig.crontab.set_table(table)
    code, doc = rig.run_json("install")
    assert code == 0 and doc["action"] == "updated"
    assert rig.crontab.table() == b"x\n" + block_of(rig) + b"y\n"


def test_lines_that_only_look_like_markers_are_foreign(rig: Rig) -> None:
    lookalikes = (
        b"#notice-route managed v1\n"
        b" # notice-route managed v1\n"
        b"# notice-route managed ends\n"
        b"# notice-route managed\n"
        b"# Notice-Route managed v1\n"
        b"0 0 * * * echo # notice-route managed v1\n"
    )
    rig.crontab.set_table(lookalikes)
    code, doc = rig.run_json("install")
    assert code == 0 and doc["action"] == "installed"
    assert rig.crontab.table() == lookalikes + block_of(rig)


def test_install_preserves_a_nul_byte_in_a_foreign_line(rig: Rig) -> None:
    rig.crontab.set_table(b"0 0 * * * a\x00b\n")
    assert rig.run("install")[0] == 0
    assert rig.crontab.table() == b"0 0 * * * a\x00b\n" + block_of(rig)


# --- uninstall -------------------------------------------------------------------------------


def test_uninstall_removes_only_the_block(rig: Rig) -> None:
    before, after = b"0 1 * * * first\r\n", b"\n0 2 * * * last\n"
    rig.crontab.set_table(before + block_of(rig) + after)
    code, doc = rig.run_json("uninstall")
    assert code == 0
    assert doc == {"action": "removed", "block": rig.expected_block(), "dry_run": False}
    assert schema_errors(doc, "cron-uninstall.json") == []
    assert rig.crontab.table() == before + after
    assert rig.crontab.kinds() == ["list", "list", "write"]


def test_uninstall_of_the_only_content_leaves_an_empty_crontab(rig: Rig) -> None:
    rig.crontab.set_table(block_of(rig))
    assert rig.run("uninstall")[0] == 0
    assert rig.crontab.table() == b""
    assert rig.crontab.writes() == [b""]


def test_uninstall_twice_is_unchanged(rig: Rig) -> None:
    rig.crontab.set_table(FOREIGN + block_of(rig))
    assert rig.run("uninstall")[0] == 0
    assert rig.crontab.table() == FOREIGN
    rig.crontab.state.joinpath("calls.jsonl").unlink()
    code, doc = rig.run_json("uninstall")
    assert code == 0 and doc == {"action": "unchanged", "block": None, "dry_run": False}
    assert schema_errors(doc, "cron-uninstall.json") == []
    assert rig.crontab.kinds() == ["list"]
    assert rig.crontab.table() == FOREIGN


def test_uninstall_without_a_crontab_writes_nothing(rig: Rig) -> None:
    code, doc = rig.run_json("uninstall")
    assert code == 0 and doc["action"] == "unchanged"
    assert rig.crontab.table() is None and rig.crontab.writes() == []


def test_uninstall_leaves_a_newline_after_a_last_line_that_had_none(rig: Rig) -> None:
    rig.crontab.set_table(block_of(rig) + b"0 1 * * * last-without-newline")
    assert rig.run("uninstall")[0] == 0
    assert rig.crontab.table() == b"0 1 * * * last-without-newline\n"


def test_uninstall_reports_a_foreign_version_block_verbatim(rig: Rig) -> None:
    rig.crontab.set_table("# notice-route managed v2 x\nfoo\n" + END + "\n")
    code, doc = rig.run_json("uninstall")
    assert code == 0 and doc["action"] == "removed"
    assert doc["block"] == "# notice-route managed v2 x\nfoo\n" + END + "\n"
    assert rig.crontab.table() == b""


def test_uninstall_needs_neither_the_config_nor_a_safe_program(rig: Rig) -> None:
    rig.crontab.set_table(block_of(rig))
    rig.config({"cron": {"env_file": "relative/is/invalid"}})
    gw = rig.gateway(argv0="/nonexistent/prog with space")
    assert rig.run("uninstall", gateway=gw)[0] == 0
    assert rig.crontab.table() == b""


# --- malformed blocks ------------------------------------------------------------------------

S1, S2, E = START, "# notice-route managed v1 other", END
MALFORMED = {
    "two_starts_one_end": f"{S1}\n{S2}\n{E}\n",
    "one_start_two_ends": f"{S1}\n{E}\n{E}\n",
    "start_only": f"a\n{S1}\nb\n",
    "end_only": f"a\n{E}\nb\n",
    "end_first": f"{E}\nx\n{S1}\n",
    "two_blocks": f"{S1}\n{E}\n{S1}\n{E}\n",
    "nested": f"{S1}\n{S2}\n{E}\n{E}\n",
    "end_first_balanced_counts": f"{E}\n{S1}\n",
}


@pytest.mark.parametrize("command", ["install", "uninstall"])
@pytest.mark.parametrize("shape", sorted(MALFORMED))
def test_malformed_blocks_are_refused_untouched(rig: Rig, command: str, shape: str) -> None:
    rig.crontab.set_table(MALFORMED[shape])
    code, out, err = rig.run(command)
    assert code == 2 and out == ""
    assert err.startswith("notice-route: usage: ") and err.count("\n") == 1
    assert rig.crontab.table() == MALFORMED[shape].encode()
    assert rig.crontab.writes() == [] and rig.crontab.kinds() == ["list"]
    code, doc = rig.run_json(command)
    assert code == 2 and doc["error"]["code"] == "usage"
    assert schema_errors(doc, "error.json") == []


def test_a_malformed_crontab_is_refused_in_a_dry_run_too(rig: Rig) -> None:
    rig.crontab.set_table(MALFORMED["start_only"])
    assert rig.run("install", "--dry-run")[0] == 2
    assert rig.run("uninstall", "--dry-run")[0] == 2


# --- hostile values --------------------------------------------------------------------------

BAD_NAMES = {
    "space": "a b",
    "quote": "it's",
    "dquote": 'a"b',
    "newline": "a\nb",
    "percent": "a%b",
    "dollar": "a$b",
    "semicolon": "a;b",
    "backtick": "a`b",
    "glob": "a*b",
    "backslash": "a\\b",
    "hash": "a#b",
    "tilde": "a~b",
    "unicode": "café",
    "colon": "a:b",
    "bad_utf8": "a" + os.fsdecode(b"\xff") + "b",
}


@pytest.mark.parametrize("name", sorted(BAD_NAMES))
def test_a_program_path_outside_the_allowed_characters_is_refused(rig: Rig, name: str) -> None:
    hostile = Path(rig.tmp / "hostile" / BAD_NAMES[name])
    try:
        hostile.mkdir(parents=True)
    except OSError:
        pytest.skip("the file system cannot hold this name")
    prog = hostile / "notice-route"
    prog.write_text("#!/bin/sh\n")
    prog.chmod(0o755)
    code, out, err = rig.run("install", gateway=rig.gateway(argv0=str(prog)))
    assert code == 2 and out == ""
    assert err.startswith("notice-route: usage: ") and err.count("\n") == 1
    assert BAD_NAMES[name] not in err
    assert rig.crontab.calls() == []  # refused before the crontab is read


@pytest.mark.parametrize("name", sorted(BAD_NAMES))
def test_an_env_file_path_outside_the_allowed_characters_is_refused(rig: Rig, name: str) -> None:
    directory = rig.tmp / "envs" / BAD_NAMES[name]
    try:
        directory.mkdir(parents=True)
    except OSError:
        pytest.skip("the file system cannot hold this name")
    directory.chmod(0o700)
    path = directory / "cron.env"
    path.write_text("A=1\n")
    path.chmod(0o600)
    rig.config({"cron": {"env_file": str(path)}})
    code, out, err = rig.run("install")
    assert code == 2 and out == ""
    assert BAD_NAMES[name] not in err
    assert rig.crontab.calls() == []


def test_allowed_punctuation_is_rendered_quoted(rig: Rig) -> None:
    directory = rig.tmp / "a_b.c+d-e"
    directory.mkdir()
    directory.chmod(0o755)
    exe = directory / "notice-route"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    env_dir = rig.tmp / "v+1.0_x-y"
    env_dir.mkdir()
    env_dir.chmod(0o700)
    env_file = env_dir / "cron.env"
    env_file.write_text("A=1\n")
    env_file.chmod(0o600)
    rig.config({"cron": {"env_file": str(env_file)}})
    assert rig.run("install", gateway=rig.gateway(argv0=str(exe)))[0] == 0
    table = (rig.crontab.table() or b"").decode()
    assert f"set -a && . '{env_file}' && set +a && '{os.path.realpath(exe)}' flush --quiet" in table
    assert f"PATH=/usr/local/bin:/usr/bin:/bin:{os.path.realpath(directory)}\n" in table


@pytest.mark.parametrize(
    "problem", ["missing", "mode_0644", "mode_0640", "symlink", "directory", "group_writable_dir"]
)
def test_an_unsafe_env_file_is_refused_as_usage(rig: Rig, problem: str) -> None:
    good = rig.env_file()
    path = good
    if problem == "missing":
        path = good.with_name("absent.env")
    elif problem == "mode_0644":
        good.chmod(0o644)
    elif problem == "mode_0640":
        good.chmod(0o640)
    elif problem == "symlink":
        path = good.with_name("link.env")
        path.symlink_to(good)
    elif problem == "directory":
        path = good.parent
    else:
        good.parent.chmod(0o770)
    rig.config({"cron": {"env_file": str(path)}})
    code, out, err = rig.run("install")
    assert code == 2 and out == ""
    assert err.startswith("notice-route: usage: ") and str(path) not in err
    assert rig.crontab.calls() == []
    rig.crontab.set_table(b"x\n")
    good.parent.chmod(0o700)


def test_the_env_file_is_never_opened_or_echoed(rig: Rig, opened_names: list[str]) -> None:
    env_file = rig.env_file()
    rig.config({"cron": {"env_file": str(env_file)}})
    opened_names.clear()  # the test's own writes above
    code, out, err = rig.run("install", "--verbose")
    assert code == 0
    assert "cron.env" not in {os.path.basename(n) for n in opened_names}
    assert "canary-env-9d41" not in out + err + (rig.crontab.table() or b"").decode()


def test_a_relative_env_file_is_a_config_error(rig: Rig) -> None:
    rig.config({"cron": {"env_file": "cron.env"}})
    code, _, err = rig.run("install")
    assert code == 2 and "config_invalid" in err
    assert rig.crontab.calls() == []


def test_a_block_line_longer_than_cron_reads_is_refused(rig: Rig) -> None:
    deep = rig.tmp
    for _ in range(12):
        deep = deep / ("d" * 90)
    deep.mkdir(parents=True)
    exe = deep / "notice-route"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    code, _, err = rig.run("install", gateway=rig.gateway(argv0=str(exe)))
    assert code == 2 and err.startswith("notice-route: usage: ")
    assert rig.crontab.calls() == []


# --- finding the program ---------------------------------------------------------------------


def test_the_program_is_the_resolved_absolute_path(rig: Rig) -> None:
    link_dir = rig.tmp / "links"
    link_dir.mkdir()
    link_dir.chmod(0o755)
    link = link_dir / "notice-route"
    link.symlink_to(rig.exe)
    assert rig.run("install", gateway=rig.gateway(argv0=str(link)))[0] == 0
    assert rig.crontab.table() == block_of(rig)


def test_a_relative_argv0_is_made_absolute(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(rig.exe.parent)
    assert rig.run("install", gateway=rig.gateway(argv0="./notice-route"))[0] == 0
    assert rig.crontab.table() == block_of(rig)


def test_a_module_run_finds_the_program_on_path(rig: Rig) -> None:
    path = f"{rig.crontab.bin_dir}:{rig.exe.parent}:/usr/bin:/bin"
    gw = rig.gateway(argv0="/x/site-packages/notice_route/__main__.py", path=path)
    assert rig.run("install", gateway=gw)[0] == 0
    assert rig.crontab.table() == block_of(rig)


def test_no_program_to_run_is_exit_1(rig: Rig) -> None:
    gw = rig.gateway(argv0="/x/notice_route/__main__.py")
    code, _, err = rig.run("install", gateway=gw)
    assert code == 1 and err.startswith("notice-route: internal: ")
    assert "not found on a safe PATH entry; run it by its absolute path" in err
    assert rig.crontab.calls() == []


def test_a_program_that_is_not_executable_is_an_internal_error(rig: Rig) -> None:
    rig.exe.chmod(0o644)
    code, _, _ = rig.run("install")
    assert code == 1
    assert rig.crontab.calls() == []


# --- crontab failures ------------------------------------------------------------------------


def test_a_crontab_that_cannot_be_read_is_never_overwritten(rig: Rig) -> None:
    rig.crontab.set_table(b"0 0 * * * keep\n")
    rig.crontab.behave(list_exit=2, list_stderr=f"boom {CANARY}\n")
    code, out, err = rig.run("install")
    assert code == 1 and CANARY not in out + err
    assert rig.crontab.writes() == [] and rig.crontab.kinds() == ["list"]
    assert rig.run("uninstall")[0] == 1


@pytest.mark.parametrize(
    "stderr",
    [
        "crontab: permission denied\n",
        "",
        "no crontabs here\n",
        "denied\n",
    ],
)
def test_only_no_crontab_for_with_exit_1_is_an_empty_crontab(rig: Rig, stderr: str) -> None:
    rig.crontab.behave(list_exit=1, list_stderr=stderr)
    code, _, err = rig.run("install")
    assert code == 1 and err.startswith("notice-route: internal: ")
    assert rig.crontab.writes() == []


def test_no_crontab_for_with_another_exit_code_is_a_failure(rig: Rig) -> None:
    rig.crontab.behave(list_exit=2, list_stderr="no crontab for fakeuser\n")
    assert rig.run("install")[0] == 1
    assert rig.crontab.writes() == []


def test_a_failing_write_is_exit_1_and_never_echoes_crontab_text(rig: Rig) -> None:
    rig.crontab.set_table(b"0 0 * * * keep\n")
    rig.crontab.behave(write_exit=1, write_stderr=f"bad line: {CANARY}\n")
    code, out, err = rig.run("install")
    assert code == 1 and CANARY not in out + err
    assert err.startswith("notice-route: internal: ") and err.count("\n") == 1
    assert "(it is unchanged)" in err
    assert rig.crontab.table() == b"0 0 * * * keep\n"
    code, doc = rig.run_json("install")
    assert code == 1 and doc["error"]["code"] == "internal" and CANARY not in json.dumps(doc)
    assert schema_errors(doc, "error.json") == []


def test_a_missing_crontab_program_is_exit_1(rig: Rig, tmp_path: Path) -> None:
    empty = tmp_path / "emptybin"
    empty.mkdir()
    empty.chmod(0o755)
    code, _, err = rig.run("install", gateway=rig.gateway(path=str(empty)))
    assert code == 1 and err.startswith("notice-route: internal: ")


def test_a_crontab_found_only_through_a_relative_path_entry_is_ignored(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(rig.crontab.bin_dir)
    code, _, _ = rig.run("install", gateway=rig.gateway(path=f".:{rig.tmp / 'nowhere'}"))
    assert code == 1
    assert rig.crontab.calls() == []


@pytest.mark.parametrize("hang", ["list", "write"])
def test_a_hanging_crontab_times_out_with_exit_1(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, hang: str
) -> None:
    monkeypatch.setattr(gateway_module, "CRONTAB_TIMEOUT", 0.3)
    rig.crontab.set_table(b"0 0 * * * keep\n")
    rig.crontab.behave(hang=hang, hang_s=20)
    code, _, err = rig.run("install")
    assert code == 1 and err.startswith("notice-route: internal: ")
    assert rig.crontab.table() == b"0 0 * * * keep\n"
    if hang == "write":
        assert "unchanged" not in err and "check it" in err


def test_a_write_that_hangs_or_overflows_says_it_may_have_changed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R13: only a write that exited nonzero is known to have left the table alone."""
    monkeypatch.setattr(gateway_module, "CRONTAB_TIMEOUT", 0.3)
    rig.crontab.set_table(b"0 0 * * * keep\n")
    rig.crontab.behave(hang="write", hang_s=20)
    err = rig.run("install")[2]
    assert "may have changed" in err and "unchanged" not in err
    monkeypatch.setattr(gateway_module, "CRONTAB_TIMEOUT", 10.0)
    rig.crontab.behave(stdout_noise="x" * (gateway_module.STDOUT_LIMIT + 10))
    err = rig.run("install")[2]
    assert "may have changed" in err and "unchanged" not in err


def test_a_write_that_exits_nonzero_says_it_is_unchanged(rig: Rig) -> None:
    rig.crontab.set_table(b"0 0 * * * keep\n")
    rig.crontab.behave(write_exit=1, write_stderr="nope\n")
    err = rig.run("install")[2]
    assert "(it is unchanged)" in err and "may have changed" not in err


def test_a_crontab_too_large_to_read_is_refused(rig: Rig) -> None:
    rig.crontab.behave(huge_list=gateway_module.STDOUT_LIMIT + 10)
    code, _, err = rig.run("install")
    assert code == 1 and err.startswith("notice-route: internal: ")
    assert rig.crontab.writes() == []


def test_a_crontab_changed_between_read_and_write_aborts(rig: Rig) -> None:
    rig.crontab.set_table(b"0 0 * * * mine\n")
    rig.crontab.behave(mutate_after_list={"n": 1, "append": "0 5 * * * theirs\n"})
    code, _, err = rig.run("install")
    assert code == 1 and err.startswith("notice-route: internal: ")
    assert rig.crontab.writes() == []
    assert rig.crontab.table() == b"0 0 * * * mine\n0 5 * * * theirs\n"
    assert rig.crontab.kinds() == ["list", "list"]


def test_a_crontab_created_between_read_and_write_aborts(rig: Rig) -> None:
    rig.crontab.behave(mutate_after_list={"n": 1, "append": "0 5 * * * theirs\n"})
    assert rig.run("install")[0] == 1
    assert rig.crontab.writes() == []
    assert rig.crontab.table() == b"0 5 * * * theirs\n"


def test_uninstall_aborts_when_the_crontab_changed_too(rig: Rig) -> None:
    rig.crontab.set_table(block_of(rig))
    rig.crontab.behave(mutate_after_list={"n": 1, "append": "0 5 * * * theirs\n"})
    assert rig.run("uninstall")[0] == 1
    assert rig.crontab.writes() == []


def test_a_second_run_after_an_abort_succeeds(rig: Rig) -> None:
    rig.crontab.set_table(b"0 0 * * * mine\n")
    rig.crontab.behave(mutate_after_list={"n": 1, "append": "0 5 * * * theirs\n"})
    assert rig.run("install")[0] == 1
    rig.crontab.behave()
    assert rig.run("install")[0] == 0
    assert rig.crontab.table() == b"0 0 * * * mine\n0 5 * * * theirs\n" + block_of(rig)


def test_two_runs_one_after_the_other_write_once(rig: Rig) -> None:
    """The documented serialization is the re-read: sequential runs converge on one block."""
    assert rig.run("install")[0] == 0
    assert rig.run("install")[0] == 0
    assert (rig.crontab.table() or b"").count(b"managed v1") == 1
    assert len(rig.crontab.writes()) == 1


# --- dry run ---------------------------------------------------------------------------------


def test_install_dry_run_prints_the_block_and_writes_nothing(rig: Rig) -> None:
    rig.crontab.set_table(FOREIGN)
    code, out, _ = rig.run("install", "--dry-run")
    assert code == 0
    assert out.endswith(rig.expected_block())
    assert rig.crontab.table() == FOREIGN
    assert rig.crontab.kinds() == ["list"]
    code, doc = rig.run_json("install", "--dry-run")
    assert doc == {
        "action": "installed",
        "block": rig.expected_block(),
        "dry_run": True,
        "env_file": None,
    }
    assert schema_errors(doc, "cron-install.json") == []
    assert rig.crontab.writes() == []


def test_install_dry_run_reports_updated_and_unchanged(rig: Rig) -> None:
    rig.crontab.set_table(b"# notice-route managed v0\nold\n" + END.encode() + b"\n")
    assert rig.run_json("install", "--dry-run")[1]["action"] == "updated"
    rig.crontab.set_table(block_of(rig))
    assert rig.run_json("install", "--dry-run")[1]["action"] == "unchanged"
    assert rig.crontab.writes() == []


def test_uninstall_dry_run_shows_the_block_and_removes_nothing(rig: Rig) -> None:
    rig.crontab.set_table(FOREIGN + block_of(rig))
    code, doc = rig.run_json("uninstall", "--dry-run")
    assert code == 0
    assert doc == {"action": "removed", "block": rig.expected_block(), "dry_run": True}
    assert schema_errors(doc, "cron-uninstall.json") == []
    assert rig.crontab.table() == FOREIGN + block_of(rig)
    assert rig.crontab.writes() == []
    rig.crontab.set_table(FOREIGN)
    assert rig.run_json("uninstall", "--dry-run")[1]["action"] == "unchanged"


def test_a_dry_run_still_refuses_hostile_values(rig: Rig) -> None:
    bad = rig.tmp / "has space"
    bad.mkdir()
    bad.chmod(0o755)
    prog = bad / "notice-route"
    prog.write_text("#!/bin/sh\n")
    prog.chmod(0o755)
    assert rig.run("install", "--dry-run", gateway=rig.gateway(argv0=str(prog)))[0] == 2
    assert rig.crontab.calls() == []


def test_a_dry_run_with_an_unreadable_crontab_fails_like_a_real_run(rig: Rig) -> None:
    rig.crontab.behave(list_exit=2, list_stderr="x\n")
    assert rig.run("install", "--dry-run")[0] == 1


# --- output and usage ------------------------------------------------------------------------


def test_human_output_names_the_action_and_prints_the_block(rig: Rig) -> None:
    code, out, err = rig.run("install")
    assert code == 0 and err == ""
    assert out == "installed\n" + rig.expected_block()
    code, out, _ = rig.run("install")
    assert out == "unchanged\n"
    code, out, _ = rig.run("uninstall")
    assert out == "removed\n"
    code, out, _ = rig.run("uninstall", "--dry-run")
    assert out == "unchanged (dry run)\n"


def test_quiet_prints_nothing_and_json_ignores_it(rig: Rig) -> None:
    code, out, err = rig.run("install", "--quiet")
    assert (code, out, err) == (0, "", "")
    assert rig.crontab.table() == block_of(rig)
    code, doc = rig.run_json("uninstall", "--quiet")
    assert code == 0 and doc["action"] == "removed"


def test_global_flags_work_before_and_after_the_command(rig: Rig) -> None:
    gw = rig.gateway()
    assert main_json(rig, ["--json", "cron", "install"], gw)["action"] == "installed"
    assert main_json(rig, ["cron", "--json", "uninstall"], gw)["action"] == "removed"
    assert main_json(rig, ["cron", "install", "--json", "--dry-run"], gw)["dry_run"] is True


def main_json(rig: Rig, argv: list[str], gw: Any) -> dict[str, Any]:
    from notice_route.cli import main

    assert main(argv, gw) == 0
    return json.loads(rig.capsys.readouterr().out)  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["bogus"],
        ["install", "extra"],
        ["install", "--actor", "auto:x"],
        ["install", "--decision-id", "x"],
        ["install", "--env-file", "/x"],
        ["install", "--dry"],
        ["install", "--dry-run", "--dry-run=1"],
        ["uninstall", "--destination", "x"],
        ["install", "uninstall"],
        ["install", "--verbose", "--quiet"],
        ["--config", "/x.yaml", "install"],
        ["install", "--config", "/x.yaml"],
        ["uninstall", "--config=/x.yaml"],
    ],
)
def test_usage_errors_are_exit_2_without_touching_the_crontab(rig: Rig, argv: list[str]) -> None:
    rig.crontab.set_table(block_of(rig))
    code, out, err = rig.run(*argv)
    assert code == 2 and out == ""
    assert err.startswith("notice-route: usage: ") and "/x" not in err
    assert rig.crontab.calls() == [] and rig.crontab.table() == block_of(rig)


# --- environment and state -------------------------------------------------------------------


def test_unconfigured_install_works_and_creates_no_state(rig: Rig) -> None:
    assert not (rig.home / ".config").exists()
    code, doc = rig.run_json("install")
    assert code == 0 and doc["env_file"] is None
    assert not (rig.home / ".local").exists()


def test_a_config_without_cron_installs_without_an_env_file(rig: Rig) -> None:
    rig.config({"quiet_hours": {}})
    code, doc = rig.run_json("install")
    assert code == 0 and doc["env_file"] is None


def test_an_invalid_config_is_exit_2_before_the_crontab_is_read(rig: Rig) -> None:
    rig.config({"bogus_key": 1})
    code, _, err = rig.run("install")
    assert code == 2 and "config_invalid" in err
    assert rig.crontab.calls() == []


def test_hook_recursion_does_not_refuse_cron_and_is_not_exported(rig: Rig) -> None:
    gw = rig.gateway(environ={"NOTICE_ROUTE_HOOK": "1"})
    assert rig.run("install", gateway=gw)[0] == 0
    assert b"NOTICE_ROUTE_HOOK" not in (rig.crontab.table() or b"")
    assert rig.run("uninstall", gateway=gw)[0] == 0


def test_the_block_runs_flush_and_prune_with_the_absolute_program(rig: Rig) -> None:
    assert rig.run("install")[0] == 0
    lines = (rig.crontab.table() or b"").decode().splitlines()
    exe = os.path.realpath(rig.exe)
    assert lines[2] == f"* * * * * '{exe}' flush --quiet"
    assert lines[3] == f"17 3 * * * '{exe}' prune --quiet"
    assert lines[0] == START and lines[-1] == END and len(lines) == 5


def test_the_block_carries_no_environment_text(rig: Rig) -> None:
    gw = rig.gateway(environ={"CRED_ONE": CANARY, "HA_TOKEN": CANARY})
    assert rig.run("install", gateway=gw)[0] == 0
    assert CANARY.encode() not in (rig.crontab.table() or b"")


def test_the_program_directory_already_on_the_path_is_not_repeated(rig: Rig) -> None:
    # a program in a system directory is not simulated; the rule is lexical
    from notice_route.cron import path_line

    assert path_line("/usr/bin") == "PATH=/usr/local/bin:/usr/bin:/bin"
    assert path_line("/opt/x") == "PATH=/usr/local/bin:/usr/bin:/bin:/opt/x"


def test_marker_names_match_the_documented_text() -> None:
    from notice_route import cron

    assert cron.START_PREFIX == "# notice-route managed v"
    assert cron.END_LINE == END
    assert cron.HEADER == START


# --- review round 1 --------------------------------------------------------------------------


def test_install_over_a_crlf_block_rewrites_it_and_keeps_foreign_crlf_lines(rig: Rig) -> None:
    table = f"a\r\n{START}\r\n{END}\r\nb\r\n".encode()
    rig.crontab.set_table(table)
    code, doc = rig.run_json("install")
    assert code == 0 and doc["action"] == "updated"
    assert rig.crontab.table() == b"a\r\n" + block_of(rig) + b"b\r\n"


@pytest.mark.parametrize(
    ("name", "table", "shown"),
    [
        ("v2", "# notice-route managed v2 x\nfoo\n" + END + "\n", None),
        ("flush5m", "# notice-route managed v1 flush=5m\nfoo\n" + END + "\n", None),
        ("crlf", f"{START}\r\nfoo\r\n{END}\r\n", f"{START}\nfoo\n{END}\n"),
        (
            "trailing_blanks",
            f"{START} \t\nfoo \n{END} \t\n",
            f"{START}\nfoo\n{END}\n",
        ),
    ],
)
def test_uninstall_output_is_schema_valid_for_any_block_version_or_line_ending(
    rig: Rig, name: str, table: str, shown: str | None
) -> None:
    rig.crontab.set_table(table)
    code, doc = rig.run_json("uninstall", "--dry-run")
    assert code == 0 and doc["action"] == "removed", name
    assert schema_errors(doc, "cron-uninstall.json") == [], name
    assert "\r" not in doc["block"]
    if shown is not None:
        assert doc["block"] == shown


def test_uninstall_shows_non_utf8_bytes_as_replacement_characters(rig: Rig) -> None:
    rig.crontab.set_table(f"{START}\n".encode() + b"x \xff y\n" + f"{END}\n".encode())
    code, doc = rig.run_json("uninstall")
    assert code == 0 and "\ufffd" in doc["block"]
    assert schema_errors(doc, "cron-uninstall.json") == []


def test_an_untrusted_program_directory_is_left_out_of_the_path_line(rig: Rig) -> None:
    rig.exe.parent.chmod(0o777)
    code, doc = rig.run_json("install")
    assert code == 0
    lines = doc["block"].splitlines()
    assert lines[1] == "PATH=/usr/local/bin:/usr/bin:/bin"
    assert f"'{os.path.realpath(rig.exe)}' flush --quiet" in lines[2]
