"""Config loading: password-database home, ownership checks, strict YAML and drop-ins (#6)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from notice_route import gateway
from notice_route.config import (
    MAX_DEPTH,
    MAX_DROPINS,
    MAX_FILE_BYTES,
    ConfigError,
    load_config,
)
from notice_route.gateway import Gateway

SECRET = "hunter2-secret-value"


class Env:
    """A private home with a config directory; every path a test writes goes through here."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.cfg = home / ".config" / "notice-route"
        self.gw = Gateway(passwd_home=lambda: str(home))

    def write(self, rel: str, text: str | bytes = "", mode: int = 0o600) -> Path:
        path = self.cfg / rel
        for directory in (self.cfg, path.parent):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        path.chmod(mode)
        return path

    def main(self, text: str = "enabled: true\n") -> Path:
        return self.write("config.yaml", text)

    def load(self, config_path: str | None = None) -> Any:
        return load_config(self.gw, config_path)

    def data(self) -> dict[str, Any]:
        loaded = self.load()
        assert loaded is not None
        return loaded.data


@pytest.fixture
def env(home: Path) -> Env:
    return Env(home)


def invalid(env: Env, key: str, config_path: str | None = None) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        env.load(config_path)
    assert info.value.code == "config_invalid"
    assert info.value.key == key
    assert info.value.exit_code == 2
    return info.value


def unsafe(env: Env, key: str, config_path: str | None = None) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        env.load(config_path)
    assert info.value.code == "config_unsafe"
    assert info.value.key == key
    assert info.value.exit_code == 2
    return info.value


# --- finding the config --------------------------------------------------------------------


def test_loads_the_main_file_from_the_password_database_home(env: Env) -> None:
    env.main("enabled: false\nlock_wait: 3s\n")
    loaded = env.load()
    assert loaded is not None
    assert loaded.data == {"enabled": False, "lock_wait": "3s"}
    assert loaded.files == ("config.yaml",)


def test_no_config_directory_is_unconfigured_and_creates_nothing(env: Env) -> None:
    assert env.load() is None
    assert list(env.home.iterdir()) == []


def test_no_config_file_is_unconfigured_and_creates_nothing(env: Env) -> None:
    env.cfg.mkdir(parents=True, mode=0o700)
    assert env.load() is None
    assert list(env.cfg.iterdir()) == []
    assert [p.name for p in env.home.iterdir()] == [".config"]


def test_dropins_alone_do_not_configure_and_are_not_read(env: Env) -> None:
    env.write("config.d/10-a.yaml", "rules: [")  # would be invalid if it were read
    assert env.load() is None


def test_environment_variables_are_ignored(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.main("lock_wait: 5s\n")
    decoy = tmp_path / "decoy"
    for rel in ("home/.config/notice-route", "xdg-config/notice-route", "xdg-state/notice-route"):
        (decoy / rel).mkdir(parents=True, mode=0o700)
        (decoy / rel / "config.yaml").write_text("lock_wait: 9s\n")
    monkeypatch.setenv("HOME", str(decoy / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(decoy / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(decoy / "xdg-state"))
    assert env.data() == {"lock_wait": "5s"}


def test_environment_variables_do_not_configure_a_missing_config(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "decoy" / "notice-route").mkdir(parents=True, mode=0o700)
    (tmp_path / "decoy" / "notice-route" / "config.yaml").write_text("enabled: true\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "decoy"))
    monkeypatch.setenv("HOME", str(tmp_path / "decoy"))
    assert env.load() is None


@pytest.mark.parametrize("value", ["", "relative/dir", "~", "/home/u\x00x"])
def test_unusable_home_is_config_invalid_with_key_home(value: str) -> None:
    broken = Env(Path("/unused"))
    broken.gw = Gateway(passwd_home=lambda: value)
    invalid(broken, "home")


def test_unresolvable_home_is_key_home() -> None:
    def lookup() -> str:
        raise KeyError("no entry")

    broken = Env(Path("/unused"))
    broken.gw = Gateway(passwd_home=lookup)
    invalid(broken, "home")


# --- --config ------------------------------------------------------------------------------


def test_config_option_replaces_config_yaml_and_dropins_beside_it_apply(
    env: Env, tmp_path: Path
) -> None:
    env.main("lock_wait: 9s\n")  # the home config is not read
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    (other / "mine.yaml").write_text("lock_wait: 4s\n")
    (other / "mine.yaml").chmod(0o600)
    (other / "config.d").mkdir(mode=0o700)
    (other / "config.d" / "x.yaml").write_text("rules: [{name: a}]\n")
    (other / "config.d" / "x.yaml").chmod(0o600)
    loaded = env.load(str(other / "mine.yaml"))
    assert loaded is not None
    assert loaded.data == {"lock_wait": "4s", "rules": [{"name": "a"}]}
    assert loaded.files == ("--config", "config.d/x.yaml")


def test_config_option_does_not_consult_the_home(tmp_path: Path) -> None:
    def lookup() -> str:
        raise AssertionError("the home must not be looked up")

    cfg = tmp_path / "c"
    cfg.mkdir(mode=0o700)
    (cfg / "c.yaml").write_text("enabled: true\n")
    (cfg / "c.yaml").chmod(0o600)
    broken = Env(tmp_path)
    broken.gw = Gateway(passwd_home=lookup)
    loaded = broken.load(str(cfg / "c.yaml"))
    assert loaded is not None
    assert loaded.data == {"enabled": True}


def test_config_option_with_a_relative_path(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "rel").mkdir(mode=0o700)
    (tmp_path / "rel" / "c.yaml").write_text("enabled: true\n")
    (tmp_path / "rel" / "c.yaml").chmod(0o600)
    monkeypatch.chdir(tmp_path / "rel")
    loaded = env.load("c.yaml")
    assert loaded is not None
    assert loaded.data == {"enabled": True}


@pytest.mark.parametrize("path", ["", "/", "dir/", "x/..", "a\x00b"])
def test_config_option_with_a_bad_path_is_key_config(env: Env, path: str) -> None:
    invalid(env, "--config", path)


def test_missing_config_option_file_is_an_error_not_unconfigured(env: Env, tmp_path: Path) -> None:
    (tmp_path / "d").mkdir(mode=0o700)
    invalid(env, "--config", str(tmp_path / "d" / "absent.yaml"))
    invalid(env, "--config", str(tmp_path / "no-dir" / "absent.yaml"))


def test_config_option_file_problems_are_keyed_config(env: Env, tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    bad = d / "c.yaml"
    bad.write_text("a: &x 1\n")
    bad.chmod(0o600)
    invalid(env, "--config", str(bad))
    bad.chmod(0o666)
    unsafe(env, "--config", str(bad))
    bad.unlink()
    bad.symlink_to(d / "real.yaml")
    unsafe(env, "--config", str(bad))


def test_config_option_directory_is_checked_like_the_config_directory(
    env: Env, tmp_path: Path
) -> None:
    d = tmp_path / "d"
    d.mkdir()
    d.chmod(0o777)
    (d / "c.yaml").write_text("enabled: true\n")
    (d / "c.yaml").chmod(0o600)
    unsafe(env, ".", str(d / "c.yaml"))
    d.chmod(0o700)


# --- ownership and mode --------------------------------------------------------------------


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777, 0o720, 0o702])
def test_config_directory_writable_by_others_is_unsafe(env: Env, mode: int) -> None:
    env.main()
    env.cfg.chmod(mode)
    unsafe(env, ".")
    env.cfg.chmod(0o700)


def test_config_directory_not_owned_by_the_user_is_unsafe(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.main()
    real = os.geteuid()
    monkeypatch.setattr(gateway.os, "geteuid", lambda: real + 1)
    unsafe(env, ".")


def test_config_directory_that_is_a_symlink_is_unsafe(env: Env, tmp_path: Path) -> None:
    real = tmp_path / "real-cfg"
    real.mkdir(mode=0o700)
    (real / "config.yaml").write_text("enabled: true\n")
    (real / "config.yaml").chmod(0o600)
    env.cfg.parent.mkdir(parents=True)
    env.cfg.symlink_to(real)
    unsafe(env, ".")


def test_config_directory_that_is_a_file_is_unsafe(env: Env) -> None:
    env.cfg.parent.mkdir(parents=True)
    env.cfg.write_text("x")
    unsafe(env, ".")


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o660, 0o666])
def test_main_file_writable_by_others_is_unsafe(env: Env, mode: int) -> None:
    env.write("config.yaml", "enabled: true\n", mode=mode)
    unsafe(env, "config.yaml")


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o400])
def test_main_file_readable_by_others_is_fine(env: Env, mode: int) -> None:
    env.write("config.yaml", "enabled: true\n", mode=mode)
    assert env.data() == {"enabled": True}


def test_main_file_not_owned_by_the_user_is_unsafe(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.main()
    real = os.geteuid()
    # Directory ownership is checked first, so make only the file look foreign by checking the
    # directory as the real user and the file as another.
    calls = {"n": 0}

    def euid() -> int:
        calls["n"] += 1
        return real if calls["n"] == 1 else real + 1  # 1st: the directory; then: the file

    monkeypatch.setattr(gateway.os, "geteuid", euid)
    unsafe(env, "config.yaml")


def test_main_file_symlink_is_unsafe_even_to_a_good_file(env: Env, tmp_path: Path) -> None:
    (tmp_path / "real.yaml").write_text("enabled: true\n")
    (tmp_path / "real.yaml").chmod(0o600)
    env.cfg.mkdir(parents=True, mode=0o700)
    (env.cfg / "config.yaml").symlink_to(tmp_path / "real.yaml")
    unsafe(env, "config.yaml")


def test_main_file_fifo_is_unsafe_and_does_not_block(env: Env) -> None:
    env.cfg.mkdir(parents=True, mode=0o700)
    os.mkfifo(env.cfg / "config.yaml", 0o600)
    unsafe(env, "config.yaml")


def test_main_file_that_is_a_directory_is_unsafe(env: Env) -> None:
    env.cfg.mkdir(parents=True)
    env.cfg.chmod(0o700)
    (env.cfg / "config.yaml").mkdir(mode=0o700)
    unsafe(env, "config.yaml")


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_config_d_writable_by_others_is_unsafe(env: Env, mode: int) -> None:
    env.main()
    env.write("config.d/10-a.yaml", "rules: []\n")
    (env.cfg / "config.d").chmod(mode)
    unsafe(env, "config.d")
    (env.cfg / "config.d").chmod(0o700)


def test_config_d_not_owned_by_the_user_is_unsafe(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.main()
    (env.cfg / "config.d").mkdir(mode=0o700)
    real = os.geteuid()
    calls = {"n": 0}

    def euid() -> int:
        calls["n"] += 1
        return real if calls["n"] <= 2 else real + 1  # config dir, config.yaml; then config.d

    monkeypatch.setattr(gateway.os, "geteuid", euid)
    unsafe(env, "config.d")


def test_config_d_that_is_a_symlink_is_unsafe(env: Env, tmp_path: Path) -> None:
    env.main()
    (tmp_path / "elsewhere").mkdir(mode=0o700)
    (tmp_path / "elsewhere" / "x.yaml").write_text("rules: []\n")
    (env.cfg / "config.d").symlink_to(tmp_path / "elsewhere")
    unsafe(env, "config.d")


def test_config_d_that_is_a_file_is_unsafe(env: Env) -> None:
    env.main()
    env.write("config.d", "x")
    unsafe(env, "config.d")


def test_empty_or_absent_config_d_is_fine(env: Env) -> None:
    env.main()
    assert env.data() == {"enabled": True}
    (env.cfg / "config.d").mkdir(mode=0o700)
    assert env.data() == {"enabled": True}


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_dropin_writable_by_others_is_unsafe(env: Env, mode: int) -> None:
    env.main()
    env.write("config.d/10-a.yaml", "rules: []\n", mode=mode)
    unsafe(env, "config.d/10-a.yaml")


def test_dropin_not_owned_by_the_user_is_unsafe(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.main()
    env.write("config.d/10-a.yaml", "rules: []\n")
    real = os.geteuid()
    calls = {"n": 0}

    def euid() -> int:
        calls["n"] += 1
        return real if calls["n"] <= 3 else real + 1  # dir, config.yaml, config.d; then drop-in

    monkeypatch.setattr(gateway.os, "geteuid", euid)
    unsafe(env, "config.d/10-a.yaml")


def test_dropin_symlink_is_unsafe_even_to_a_good_file(env: Env, tmp_path: Path) -> None:
    env.main()
    (tmp_path / "real.yaml").write_text("rules: []\n")
    (tmp_path / "real.yaml").chmod(0o600)
    (env.cfg / "config.d").mkdir(mode=0o700)
    (env.cfg / "config.d" / "10-a.yaml").symlink_to(tmp_path / "real.yaml")
    unsafe(env, "config.d/10-a.yaml")


def test_dangling_dropin_symlink_is_unsafe(env: Env, tmp_path: Path) -> None:
    env.main()
    (env.cfg / "config.d").mkdir(mode=0o700)
    (env.cfg / "config.d" / "10-a.yaml").symlink_to(tmp_path / "gone.yaml")
    unsafe(env, "config.d/10-a.yaml")


def test_dropin_fifo_and_directory_are_unsafe(env: Env) -> None:
    env.main()
    (env.cfg / "config.d").mkdir(mode=0o700)
    os.mkfifo(env.cfg / "config.d" / "10-a.yaml", 0o600)
    unsafe(env, "config.d/10-a.yaml")
    (env.cfg / "config.d" / "10-a.yaml").unlink()
    (env.cfg / "config.d" / "10-a.yaml").mkdir(mode=0o700)
    unsafe(env, "config.d/10-a.yaml")


def test_unsafe_dropin_name_is_sanitized_in_the_key(env: Env) -> None:
    env.main()
    env.write("config.d/we ird\u202e\n.yaml", "rules: []\n", mode=0o666)
    err = unsafe(env, "config.d/we?ird??.yaml")
    assert "\u202e" not in err.key


def test_directory_is_checked_before_the_file(env: Env) -> None:
    env.write("config.yaml", "enabled: true\n", mode=0o666)
    env.cfg.chmod(0o777)
    unsafe(env, ".")
    env.cfg.chmod(0o700)


def test_unreadable_main_file_is_invalid_with_its_name(env: Env) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    env.write("config.yaml", "enabled: true\n", mode=0o000)
    invalid(env, "config.yaml")


def test_unreadable_config_directory_is_invalid(env: Env) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    env.main()
    env.cfg.chmod(0o000)
    try:
        invalid(env, ".")
    finally:
        env.cfg.chmod(0o700)


# --- size and count limits -----------------------------------------------------------------


def _padded(size: int) -> bytes:
    base = b"enabled: true\n"
    return base + b"#" * (size - len(base) - 1) + b"\n"


def test_file_of_exactly_the_limit_is_read(env: Env) -> None:
    assert MAX_FILE_BYTES == 256 * 1024
    env.write("config.yaml", _padded(MAX_FILE_BYTES))
    assert env.data() == {"enabled": True}


def test_file_one_byte_over_the_limit_is_invalid(env: Env) -> None:
    env.write("config.yaml", _padded(MAX_FILE_BYTES + 1))
    invalid(env, "config.yaml")


def test_dropin_over_the_limit_is_invalid_with_its_name(env: Env) -> None:
    env.main()
    env.write("config.d/10-big.yaml", _padded(MAX_FILE_BYTES + 1))
    invalid(env, "config.d/10-big.yaml")


def test_huge_file_is_refused_without_reading_it_all(env: Env) -> None:
    env.write("config.yaml", b"#" * (20 * 1024 * 1024))
    start = time.monotonic()
    invalid(env, "config.yaml")
    assert time.monotonic() - start < 5


def test_limit_counts_bytes_not_characters(env: Env) -> None:
    text = "a: '" + "\u00e9" * (MAX_FILE_BYTES // 2 - 1) + "'\n"
    assert len(text) < MAX_FILE_BYTES < len(text.encode())
    env.write("config.yaml", text)
    invalid(env, "config.yaml")


def test_256_dropins_are_read_and_257_are_refused(env: Env) -> None:
    assert MAX_DROPINS == 256
    env.main("lock_wait: 2s\n")
    for index in range(256):
        env.write(f"config.d/{index:03d}.yaml", f"key{index}: 1\n")
    assert len(env.data()) == 257
    env.write("config.d/999.yaml", "key999: 1\n")
    invalid(env, "config.d")


def test_non_yaml_names_do_not_count_toward_the_dropin_limit(env: Env) -> None:
    env.main()
    for index in range(300):
        env.write(f"config.d/{index}.txt", "not read\n")
    assert env.data() == {"enabled": True}


# --- which names are drop-ins --------------------------------------------------------------


def test_only_visible_yaml_names_are_dropins(env: Env) -> None:
    env.main("a: 1\n")
    env.write("config.d/10-real.yaml", "b: 2\n")
    for name in (
        ".hidden.yaml",
        "notes.yml",
        "backup.yaml~",
        "x.yaml.bak",
        "config.YAML",
        "README",
    ):
        env.write(f"config.d/{name}", "boom: [\n", mode=0o666)  # never read, never checked
    loaded = env.load()
    assert loaded is not None
    assert loaded.data == {"a": 1, "b": 2}
    assert loaded.files == ("config.yaml", "config.d/10-real.yaml")


def test_dropins_are_read_in_lexical_order(env: Env) -> None:
    env.main("rules: [{name: main}]\n")
    for name in ("20-b.yaml", "10-a.yaml", "100-c.yaml", "B.yaml", "a.yaml", "00.yaml"):
        env.write(f"config.d/{name}", f"rules: [{{name: '{name}'}}]\n")
    loaded = env.load()
    assert loaded is not None
    assert [r["name"] for r in loaded.data["rules"]] == [
        "main",
        "00.yaml",
        "10-a.yaml",
        "100-c.yaml",
        "20-b.yaml",
        "B.yaml",
        "a.yaml",
    ]
    assert loaded.files[0] == "config.yaml"


# --- strict YAML ---------------------------------------------------------------------------


def test_yes_on_and_dates_stay_strings(env: Env) -> None:
    env.main(
        "a: yes\nb: on\nc: no\nd: off\ne: y\nf: 2026-11-24\ng: 2026-11-24 10:00:00\n"
        "h: True\ni: TRUE\nj: 010\nk: 0x10\nl: 1_000\nm: 1:30\nn: 1e3\no: .inf\np: .nan\nq: +1\n"
    )
    assert env.data() == {
        "a": "yes",
        "b": "on",
        "c": "no",
        "d": "off",
        "e": "y",
        "f": "2026-11-24",
        "g": "2026-11-24 10:00:00",
        "h": "True",
        "i": "TRUE",
        "j": "010",
        "k": "0x10",
        "l": "1_000",
        "m": "1:30",
        "n": "1e3",
        "o": ".inf",
        "p": ".nan",
        "q": "+1",
    }


def test_plain_scalars_still_have_their_types(env: Env) -> None:
    env.main("a: true\nb: false\nc: 12\nd: -3\ne: 1.5\nf: null\ng: ~\nh:\ni: 0\nj: abc\n")
    assert env.data() == {
        "a": True,
        "b": False,
        "c": 12,
        "d": -3,
        "e": 1.5,
        "f": None,
        "g": None,
        "h": None,
        "i": 0,
        "j": "abc",
    }


def test_quoted_scalars_are_strings(env: Env) -> None:
    env.main("a: 'true'\nb: \"12\"\nc: '1.5'\nd: \"null\"\n")
    assert env.data() == {"a": "true", "b": "12", "c": "1.5", "d": "null"}


def test_nested_structures_load(env: Env) -> None:
    env.main("rules:\n  - name: a\n    match:\n      labels: {k: v}\n      list: [1, 2]\n")
    assert env.data() == {"rules": [{"name": "a", "match": {"labels": {"k": "v"}, "list": [1, 2]}}]}


def test_empty_file_and_comment_only_file_are_empty_maps(env: Env) -> None:
    env.main("")
    assert env.data() == {}
    env.main("# only a comment\n\n  \n")
    assert env.data() == {}


@pytest.mark.parametrize("text", ["---\n", "null\n", "~\n", "--- ~\n...\n"])
def test_a_null_document_is_invalid(env: Env, text: str) -> None:
    env.main(text)
    invalid(env, "config.yaml")


@pytest.mark.parametrize(
    "text",
    [
        "a: &x 1\n",
        "a: &x 1\nb: *x\n",
        "a: *x\n",
        "- &x a\n",
        "a: &x\n  b: 1\nc: *x\n",
        "&x a: 1\n",
        "a: &x [1]\nb: [*x]\n",
        "a: !!str 1\n",
        "a: !str 1\n",
        "a: !!int '1'\n",
        "a: ! 1\n",
        "a: !!python/object/apply:os.system ['echo pwned']\n",
        "a: !!python/name:os.system\n",
        "!!map {a: 1}\n",
        "a: !!binary aGk=\n",
        "a: !!set {x}\n",
        "a: !!timestamp 2026-01-01\n",
        "a: !<tag:yaml.org,2002:str> x\n",
        "%TAG !e! tag:example.com,2026:\n---\na: !e!x 1\n",
        "a: !!seq [1]\n",
        "a: !!map {b: 1}\n",
        "<<: {a: 1}\n",
        "a: {<<: {b: 1}}\n",
        "base: &b {x: 1}\nd:\n  <<: *b\n",
    ],
)
def test_anchors_aliases_tags_and_merge_keys_are_invalid(env: Env, text: str) -> None:
    env.main(text)
    invalid(env, "config.yaml")


@pytest.mark.parametrize(
    "text",
    [
        "a: 1\na: 2\n",
        "a: 1\n'a': 2\n",
        "a: {b: 1, b: 2}\n",
        "r:\n  - x: 1\n    x: 2\n",
        'a: 1\n"a": 2\n',
        "{a: 1, a: 1}\n",
    ],
)
def test_duplicate_keys_are_invalid(env: Env, text: str) -> None:
    env.main(text)
    invalid(env, "config.yaml")


@pytest.mark.parametrize(
    "text",
    [
        "1: a\n",
        "true: a\n",
        "null: a\n",
        "~: a\n",
        "1.5: a\n",
        "? [a, b]\n: c\n",
        "? {a: 1}\n: c\n",
        "a:\n  2: x\n",
    ],
)
def test_non_string_keys_are_invalid(env: Env, text: str) -> None:
    env.main(text)
    invalid(env, "config.yaml")


@pytest.mark.parametrize(
    "text",
    [
        "a: [\n",
        "a: 'unterminated\n",
        "a: b: c\n",
        "a:\n\t- x\n",
        "- a\n- b\n",
        "just a string\n",
        "42\n",
        "true\n",
        "a: 1\n---\nb: 2\n",
        "---\na: 1\n...\n---\nb: 2\n",
        "a: 1\n--- \n",
        "{\n",
        "]\n",
        'a: "\\ud800"\n',
        'a: "\\x00"\n',
        'a: "\\u0000"\n',
        '"\\x00": 1\n',
    ],
)
def test_malformed_and_non_mapping_documents_are_invalid(env: Env, text: str) -> None:
    env.main(text)
    invalid(env, "config.yaml")


@pytest.mark.parametrize(
    "raw",
    [b"a: \xff\xfe\n", b"a: \x00\n", b"\x00", b"a: \xc3\n", b"a: \xed\xa0\x80\n", b"a: \x01\n"],
)
def test_undecodable_and_unprintable_bytes_are_invalid(env: Env, raw: bytes) -> None:
    env.write("config.yaml", raw)
    invalid(env, "config.yaml")


def test_utf16_is_not_sniffed(env: Env) -> None:
    env.write("config.yaml", "a: 1\n".encode("utf-16"))
    invalid(env, "config.yaml")


def test_unicode_text_is_kept(env: Env) -> None:
    env.main("a: caf\u00e9 \u2603 \U0001f600\n'k\u00e9y': 'v'\n")
    assert env.data() == {"a": "caf\u00e9 \u2603 \U0001f600", "k\u00e9y": "v"}


def test_huge_integer_is_invalid_not_a_crash(env: Env) -> None:
    env.main("a: " + "9" * 100_000 + "\n")
    invalid(env, "config.yaml")


def test_alias_bomb_is_refused_at_the_first_alias_quickly(env: Env) -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    for index in range(1, 10):
        refs = ", ".join([f"*a{index - 1}"] * 9)
        lines.append(f"a{index}: &a{index} [{refs}]")
    env.main("\n".join(lines) + "\n")
    start = time.monotonic()
    invalid(env, "config.yaml")
    assert time.monotonic() - start < 2


def test_flow_nesting_at_the_depth_limit_loads_and_beyond_it_is_invalid(env: Env) -> None:
    ok = "a: " + "[" * (MAX_DEPTH - 1) + "]" * (MAX_DEPTH - 1) + "\n"
    env.main(ok)
    assert "a" in env.data()
    bad = "a: " + "[" * MAX_DEPTH + "]" * MAX_DEPTH + "\n"
    env.main(bad)
    invalid(env, "config.yaml")


@pytest.mark.parametrize("opener", ["[", "{a: ", "- ", "? "])
def test_very_deep_nesting_is_invalid_not_a_crash(env: Env, opener: str) -> None:
    depth = 100_000 if opener in ("[", "{a: ") else 2_000
    if opener in ("[", "{a: "):
        text = "a: " + opener * depth
    else:
        text = "".join(" " * i + opener + "\n" for i in range(depth))
    env.write("config.yaml", text[: MAX_FILE_BYTES - 1])
    start = time.monotonic()
    invalid(env, "config.yaml")
    assert time.monotonic() - start < 5


def test_block_nesting_beyond_the_limit_is_invalid(env: Env) -> None:
    text = "".join("  " * i + "k:\n" for i in range(MAX_DEPTH + 5)) + "  " * (MAX_DEPTH + 5) + "v\n"
    env.main(text)
    invalid(env, "config.yaml")


def test_long_scalar_and_wide_map_within_the_limit_load(env: Env) -> None:
    env.main("a: " + "x" * 200_000 + "\n")
    assert len(env.data()["a"]) == 200_000
    env.main("".join(f"k{i}: {i}\n" for i in range(5000)))
    assert len(env.data()) == 5000


def test_error_never_carries_file_content(env: Env) -> None:
    for text in (
        f"a: &x {SECRET}\n",
        f"a: {SECRET}\na: 2\n",
        f"a: [{SECRET}\n",
        f"{SECRET}: 1\n{SECRET}: 2\n",
        f"- {SECRET}\n",
        f"a: !!{SECRET} 1\n",
    ):
        env.main(text)
        with pytest.raises(ConfigError) as info:
            env.load()
        err = info.value
        assert SECRET not in str(err) and SECRET not in repr(err)
        assert SECRET not in err.key
        assert err.__cause__ is None and err.__context__ is None
        assert err.args == ("invalid configuration",) or SECRET not in repr(err.args)


# --- merge ---------------------------------------------------------------------------------


def test_rules_append_in_file_order(env: Env) -> None:
    env.main("rules:\n  - {name: r1}\n  - {name: r2}\n")
    env.write("config.d/20-b.yaml", "rules:\n  - {name: r4}\n")
    env.write("config.d/10-a.yaml", "rules:\n  - {name: r3}\n")
    assert [r["name"] for r in env.data()["rules"]] == ["r1", "r2", "r3", "r4"]


def test_a_dropin_with_only_rules_is_valid(env: Env) -> None:
    env.main("destinations: {d: {cmd: [/bin/true]}}\n")
    env.write("config.d/10-a.yaml", "rules: [{name: a}]\n")
    assert set(env.data()) == {"destinations", "rules"}


def test_duplicate_rule_name_across_files_is_invalid(env: Env) -> None:
    env.main("rules:\n  - {name: same}\n")
    env.write("config.d/10-a.yaml", "rules:\n  - {name: other}\n  - {name: same}\n")
    invalid(env, "rules.2.name")


def test_duplicate_rule_name_in_one_file_is_invalid(env: Env) -> None:
    env.main("rules:\n  - {name: a}\n  - {name: a}\n")
    invalid(env, "rules.1.name")


def test_rule_names_are_compared_exactly(env: Env) -> None:
    env.main("rules:\n  - {name: a}\n  - {name: A}\n  - {name: 'a '}\n  - {name: 1}\n")
    assert len(env.data()["rules"]) == 4


def test_post_fail_hooks_append(env: Env) -> None:
    env.main("hooks:\n  post-fail:\n    - {cmd: [/a]}\n")
    env.write("config.d/10-a.yaml", "hooks:\n  post-fail:\n    - {cmd: [/b]}\n")
    env.write("config.d/20-b.yaml", "hooks:\n  post-fail:\n    - {cmd: [/c]}\n")
    assert [h["cmd"] for h in env.data()["hooks"]["post-fail"]] == [["/a"], ["/b"], ["/c"]]


@pytest.mark.parametrize(
    ("section", "entry_a", "entry_b"),
    [
        ("destinations", "{cmd: [/a]}", "{cmd: [/b]}"),
        ("quiet_hours", "{timezone: UTC}", "{timezone: UTC}"),
    ],
)
def test_named_sections_merge_by_name_and_refuse_a_repeat(
    env: Env, section: str, entry_a: str, entry_b: str
) -> None:
    env.main(f"{section}:\n  one: {entry_a}\n")
    env.write("config.d/10-a.yaml", f"{section}:\n  two: {entry_b}\n")
    assert set(env.data()[section]) == {"one", "two"}
    env.write("config.d/20-b.yaml", f"{section}:\n  one: {entry_b}\n")
    invalid(env, f"{section}.one")


@pytest.mark.parametrize("sub", ["hosts", "tickets"])
def test_links_sections_merge_by_name_and_refuse_a_repeat(env: Env, sub: str) -> None:
    env.main(f"links:\n  {sub}:\n    one: v1\n")
    env.write("config.d/10-a.yaml", f"links:\n  {sub}:\n    two: v2\n")
    assert env.data()["links"][sub] == {"one": "v1", "two": "v2"}
    env.write("config.d/20-b.yaml", f"links:\n  {sub}:\n    one: v3\n")
    invalid(env, f"links.{sub}.one")


def test_links_hosts_and_tickets_in_different_files_merge(env: Env) -> None:
    env.main("links:\n  hosts: {h: github}\n")
    env.write("config.d/10-a.yaml", "links:\n  tickets: {t: 'https://x.test/'}\n")
    assert env.data()["links"] == {"hosts": {"h": "github"}, "tickets": {"t": "https://x.test/"}}


def test_a_repeated_name_with_an_odd_spelling_is_keyed_with_a_question_mark(env: Env) -> None:
    env.main("destinations:\n  'a b\u202e': {cmd: [/a]}\n")
    env.write("config.d/10-a.yaml", "destinations:\n  'a b\u202e': {cmd: [/b]}\n")
    invalid(env, "destinations.?")


@pytest.mark.parametrize(
    ("first", "second", "key"),
    [
        ("enabled: true\n", "enabled: false\n", "enabled"),
        ("lock_wait: 2s\n", "lock_wait: 3s\n", "lock_wait"),
        ("log: true\n", "log: true\n", "log"),
        (
            "defaults: {dedup_window: 1m}\n",
            "defaults: {dedup_window: 2m}\n",
            "defaults.dedup_window",
        ),
        ("retention: {history: 2d}\n", "retention: {history: 3d}\n", "retention.history"),
        ("cron: {env_file: /a}\n", "cron: {env_file: /b}\n", "cron.env_file"),
        ("zzz: 1\n", "zzz: 2\n", "?"),
        ("hooks: {other: 1}\n", "hooks: {other: 2}\n", "hooks.?"),
        ("enabled: true\n", "enabled:\n", "enabled"),
        ("x: [1]\n", "x: [2]\n", "?"),
        ("x: {a: 1}\n", "x: 2\n", "?"),
        ("x: 2\n", "x: {a: 1}\n", "?"),
    ],
)
def test_any_other_key_defined_twice_is_invalid(
    env: Env, first: str, second: str, key: str
) -> None:
    env.main(first)
    env.write("config.d/10-a.yaml", second)
    invalid(env, key)


def test_disjoint_keys_of_one_nested_map_merge(env: Env) -> None:
    env.main("retention: {history: 2d}\n")
    env.write("config.d/10-a.yaml", "retention: {event_ids: 3d}\n")
    assert env.data()["retention"] == {"history": "2d", "event_ids": "3d"}


def test_odd_key_names_are_never_echoed_in_the_key(env: Env) -> None:
    env.main(f"'{SECRET} x': 1\n")
    env.write("config.d/10-a.yaml", f"'{SECRET} x': 2\n")
    err = invalid(env, "?")
    assert SECRET not in err.key


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("rules: {a: 1}\n", "rules"),
        ("rules: x\n", "rules"),
        ("rules:\n", "rules"),
        ("destinations: [a]\n", "destinations"),
        ("destinations:\n", "destinations"),
        ("quiet_hours: 3\n", "quiet_hours"),
        ("links: [a]\n", "links"),
        ("links: {hosts: [a]}\n", "links.hosts"),
        ("links: {tickets: x}\n", "links.tickets"),
        ("hooks: x\n", "hooks"),
        ("hooks: {post-fail: {a: 1}}\n", "hooks.post-fail"),
    ],
)
def test_mergeable_sections_must_have_their_container_type(env: Env, text: str, key: str) -> None:
    env.main(text)
    invalid(env, key)
    env.main("enabled: true\n")
    env.write("config.d/10-a.yaml", text)
    invalid(env, key)


def test_merge_does_not_alias_values_between_loads(env: Env) -> None:
    env.main("rules: [{name: a}]\n")
    first = env.data()
    first["rules"].append({"name": "mutated"})
    assert env.data()["rules"] == [{"name": "a"}]


def test_rule_without_a_string_name_is_left_to_the_validator(env: Env) -> None:
    env.main("rules:\n  - {x: 1}\n  - {x: 1}\n  - not-a-map\n  - [list]\n  - {name: [a]}\n")
    assert len(env.data()["rules"]) == 5  # TODO(#7): rule shape and names are validated there


def test_yaml_error_in_a_dropin_names_the_dropin(env: Env) -> None:
    env.main()
    env.write("config.d/10-a.yaml", "a: &x 1\n")
    invalid(env, "config.d/10-a.yaml")
