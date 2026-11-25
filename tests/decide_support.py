"""Builders for the decision-order tests (#18): a typed config without files, a notice, and a
gateway with a private home and a fake clock."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from notice_route.decide import Notice
from notice_route.dryrun import dedupe_digest
from notice_route.gateway import FakeClock, Gateway
from notice_route.validate import Config, Destination, Links, QuietWindow

DAY0 = 1_699_920_000  # 2023-11-14T00:00:00Z, a Tuesday
NOON = DAY0 + 12 * 3600  # outside the quiet window
NIGHT = DAY0 + 22 * 3600 + 800  # 22:13:20, inside it; also the hour of 1_700_000_000
NEXT_MORNING = DAY0 + 86400 + 6 * 3600  # 06:00 the next day: the end of the window

# 22:00 to 06:00 UTC on every day. ``timezone.utc`` stands in for a zone so that the tests need
# no system time zone database.
QUIET = QuietWindow(
    name="night",
    timezone="UTC",
    zone=cast(ZoneInfo, UTC),
    days=(0, 1, 2, 3, 4, 5, 6),
    start=22 * 60,
    end=6 * 60,
)


def dest(name: str = "d", **kw: Any) -> Destination:
    base: dict[str, Any] = {
        "name": name,
        "kind": "generic",
        "cmd": ("/bin/true",),
        "timeout": 10.0,
        "floor": "info",
        "quiet": None,
        "max_per_hour": 30,
        "env": (),
        "enabled": True,
    }
    base.update(kw)
    return Destination(**base)


def make_config(*dests: Destination, retention_event_ids: float = 14 * 86400.0) -> Config:
    return Config(
        enabled=True,
        destinations={d.name: d for d in dests},
        quiet_hours={"night": QUIET},
        links=Links({}, {}),
        rules=(),
        default_dedup_window=600.0,
        lock_wait=2.0,
        retention_history=30 * 86400.0,
        retention_event_ids=retention_event_ids,
        hooks_post_fail=(),
        cron_env_file=None,
        log=True,
    )


def notice(
    level: str = "warn",
    message: str = "m",
    *,
    title: str = "t",
    source: str = "s",
    hook: str | None = "h",
    event_id: int | str | None = None,
    window: int = 600,
    **kw: Any,
) -> Notice:
    return Notice(
        source=source,
        hook=hook,
        level=level,
        rule="r",
        title=title,
        message=message,
        link="",
        dedupe=dedupe_digest(source, hook, level, title, message, ""),
        dedup_window=window,
        event_id=event_id,
        **kw,
    )


def gateway(home: Path, wall: float = NOON, env: dict[str, str] | None = None) -> Gateway:
    return Gateway(
        passwd_home=lambda: str(home),
        clock=FakeClock(wall=wall),
        environ=lambda: dict(env or {}),
    )
