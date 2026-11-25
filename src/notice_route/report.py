"""The documents of ``rules`` and offline ``validate`` (#9), built from a validated config.

Both are pure over their inputs except for the gateway calls ``validate`` makes: ``check_exec``,
``check_env_file`` (neither reads the file), ``read_state`` (read-only) and the environment
lookup. ``validate`` never runs a command, never reads the env file and never writes.

Choices where the interface is silent (the narrower reading):

- ``rules`` lists the destinations the rules reference (in config order) and the quiet windows
  those destinations reference; a destination no rule uses is reported by ``validate`` instead.
- A config with no destinations is still a config: ``validate`` reports ``ok`` with no
  destinations; only a missing config is ``no_op`` (exit 22).
- ``send_worst_case_s`` is ``lock_wait`` plus the largest destination ``timeout`` (of every
  destination, enabled or not) plus 17.
- ``flush_stale`` fires when no flush ever ran or the last started more than 300 s ago.
- ``alert_unrouted`` fires only when no rule can route a ``post-fail`` payload of source
  ``notice-route``: a rule counts as possibly routing it unless it is a ``drop`` rule, has
  ``level_min`` or ``repo`` (a notice-only condition, and a field the payload lacks), or its
  ``source`` or ``hook`` cannot match. ``labels`` and ``where`` are not judged.
- ``rule_unreachable``: an earlier rule covers a later one when every condition of the earlier
  rule is in the later one with the same value (``level_min`` and ``where`` values compare by
  type and value) or, for ``source``, ``hook`` and ``repo``, an earlier ``*`` value is a prefix of
  the later value. An earlier rule with an empty ``match`` covers every later rule.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .gateway import Gateway
from .match import rule_covers, wildcard_matches
from .validate import DAYS, Config, Match, Rule

__all__ = ["env_unset", "rules_document", "validate_document"]

HOOK_TIMEOUT_S = 30.0  # the default hook timeout the worst case is compared with
FLUSH_STALE_S = 300.0
_WORST_CASE_EXTRA = 17.0  # 10 s to the kill deadline, 5 s TERM grace, 1 s read, 1 s database
_DESTINATION_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def _number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# --- rules -----------------------------------------------------------------------------------


def _match(match: Match) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("source", "hook", "repo"):
        value = getattr(match, key)
        if value is not None:
            out[key] = value
    if match.labels:
        out["labels"] = list(match.labels)
    if match.where:
        out["where"] = dict(match.where)
    if match.level_min is not None:
        out["level_min"] = match.level_min
    return out


def _rule(rule: Rule) -> dict[str, Any]:
    return {
        "name": rule.name,
        "match": _match(rule.match),
        "destinations": list(rule.notify),
        "level": rule.level,
        "drop": rule.drop,
        "pass_through": not rule.drop and rule.message is None,
        "dedup_window_s": None if rule.drop else _number(rule.dedup_window),
    }


def rules_document(config: Config | None) -> dict[str, Any]:
    """The ``rules --json`` document; empty lists when there is no config."""
    if config is None:
        return {"rules": [], "destinations": [], "quiet_hours": []}
    used = {name for rule in config.rules for name in rule.notify}
    destinations = [d for d in config.destinations.values() if d.name in used]
    windows = {d.quiet for d in destinations if d.quiet is not None}
    return {
        "rules": [_rule(rule) for rule in config.rules],
        "destinations": [
            {
                "name": d.name,
                "kind": d.kind,
                "floor": d.floor,
                "quiet": d.quiet,
                "max_per_hour": d.max_per_hour,
                "enabled": d.enabled,
            }
            for d in destinations
        ],
        "quiet_hours": [
            {
                "name": w.name,
                "timezone": w.timezone,
                "days": [DAYS[i] for i in w.days],
                "start": _clock(w.start),
                "end": _clock(w.end),
            }
            for w in config.quiet_hours.values()
            if w.name in windows
        ],
    }


# --- validate --------------------------------------------------------------------------------


def _alert_routed(rules: Sequence[Rule]) -> bool:
    """Whether a post-fail alert (source ``notice-route``, hook ``post-fail``) may be routed.

    Rules are walked in order, as the router does (the first match wins). A rule that cannot
    match the sample is skipped. A rule that certainly matches it (only ``source`` and ``hook``
    conditions, or none) ends the walk: routed if it notifies, not routed if it drops. A rule
    that might match (``labels`` or ``where``) counts as routing when it notifies and is skipped
    when it drops. ``level_min`` and ``repo`` rules never count as routing and never end the walk.
    """
    for rule in rules:
        match = rule.match
        if match.source is not None and not wildcard_matches(match.source, "notice-route"):
            continue
        if match.hook is not None and not wildcard_matches(match.hook, "post-fail"):
            continue
        if match.level_min is not None or match.repo is not None:
            continue
        if match.labels or match.where:
            if not rule.drop:
                return True
            continue
        return not rule.drop
    return False


def env_unset(names: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    """The variables of ``names`` that are missing or empty in ``environ`` (``validate`` reports
    them; ``decide`` defers a destination that has one)."""
    return [n for n in names if environ.get(n, "") == ""]


def worst_case_s(config: Config) -> float:
    longest = max((d.timeout for d in config.destinations.values()), default=0.0)
    return round(config.lock_wait + longest + _WORST_CASE_EXTRA, 3)


def _warning(
    code: str, message: str, *, rule: str | None = None, destination: str | None = None
) -> dict[str, Any]:
    return {"code": code, "rule": rule, "destination": destination, "message": message}


def _warnings(
    config: Config,
    worst: float,
    flush_age: int | None,
    env_missing: bool,
    env_file_ok: bool,
    orphans: dict[str, int],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    used = {name for rule in config.rules for name in rule.notify}
    for name in config.destinations:
        if name not in used:
            out.append(
                _warning(
                    "destination_unused", "no rule notifies this destination", destination=name
                )
            )
    for index, rule in enumerate(config.rules):
        if not rule.drop and rule.level is None and rule.match.level_min not in (None, "error"):
            out.append(
                _warning(
                    "level_from_notice",
                    "the rule sets no level, so the sender chooses it above level_min",
                    rule=rule.name,
                )
            )
        if any(rule_covers(config.rules[i].match, rule.match) for i in range(index)):
            out.append(
                _warning(
                    "rule_unreachable",
                    "an earlier rule matches everything this one does",
                    rule=rule.name,
                )
            )
    if worst > HOOK_TIMEOUT_S:
        out.append(
            _warning(
                "worst_case_over_hook_timeout",
                "send_worst_case_s is above the default hook timeout of 30 seconds",
            )
        )
    if flush_age is None or flush_age > FLUSH_STALE_S:
        out.append(_warning("flush_stale", "no flush started in the last 5 minutes"))
    if env_missing:
        out.append(
            _warning("flush_env_missing", "the last flush skipped a destination as env_missing")
        )
    if config.cron_env_file is not None and not env_file_ok:
        out.append(
            _warning("env_file_unsafe", "cron.env_file is missing or fails the safety check")
        )
    if not config.hooks_post_fail and not _alert_routed(config.rules):
        out.append(
            _warning(
                "alert_unrouted", "no rule routes a post-fail alert and no post-fail hook exists"
            )
        )
    for name, count in sorted(orphans.items()):
        if name in config.destinations:
            continue
        shown = name if _DESTINATION_NAME.fullmatch(name) else None
        out.append(
            _warning(
                "orphaned_queue",
                f"{count} queued rows belong to a destination that is not configured",
                destination=shown,
            )
        )
    return out


def validate_document(config: Config | None, gateway: Gateway) -> tuple[dict[str, Any], int]:
    """The ``validate --json`` document and its exit code (0, 12 or 22).

    Raises :class:`HomeError` and :class:`StateError` from the state read.
    """
    if config is None:
        doc: dict[str, Any] = {
            "outcome": "no_op",
            "probe": False,
            "destinations": [],
            "warnings": [],
            "send_worst_case_s": 0,
            "flush_age_s": None,
        }
        return doc, 22
    state = gateway.read_state()
    environ = gateway.environ()
    flush_age: int | None = None  # whole seconds: the printed value is the one compared
    if state.flush_last is not None:
        flush_age = int(max(0.0, gateway.wall() - state.flush_last))
    destinations: list[dict[str, Any]] = []
    for d in config.destinations.values():
        destinations.append(
            {
                "name": d.name,
                "cmd_ok": gateway.check_exec(d.cmd[0]),
                "env_unset": env_unset(d.env, environ),
                "probe": None,  # TODO(#29): validate --probe fills this
                "surfaced_today": list(state.surfaced_today.get(d.name, ())),
            }
        )
    env_file_ok = config.cron_env_file is None or gateway.check_env_file(config.cron_env_file)
    worst = worst_case_s(config)
    failed = any(not d["cmd_ok"] for d in destinations)
    doc = {
        "outcome": "failed" if failed else "ok",
        "probe": False,
        "destinations": destinations,
        "warnings": _warnings(
            config,
            worst,
            flush_age,
            state.flush_env_missing is not None,
            env_file_ok,
            dict(state.queue_by_destination),
        ),
        "send_worst_case_s": _number(worst),
        "flush_age_s": flush_age,
    }
    return doc, 12 if failed else 0
