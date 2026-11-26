"""Live checks that need no notifier and no network: the real shell, real process groups and
the real operating system under the `post-fail` hook (#27 notes for the macOS run). They send
nothing and call no service: the failing destination is a local script. Marked `live_local` too,
so the offline harness test can run the rest of the suite without them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.live.support import LiveRouter, Report

pytestmark = pytest.mark.live_local

FIXED = {
    "PATH",
    "LANG",
    "LC_ALL",
    "NOTICE_ROUTE_HOOK",
    "NOTICE_ROUTE_ID",
    "NOTICE_ROUTE_OBJECT_TYPE",
}
SH_ADDED = {"PWD", "SHLVL", "_", "OLDPWD"}  # what a POSIX sh may add on its own


def script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o700)
    return path


def setup_failing(router: LiveRouter, scratch: Path, hook_body: str, *, async_: bool,
                  timeout: str = "10s") -> None:  # fmt: skip
    failing = script(scratch / "fail.sh", "cat > /dev/null\nexit 3\n")
    hook = script(scratch / "hook.sh", hook_body)
    router.write_config({
        "destinations": {"broken": {"kind": "generic", "cmd": [str(failing)]}},
        "rules": [{"name": "live", "match": {"source": "live-test", "level_min": "info"},
                   "notify": ["broken"]}],
        "hooks": {"post-fail": [{"cmd": str(hook), "timeout": timeout, "async": async_}]},
    })  # fmt: skip
    r = router.send("TEST MESSAGE: a local failing destination, nothing leaves this machine",
                    title="notice-route live test")  # fmt: skip
    assert r.code == 12, f"send exit code {r.code}"
    assert r.json()["destinations"][0]["code"] == "generic_failed"


def wait_for(path: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return True
        time.sleep(0.1)
    return False


def test_sync_hook_environment_and_payload(
    router: LiveRouter, scratch: Path, report: Report
) -> None:
    """#27 note 2: the hook runs as `sh -c <cmd>`; its environment is exactly the documented set
    plus what `sh` adds itself (observed on this platform: record, compare on macOS)."""
    out = scratch / "env.txt"
    setup_failing(router, scratch,
                  f"env > {out}.tmp\nmv {out}.tmp {out}\ncat > {scratch}/payload.json\n",
                  async_=False)  # fmt: skip
    f = router.run("flush", "--json")
    assert f.code in (0, 12), f"flush exit code {f.code}"
    names = {line.split("=", 1)[0] for line in out.read_text().splitlines() if "=" in line}
    names = {n for n in names if n.isidentifier()}
    report.record("hook/sync_env", names=sorted(names), extra_over_fixed=sorted(names - FIXED))
    assert names - FIXED <= SH_ADDED, sorted(names - FIXED - SH_ADDED)
    assert {"NOTICE_ROUTE_HOOK", "NOTICE_ROUTE_ID", "NOTICE_ROUTE_OBJECT_TYPE"} <= names
    payload = json.loads((scratch / "payload.json").read_text())
    assert payload["hook"] == "post-fail" and payload["object_id"] == "broken"
    assert payload["code"] == "generic_failed"
    carried = any(t in json.dumps(payload) for t in ("TEST MESSAGE", "notice-route live test"))
    assert not carried, "the hook payload carries the title or message"  # no title, message, link


def test_async_hook_outlives_flush_and_hits_its_timeout(router: LiveRouter, scratch: Path,
                                                        report: Report) -> None:  # fmt: skip
    """#27 note 3: an `async` hook's detached supervisor survives the end of `flush` and ends the
    hook at its timeout. The hook traps SIGTERM, writes `termed` and exits; it loops in short
    steps for at most 20 s on its own. `termed` appearing after `flush` returned proves the
    supervisor outlived `flush` and sent the TERM at the hook's 2 s timeout. The order is
    recorded (flush return time against the mtime of `termed`). Asserted: `termed` did not exist
    when `flush` returned, and its mtime is at least 1 s after the mtime of `started` (the
    hook's timeout is 2 s); no other timing is asserted."""
    started = scratch / "started"
    termed = scratch / "termed"
    body = (
        f"trap 'echo term > {termed}; exit 0' TERM\n"
        f"echo up > {started}\n"
        "i=0\nwhile [ $i -lt 200 ]; do sleep 0.1; i=$((i+1)); done\n"
    )
    setup_failing(router, scratch, body, async_=True, timeout="2s")
    f = router.run("flush", "--json")
    already_termed = termed.exists()  # taken at once: flush must not have waited for the hook
    flush_returned = time.time()
    assert f.code in (0, 12), f"flush exit code {f.code}"
    assert wait_for(started, 10), "the async hook never started"
    appeared = wait_for(termed, 25)
    report.record("hook/async", started=started.exists(), termed=appeared,
                  termed_before_flush_returned=already_termed, flush_returned_at=flush_returned,
                  termed_mtime=termed.stat().st_mtime if appeared else None)  # fmt: skip
    assert appeared, "the hook was never terminated at its timeout"
    assert not already_termed, "flush waited for the async hook (`termed` existed on return)"
    lived = termed.stat().st_mtime - started.stat().st_mtime
    assert lived >= 1.0, f"the hook ended {lived:.2f} s after it started; its timeout is 2 s"


def test_hook_recursion_guard_with_the_real_environment(router: LiveRouter, scratch: Path,
                                                        report: Report) -> None:  # fmt: skip
    """A process started from the hook that calls `send` is refused (exit 20, `hook_recursion`)."""
    result = scratch / "recursion.json"
    probe = router.argv("send", "--stdin-json", "--json", "--source", "live-test")
    quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in probe)
    setup_failing(router, scratch,
                  f"cat > /dev/null\necho '{{\"message\":\"x\"}}' | {quoted} > {result}\n",
                  async_=False)  # fmt: skip
    f = router.run("flush", "--json")
    assert f.code in (0, 12), f"flush exit code {f.code}"
    assert wait_for(result, 10)
    doc = json.loads(result.read_text())
    report.record("hook/recursion", stdout=doc)
    assert doc["error"]["reason"] == "hook_recursion"
