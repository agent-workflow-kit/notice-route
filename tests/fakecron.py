"""Build and drive the fake ``crontab`` of ``tests/fakebin/crontab.py``.

``build_fake_crontab`` writes an executable stub whose first line is the absolute test
interpreter and which bakes in its state directory, so a scrubbed environment cannot lose it.
The state directory holds the stored table, the call log and the behavior file.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.fakes import FAKEBIN

_STUB = """#!{python} -I
import sys

sys.path.insert(0, {fakebin!r})
import crontab

sys.exit(crontab.main(sys.argv[1:], {state!r}))
"""


@dataclass
class FakeCrontab:
    bin_dir: Path
    state: Path

    @property
    def stub(self) -> Path:
        return self.bin_dir / "crontab"

    def set_table(self, data: bytes | str) -> None:
        self.state.joinpath("table").write_bytes(data.encode() if isinstance(data, str) else data)

    def table(self) -> bytes | None:
        path = self.state / "table"
        return path.read_bytes() if path.exists() else None

    def behave(self, **behavior: Any) -> None:
        self.state.joinpath("behavior.json").write_text(json.dumps(behavior), "utf-8")

    def calls(self) -> list[dict[str, Any]]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()]

    def kinds(self) -> list[str]:
        return [c["kind"] for c in self.calls()]

    def writes(self) -> list[bytes]:
        path = self.state / "writes.log"
        if not path.exists():
            return []
        parts = path.read_bytes().split(b"\0--end-of-write--\0")
        return parts[:-1]


def build_fake_crontab(directory: Path) -> FakeCrontab:
    python = sys.executable
    if any(c.isspace() for c in python):
        raise RuntimeError("the test interpreter path must not contain whitespace")
    bin_dir = directory / "bin"
    state = directory / "state"
    bin_dir.mkdir(parents=True)
    state.mkdir()
    bin_dir.chmod(0o755)
    stub = bin_dir / "crontab"
    stub.write_text(
        _STUB.format(python=python, fakebin=str(FAKEBIN), state=str(state)),
        "utf-8",
    )
    stub.chmod(0o755)
    return FakeCrontab(bin_dir, state)
