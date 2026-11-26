"""What ``make live`` will do, printed before it does it: names and yes/no only, never values.

``python -m tests.live.plan`` prints it; ``describe`` is the pure part the offline test uses.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping

from tests.live.support import REPORT_DIR_VAR, TOOLS, tool_config


def describe(
    environ: Mapping[str, str], which: Callable[[str], str | None] = shutil.which
) -> str:
    lines = [
        "notice-route live suite (opt-in, side-effecting)",
        "",
        "It will run the real notifiers listed below and a real shell, in a private scratch",
        "home (the router's config and state never touch yours). Marked test messages go only",
        "to the destinations those notifiers' own configs name. It never runs crontab.",
        "",
    ]
    for name, spec in TOOLS.items():
        cfg, reason = tool_config(name, environ, need_credential=True, which=which)
        if cfg is not None:
            cred = f", credential variable {cfg.cred_env} (set)" if cfg.cred_env else ""
            cfg_note = "its own config designated" if cfg.config else "its default config"
            lines.append(f"  {name}: will run {cfg.binary}, {cfg_note}{cred}")
        else:
            lines.append(f"  {name}: its tests will be skipped ({reason})")
    where = environ.get(REPORT_DIR_VAR) or "a new private directory under the temp directory"
    lines += ["", f"The observation report is written to: {where}", "Read docs/live-testing.md first."]
    return "\n".join(lines)


def main() -> int:
    sys.stdout.write(describe(os.environ) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
