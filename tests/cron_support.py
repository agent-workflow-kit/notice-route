"""A rig for the tests of ``cron install`` and ``cron uninstall``: a private home, a fake
``notice-route`` program, the fake ``crontab`` first on the router's ``PATH`` and a gateway whose
wall clock fails the test when read (the commands never look at the time)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from notice_route.cli import main
from notice_route.gateway import Gateway
from tests.fakecron import FakeCrontab
from tests.test_cli_rules_validate import write_config

GOLDEN = Path(__file__).resolve().parent / "golden" / "cron"
SYSTEM_PATH = "/usr/bin:/bin"


class NoClock:
    """A clock the cron commands must not read."""

    def wall(self) -> float:
        raise AssertionError("cron read the wall clock")

    def monotonic(self) -> float:
        import time

        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        raise AssertionError("cron slept")


@dataclass
class Rig:
    home: Path
    tmp: Path
    exe: Path
    crontab: FakeCrontab
    capsys: pytest.CaptureFixture[str]

    def gateway(
        self,
        *,
        argv0: str | None = None,
        path: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Gateway:
        env = dict(environ or {})
        env["PATH"] = path if path is not None else f"{self.crontab.bin_dir}:{SYSTEM_PATH}"
        first = str(self.exe) if argv0 is None else argv0
        return Gateway(
            clock=NoClock(),
            passwd_home=lambda: str(self.home),
            environ=lambda: env,
            argv0=lambda: first,
            exec_check_from=str(self.tmp),
        )

    def run(self, *argv: str, gateway: Gateway | None = None) -> tuple[int, str, str]:
        code = main(["cron", *argv], gateway or self.gateway())
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err

    def run_json(self, *argv: str, gateway: Gateway | None = None) -> tuple[int, dict[str, Any]]:
        code, out, err = self.run(*argv, "--json", gateway=gateway)
        assert out.endswith("\n") and out.count("\n") == 1, (out, err)
        return code, json.loads(out)

    def config(self, config: dict[str, Any]) -> None:
        write_config(self.home, config)

    def env_file(self, name: str = "cron.env", mode: int = 0o600) -> Path:
        directory = self.tmp / "envs"
        directory.mkdir(exist_ok=True)
        directory.chmod(0o700)
        path = directory / name
        path.write_text("A_VALUE=canary-env-9d41\n")
        path.chmod(mode)
        return path

    def expected_block(self, env_file: Path | None = None) -> str:
        name = "install.txt" if env_file is None else "install_env.txt"
        text = (GOLDEN / name).read_text("utf-8")
        text = text.replace("@EXE@", os.path.realpath(self.exe))
        text = text.replace("@DIR@", os.path.dirname(os.path.realpath(self.exe)))
        if env_file is not None:
            text = text.replace("@ENV@", str(env_file))
        return text
