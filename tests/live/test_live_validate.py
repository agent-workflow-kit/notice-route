"""``validate`` of the real notifiers, directly and through the router's ``validate --probe``.

Direct calls run ``validate --json`` (plus ``--probe`` for ha-notice and wsl-notice) without
``--test-message``: no notification is sent, though ``--probe`` may contact the service.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import pytest

from tests.live.support import LiveRouter, Report, ToolConfig, run_tool

ALL = ["slack-notice", "ha-notice", "wsl-notice"]


@pytest.mark.parametrize("name", ALL)
def test_validate_json_real_output(name: str, make_tool: Callable[..., ToolConfig],
                                   report: Report) -> None:  # fmt: skip
    """FIDELITY (#29): the exact stdout and exit code of `validate --json [--probe]`. The router
    reads only the exit code and the boolean `enabled` (exit 22 is `{"outcome":"disabled"}`). The
    line count and trailing newline of stdout are recorded, not asserted."""
    cfg = make_tool(name, need_credential=cfg_needs(name))
    r = run_tool(cfg, cfg.spec.probe_args, os.environ, credential=True)
    report.record(f"validate/{name}", code=r.code, stdout=report.clip(r.out, 2000),
                  stderr=report.clip(r.err, 400), args=list(cfg.spec.probe_args),
                  stdout_lines=r.out.count("\n"),
                  stdout_ends_newline=r.out.endswith("\n"))  # fmt: skip
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


def cfg_needs(name: str) -> bool:
    return name != "wsl-notice"


def parsed(out: str) -> Any:
    try:
        return json.loads(out)
    except ValueError:
        return None


@pytest.mark.parametrize("name", ["slack-notice", "ha-notice"])
def test_validate_rejects_probe_grammar(name: str, make_tool: Callable[..., ToolConfig],
                                        report: Report) -> None:  # fmt: skip
    """FIDELITY: slack-notice rejects `--probe` (exit 2, an error envelope of code `usage`),
    ha-notice accepts it (exit 0, 12 or 22, never a usage envelope). The decision reads the
    stdout, not the exit code alone; an unloadable config (`config_invalid`) is a skip."""
    cfg = make_tool(name)
    r = run_tool(cfg, ("validate", "--json", "--probe"), os.environ)
    report.record(f"validate_probe_grammar/{name}", code=r.code, stdout=report.clip(r.out, 400))
    doc = parsed(r.out)
    error = doc.get("error") if isinstance(doc, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if code == "config_invalid":
        pytest.skip(f"{name} has no loadable config (set {cfg.spec.config_var})")
    if name == "slack-notice":
        assert r.code == 2, f"slack-notice validate --probe exit code {r.code}, expected 2"
        assert code == "usage", "slack-notice did not answer with a usage envelope"
    else:
        assert r.code in (0, 12, 22), f"ha-notice validate --probe exit code {r.code}"
        assert code != "usage", "ha-notice answered with a usage envelope"


@pytest.mark.parametrize("name", ["slack-notice", "ha-notice"])
def test_observe_whitespace_only_credential(name: str, make_tool: Callable[..., ToolConfig],
                                            report: Report) -> None:  # fmt: skip
    """Observation (#31 comment 3): plain `validate --json` (no `--probe`, so no network call;
    sibling interface docs) with a whitespace-only value under the real credential variable name.
    Recorded, never asserted; nothing is sent. wsl-notice has no credential."""
    cfg = make_tool(name, need_credential=True)
    assert cfg.cred_env
    environ = {**os.environ, cfg.cred_env: "   "}
    r = run_tool(cfg, ("validate", "--json"), environ, credential=True)
    report.record(f"whitespace_credential/{name}", code=r.code, stdout=report.clip(r.out, 1000),
                  stderr=report.clip(r.err, 300))  # fmt: skip


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
                         "rules": [{"name": "pt", "match": {"level_min": "info"},
                                    "notify": names}]})  # fmt: skip
    # Each probe takes up to lock_wait + timeout + 16 s, one after the other (interface.md).
    r = router.run("validate", "--probe", "--json", timeout=45.0 * len(found) + 30.0)
    report.record("router_validate_probe", code=r.code, stdout=report.clip(r.out, 3000),
                  stderr=report.clip(r.err, 400))  # fmt: skip
    assert r.code in (0, 12, 22), f"validate --probe exit code {r.code}"
    doc = r.json()
    assert doc["probe"] is True
    by_name = {d["name"]: d for d in doc["destinations"]}
    assert set(by_name) == set(names)
    if not all(d["cmd_ok"] for d in by_name.values()):
        pytest.skip(
            "the router refused a notifier path (cmd_ok false: a directory of its path is "
            "writable by others); see 'A refused notifier path' in docs/live-testing.md"
        )
    for d in by_name.values():  # the probe result the router derived from the notifier's output
        assert d["probe"] is not None
        assert d["probe"]["status"] in ("ok", "failed", "disabled")
        enabled = d["probe"]["enabled"]
        assert enabled is None or isinstance(enabled, bool)
    leaked = report.protected.leaks(r.out, r.err)
    assert not leaked, "credential value found in the validate output (redacted)"
