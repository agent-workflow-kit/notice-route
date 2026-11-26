"""``validate`` of the real notifiers, directly and through the router's ``validate --probe``.

Direct calls run ``validate --json`` (plus ``--probe`` for ha-notice and wsl-notice) without
``--test-message``: no notification is sent, though ``--probe`` may contact the service.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.live.support import LiveRouter, Report, ToolConfig, run_tool, window_around_now  # noqa: F401

ALL = ["slack-notice", "ha-notice", "wsl-notice"]


@pytest.mark.parametrize("name", ALL)
def test_validate_json_real_output(name: str, make_tool: Callable[..., ToolConfig],
                                   report: Report) -> None:  # fmt: skip
    """FIDELITY (#29): the exact stdout and exit code of `validate --json [--probe]`. The router
    reads only the exit code and the boolean `enabled` (exit 22 reports `{"outcome":"disabled"}`)."""
    cfg = make_tool(name, need_credential=cfg_needs(name))
    r = run_tool(cfg, cfg.spec.probe_args, os.environ, credential=True)
    report.record(f"validate/{name}", code=r.code, stdout=r.out[:2000], stderr=r.err[:400],
                  args=list(cfg.spec.probe_args))  # fmt: skip
    if r.code == 22:
        assert json.loads(r.out) == {"outcome": "disabled"}
        return
    assert r.code in (0, 2, 12), r.code
    doc = json.loads(r.out)
    if "error" in doc:  # an unloadable config: the error envelope (exit 2)
        assert r.code == 2
        return
    assert isinstance(doc["enabled"], bool)
    assert isinstance(doc["checks"], list)
    assert r.out.endswith("\n") and r.out.count("\n") == 1


def cfg_needs(name: str) -> bool:
    return name != "wsl-notice"


@pytest.mark.parametrize("name", ["slack-notice", "ha-notice"])
def test_validate_rejects_probe_grammar(name: str, make_tool: Callable[..., ToolConfig],
                                        report: Report) -> None:  # fmt: skip
    """FIDELITY: slack-notice rejects `--probe` (exit 2 usage), ha-notice accepts it."""
    cfg = make_tool(name)
    r = run_tool(cfg, ("validate", "--json", "--probe"), os.environ)
    report.record(f"validate_probe_grammar/{name}", code=r.code, stdout=r.out[:400])
    if name == "slack-notice":
        assert r.code == 2
    else:
        assert r.code != 2


def test_router_validate_probe(make_tool: Callable[..., ToolConfig], router: LiveRouter,
                               report: Report) -> None:  # fmt: skip
    """`notice-route validate --probe --json` against each real notifier that is designated."""
    found: dict[str, ToolConfig] = {}
    for name in ALL:
        try:
            found[name] = make_tool(name, need_credential=cfg_needs(name))
        except pytest.skip.Exception:
            continue
    if not found:
        pytest.skip("no real notifier is designated")
    dests: dict[str, Any] = {}
    names: list[str] = []
    for name, cfg in found.items():
        dest = name.split("-")[0]
        dests[dest] = {"kind": cfg.spec.kind, "cmd": cfg.cmd, "timeout": "20s",
                       **({"env": [cfg.cred_env]} if cfg.cred_env else {})}  # fmt: skip
        names.append(dest)
    router.pass_names = tuple(c.cred_env for c in found.values() if c.cred_env)
    router.write_config({"destinations": dests,
                         "rules": [{"name": "pt", "match": {"level_min": "info"}, "notify": names}]})  # fmt: skip
    r = router.run("validate", "--probe", "--json")
    report.record("router_validate_probe", code=r.code, stdout=r.out[:3000], stderr=r.err[:400])
    assert r.code in (0, 12, 22), r.code
    doc = r.json()
    assert doc["probe"] is True
    by_name = {d["name"]: d for d in doc["destinations"]}
    assert set(by_name) == set(names)
    assert all(d["cmd_ok"] for d in by_name.values())
    for d in by_name.values():  # the probe result the router derived from the notifier's output
        assert d["probe"] is not None
        assert d["probe"]["status"] in ("ok", "failed", "disabled")
        assert d["probe"]["enabled"] in (True, False, None)
    assert not report.protected.leaks(r.out, r.err)
    assert Path(router.home).exists()
