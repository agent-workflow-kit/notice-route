"""Validation of the merged config into typed objects (#7).

``validate_config(data, gateway)`` takes the merged raw mapping that :func:`load_config` returns
and builds a :class:`Config`, or raises :class:`ConfigError` (``config_invalid``) whose ``key``
is the key path of the first problem. ``load_valid_config`` is both steps in one.

Rules kept here (docs/interface.md: Destinations, Quiet hours, Rules, Links, Config):

- Every key is checked for type, grammar and range; an unknown key is refused. Booleans are never
  numbers and numbers are never booleans.
- The error key holds only known key names, a rule index, or an entry name that is plain
  (``[A-Za-z0-9][A-Za-z0-9._-]{0,63}``); any other name shows as ``?`` and nothing after a ``?``
  is shown. A value never reaches the key or the message (C-40). A problem inside a list or a
  ``where`` map is keyed by the list or map itself.
- Durations are a number of seconds or ``[0-9]+(\\.[0-9]+)?(ms|s|m)?``; ``h`` and ``d`` are
  accepted only by ``defaults.dedup_window``, a rule's ``dedup_window`` and ``retention.*``.
- Cross-references (a rule's destinations, a destination's quiet window) must resolve.
- Time zones load through :meth:`Gateway.load_zone` (system database only).

Templates and links are checked for grammar only.

The rule matcher (``match.py``) consumes :class:`Match`.
``render.py`` (#11) renders ``Rule.title`` and ``Rule.message`` and reuses ``TOKEN_PATH``.
TODO(#12): link building consumes :class:`Links` and ``Rule.link``.
The ``validate`` command (``report.py``) calls ``load_valid_config`` and adds its warnings.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from .config import ConfigError, load_config
from .gateway import Gateway, ZoneError

__all__ = [
    "DAYS",
    "LEVELS",
    "TEMPLATE",
    "TOKEN_PATH",
    "Config",
    "Destination",
    "Hook",
    "Level",
    "Links",
    "Match",
    "QuietWindow",
    "Rule",
    "load_valid_config",
    "validate_config",
]

Level = Literal["info", "warn", "action", "error"]
Kind = Literal["slack-notice", "ha-notice", "wsl-notice", "generic"]
WhereValue = str | int | bool

LEVELS: tuple[Level, ...] = ("info", "warn", "action", "error")  # ascending
KINDS: tuple[Kind, ...] = ("slack-notice", "ha-notice", "wsl-notice", "generic")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

MAX_DESTINATIONS = 32
MAX_RULES = 256
MAX_NOTIFY = 8
MAX_INCLUDE_TEXT = 16
MAX_LABELS = 32
MAX_WHERE = 16
MAX_CMD = 32
MAX_ARG = 4096
MAX_TEMPLATE = 2000
MAX_LINK = 1024
MAX_MATCH_VALUE = 256
MAX_WHERE_VALUE = 1024
_DAY = 86400.0
_RETENTION_MAX = 36500 * _DAY  # 100 years: a bound the interface leaves open
_HOOK_TIMEOUT_MAX = 3600.0  # likewise: a hook's range is not stated; 1s to 1h

_NAME_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # a plain entry name (C-40)
_DESTINATION_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_RULE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SITE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_ENV_FIXED = frozenset({"PATH", "LANG", "LC_ALL", "WSL_INTEROP", "WSL_DISTRO_NAME"})
_FORBIDDEN_ARG = re.compile(r"(?:send|--stdin-json|--json|--actor|--dedupe-key)(?:=[\s\S]*)?")
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_DURATION = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)?")
_UNITS = {
    None: Fraction(1),
    "ms": Fraction(1, 1000),
    "s": Fraction(1),
    "m": Fraction(60),
    "h": Fraction(3600),
    "d": Fraction(86400),
}

_HOST = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
_HOST_KEY = re.compile(r"(" + _HOST + r")(?::([0-9]{1,5}))?")
_URL = re.compile(r"https://(" + _HOST + r")(?::([0-9]{1,5}))?(/[A-Za-z0-9._~/%?=&+-]*)?")
_LABEL = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}=[A-Za-z0-9._:/#@+=,-]{0,256}")

_PATH = r"[A-Za-z_][A-Za-z0-9_]{0,63}(?:\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,3}"
# A token path: ``labels.<key>`` or a field path of up to four parts.
TOKEN_PATH = r"(?:labels\.[a-z0-9][a-z0-9_.-]{0,63}|(?!labels\.)" + _PATH + r")"
_TOKEN_PATH = re.compile(TOKEN_PATH)
_WHERE_KEY = re.compile(_PATH)
# A template: literal text and ``{{path}}`` tokens; a ``{{`` that does not open a token is
# refused, a ``}}`` outside a token is literal. Checked after the 2000-character cap.
TEMPLATE = re.compile(r"(?:(?!\{\{)[\s\S]|\{\{" + TOKEN_PATH + r"\}\})*")
_LINK_TOKENS = frozenset({"{{pr_url}}", "{{ticket_url}}"})


# --- typed result ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Destination:
    name: str
    kind: Kind
    cmd: tuple[str, ...]
    timeout: float
    floor: Level
    quiet: str | None
    max_per_hour: int
    env: tuple[str, ...]
    enabled: bool


@dataclass(frozen=True, slots=True)
class QuietWindow:
    """``days`` are weekday numbers (0 is Monday); ``start`` and ``end`` are minutes of the day."""

    name: str
    timezone: str
    zone: ZoneInfo
    days: tuple[int, ...]
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Links:
    hosts: dict[str, Literal["github", "gitlab"]]
    tickets: dict[str, str]


@dataclass(frozen=True, slots=True)
class Match:
    """Conditions as written; ``match.py`` gives them meaning. ``where`` keeps file order."""

    source: str | None
    hook: str | None
    repo: str | None
    labels: tuple[str, ...]
    where: tuple[tuple[str, WhereValue], ...]
    level_min: Level | None


@dataclass(frozen=True, slots=True)
class Rule:
    """A drop rule has no ``notify`` and no ``level``; a pass-through rule has no ``message``.
    Templates are the validated source text (rendering is #11's)."""

    name: str
    match: Match
    drop: bool
    notify: tuple[str, ...]
    level: Level | None
    title: str | None
    message: str | None
    link: str | None
    dedup_window: float
    include_text: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Hook:
    cmd: str
    timeout: float
    is_async: bool


@dataclass(frozen=True, slots=True)
class Config:
    """The validated config. Durations are seconds."""

    enabled: bool
    destinations: dict[str, Destination]
    quiet_hours: dict[str, QuietWindow]
    links: Links
    rules: tuple[Rule, ...]
    default_dedup_window: float
    lock_wait: float
    retention_history: float
    retention_event_ids: float
    hooks_post_fail: tuple[Hook, ...]
    cron_env_file: str | None
    log: bool


# --- helpers ---------------------------------------------------------------------------------

Path = tuple[str, ...]


def _bad(path: Path) -> ConfigError:
    return ConfigError("config_invalid", ".".join(path))


def _entry(name: Any) -> str:
    """An entry name as it may appear in a key: plain names only (C-40)."""
    return name if isinstance(name, str) and _NAME_SEGMENT.fullmatch(name) else "?"


def _mapping(value: Any, path: Path) -> dict[Any, Any]:
    """A mapping; its keys are not known to be strings (a caller may hand in anything)."""
    if not isinstance(value, dict):
        raise _bad(path)
    return cast("dict[Any, Any]", value)


def _closed(value: Any, path: Path, allowed: frozenset[str]) -> dict[str, Any]:
    """A mapping with no key outside ``allowed``: the first unknown key is ``<path>.?``."""
    mapping = _mapping(value, path)
    for key in mapping:
        if key not in allowed:
            raise _bad((*path, "?"))
    return mapping


def _boolean(value: Any, path: Path) -> bool:
    if not isinstance(value, bool):
        raise _bad(path)
    return value


def _integer(value: Any, path: Path, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _bad(path)
    return value


def _string(value: Any, path: Path, *, low: int = 1, high: int = MAX_ARG) -> str:
    if not isinstance(value, str) or not low <= len(value) <= high or "\x00" in value:
        raise _bad(path)
    return value


def _pattern(value: Any, path: Path, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _bad(path)
    return value


def _list(value: Any, path: Path, low: int, high: int) -> list[Any]:
    if not isinstance(value, list) or not low <= len(cast("list[Any]", value)) <= high:
        raise _bad(path)
    return cast("list[Any]", value)


def _level(value: Any, path: Path) -> Level:
    if not isinstance(value, str) or value not in LEVELS:
        raise _bad(path)
    return cast(Level, value)


def _duration(value: Any, path: Path, low: float, high: float, *, long: bool = False) -> float:
    """Seconds from a number or a duration string; ``h`` and ``d`` only when ``long``. The range
    is inclusive. Nothing about the value reaches the error."""
    if isinstance(value, bool):
        raise _bad(path)
    exact: Fraction
    if isinstance(value, int):
        exact = Fraction(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise _bad(path)
        exact = Fraction(value)
    elif isinstance(value, str):
        found = _DURATION.fullmatch(value)
        if found is None or (found.group(2) in ("h", "d") and not long):
            raise _bad(path)
        exact = Fraction(Decimal(found.group(1))) * _UNITS[found.group(2)]
    else:
        raise _bad(path)
    # The range is checked on the exact value, so a string a hair outside it is refused; the
    # float is made only once the value is known to be in range.
    if not Fraction(low) <= exact <= Fraction(high):
        raise _bad(path)
    return float(exact)


def _absolute(value: Any, path: Path, high: int = MAX_ARG) -> str:
    """An absolute path: a leading ``/`` and at least one more character."""
    if (
        not isinstance(value, str)
        or not 2 <= len(value) <= high
        or value[0] != "/"
        or "\x00" in value
    ):
        raise _bad(path)
    return value


def _port_ok(port: str | None) -> bool:
    return port is None or 1 <= int(port) <= 65535


def _template(value: Any, path: Path) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_TEMPLATE:
        raise _bad(path)
    if not TEMPLATE.fullmatch(value):
        raise _bad(path)
    return value


def _literal_url(value: Any, path: Path, high: int = MAX_LINK) -> str:
    if not isinstance(value, str) or not 9 <= len(value) <= high:
        raise _bad(path)
    found = _URL.fullmatch(value)
    if found is None or not _port_ok(found.group(2)):
        raise _bad(path)
    return value


# --- destinations and quiet hours ------------------------------------------------------------

_DESTINATION_KEYS = frozenset(
    {"kind", "cmd", "timeout", "floor", "quiet", "max_per_hour", "env", "enabled"}
)
_WINDOW_KEYS = frozenset({"timezone", "days", "start", "end"})


def _destination(name: Any, body: Any, windows: dict[str, QuietWindow]) -> Destination:
    path: Path = ("destinations", _entry(name))
    if not isinstance(name, str) or not _DESTINATION_NAME.fullmatch(name):
        raise _bad(path)
    node = _closed(body, path, _DESTINATION_KEYS)

    kind = node.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise _bad((*path, "kind"))

    cmd_path = (*path, "cmd")
    raw_cmd = _list(node.get("cmd"), cmd_path, 1, MAX_CMD)
    cmd = tuple(_string(item, cmd_path, low=0) for item in raw_cmd)
    _absolute(cmd[0], cmd_path)
    if any(_FORBIDDEN_ARG.fullmatch(item) for item in cmd):
        raise _bad(cmd_path)

    timeout = _duration(node.get("timeout", "10s"), (*path, "timeout"), 1.0, 120.0)
    floor = _level(node.get("floor", "info"), (*path, "floor"))

    quiet: str | None = None
    if "quiet" in node:
        quiet = _pattern(node["quiet"], (*path, "quiet"), _DESTINATION_NAME)
        if quiet not in windows:
            raise _bad((*path, "quiet"))

    max_per_hour = _integer(node.get("max_per_hour", 30), (*path, "max_per_hour"), 0, 3600)

    env_path = (*path, "env")
    env = tuple(
        _pattern(item, env_path, _ENV_NAME)
        for item in _list(node.get("env", []), env_path, 0, 4096)
    )
    if len(set(env)) != len(env) or any(item in _ENV_FIXED for item in env):
        raise _bad(env_path)

    enabled = _boolean(node.get("enabled", True), (*path, "enabled"))
    return Destination(
        name, cast(Kind, kind), cmd, timeout, floor, quiet, max_per_hour, env, enabled
    )


def _destinations(value: Any, windows: dict[str, QuietWindow]) -> dict[str, Destination]:
    node = _mapping(value, ("destinations",))
    if len(node) > MAX_DESTINATIONS:
        raise _bad(("destinations",))
    return {name: _destination(name, body, windows) for name, body in node.items()}


def _clock(value: Any, path: Path) -> int:
    text = _pattern(value, path, _CLOCK)
    return int(text[:2]) * 60 + int(text[3:])


def _window(name: Any, body: Any, gateway: Gateway) -> QuietWindow:
    path: Path = ("quiet_hours", _entry(name))
    if not isinstance(name, str) or not _DESTINATION_NAME.fullmatch(
        name
    ):  # a window name follows the destination grammar
        raise _bad(path)
    node = _closed(body, path, _WINDOW_KEYS)

    zone_path = (*path, "timezone")
    timezone = node.get("timezone")
    if not isinstance(timezone, str):
        raise _bad(zone_path)
    failed = False
    zone: ZoneInfo | None = None
    try:
        zone = gateway.load_zone(timezone)
    except ZoneError:
        failed = True
    if failed or zone is None:
        raise _bad(zone_path)

    days_path = (*path, "days")
    if "days" in node:
        names = [_string(d, days_path, low=3, high=3) for d in _list(node["days"], days_path, 1, 7)]
        if any(d not in DAYS for d in names) or len(set(names)) != len(names):
            raise _bad(days_path)
        days = tuple(sorted(DAYS.index(d) for d in names))
    else:
        days = tuple(range(7))

    start = _clock(node.get("start"), (*path, "start"))
    end = _clock(node.get("end"), (*path, "end"))
    if end == start:
        raise _bad((*path, "end"))
    return QuietWindow(name, timezone, zone, days, start, end)


def _quiet_hours(value: Any, gateway: Gateway) -> dict[str, QuietWindow]:
    node = _mapping(value, ("quiet_hours",))
    return {name: _window(name, body, gateway) for name, body in node.items()}


# --- links -----------------------------------------------------------------------------------


def _links(value: Any) -> Links:
    node = _closed(value, ("links",), frozenset({"hosts", "tickets"}))
    hosts: dict[str, Literal["github", "gitlab"]] = {}
    for name, forge in _mapping(node.get("hosts", {}), ("links", "hosts")).items():
        path: Path = ("links", "hosts", _entry(name))
        found = _HOST_KEY.fullmatch(name) if isinstance(name, str) and len(name) <= 259 else None
        if found is None or not _port_ok(found.group(2)):
            raise _bad(path)
        if not isinstance(forge, str) or forge not in ("github", "gitlab"):
            raise _bad(path)
        hosts[name] = cast('Literal["github", "gitlab"]', forge)
    tickets: dict[str, str] = {}
    for site, base in _mapping(node.get("tickets", {}), ("links", "tickets")).items():
        path = ("links", "tickets", _entry(site))
        if not isinstance(site, str) or not _SITE_NAME.fullmatch(site):
            raise _bad(path)
        url = _literal_url(base, path)
        if not url.endswith("/"):
            raise _bad(path)
        tickets[site] = url
    return Links(hosts, tickets)


# --- rules -----------------------------------------------------------------------------------

_MATCH_KEYS = frozenset({"source", "hook", "repo", "labels", "where", "level_min"})
_RULE_KEYS = frozenset(
    {"name", "match", "drop", "notify", "level", "title", "message", "link", "dedup_window"}
    | {"include_text"}
)
_DROP_KEYS = frozenset({"name", "match", "drop"})


def _wildcard(value: Any, path: Path) -> str:
    text = _string(value, path, high=MAX_MATCH_VALUE)
    if text.count("*") > 1 or ("*" in text and not text.endswith("*")):
        raise _bad(path)
    return text


def _match(value: Any, path: Path) -> Match:
    node = _closed(value, path, _MATCH_KEYS)
    source, hook, repo = (
        _wildcard(node[k], (*path, k)) if k in node else None for k in ("source", "hook", "repo")
    )

    labels: tuple[str, ...] = ()
    if "labels" in node:
        labels_path = (*path, "labels")
        labels = tuple(
            _pattern(item, labels_path, _LABEL)
            for item in _list(node["labels"], labels_path, 1, MAX_LABELS)
        )
        if len(set(labels)) != len(labels):
            raise _bad(labels_path)

    where: tuple[tuple[str, WhereValue], ...] = ()
    if "where" in node:
        where_path = (*path, "where")
        pairs = _mapping(node["where"], where_path)
        if not 1 <= len(pairs) <= MAX_WHERE:
            raise _bad(where_path)
        built: list[tuple[str, WhereValue]] = []
        for key, item in pairs.items():
            if not isinstance(key, str) or not _WHERE_KEY.fullmatch(key):
                raise _bad(where_path)
            if isinstance(item, str):
                if len(item) > MAX_WHERE_VALUE or "\x00" in item:
                    raise _bad(where_path)
            elif not isinstance(item, (bool, int)):  # a float, a list, a map or a null
                raise _bad(where_path)
            built.append((key, cast(WhereValue, item)))
        where = tuple(built)

    level_min = _level(node["level_min"], (*path, "level_min")) if "level_min" in node else None
    if level_min is not None and (hook is not None or repo is not None or where):
        raise _bad((*path, "level_min"))
    return Match(source, hook, repo, labels, where, level_min)


def _rule(
    index: int,
    body: Any,
    destinations: dict[str, Destination],
    default_window: float,
) -> Rule:
    path: Path = ("rules", str(index))
    node = _closed(body, path, _RULE_KEYS)
    name = _pattern(node.get("name"), (*path, "name"), _RULE_NAME)
    if "match" not in node:
        raise _bad((*path, "match"))
    match = _match(node["match"], (*path, "match"))
    drop = _boolean(node.get("drop", False), (*path, "drop"))
    if drop:
        for key in node:
            if key not in _DROP_KEYS:
                raise _bad((*path, key))
        return Rule(name, match, True, (), None, None, None, None, default_window, ())

    notify_path = (*path, "notify")
    if "notify" not in node:
        raise _bad(notify_path)
    notify = tuple(
        _pattern(item, notify_path, _DESTINATION_NAME)
        for item in _list(node["notify"], notify_path, 1, MAX_NOTIFY)
    )
    if len(set(notify)) != len(notify) or any(item not in destinations for item in notify):
        raise _bad(notify_path)

    if "title" in node and "message" not in node:
        raise _bad((*path, "title"))
    if "message" not in node and match.level_min is None:  # a pass-through rule
        raise _bad((*path, "match", "level_min"))

    level: Level | None = None
    if "level" in node:
        level = _level(node["level"], (*path, "level"))
    elif match.level_min is None:  # a rule that can match a hook payload sets the level
        raise _bad((*path, "level"))

    title = _template(node["title"], (*path, "title")) if "title" in node else None
    message = _template(node["message"], (*path, "message")) if "message" in node else None

    link: str | None = None
    if "link" in node:
        link_path = (*path, "link")
        raw_link = node["link"]
        if isinstance(raw_link, str) and raw_link in _LINK_TOKENS:
            if message is None:  # a pass-through rule sends a notice, which has no PR or ticket
                raise _bad(link_path)
            link = raw_link
        else:
            link = _literal_url(raw_link, link_path)

    window = default_window
    if "dedup_window" in node:
        window = _duration(node["dedup_window"], (*path, "dedup_window"), 1.0, _DAY, long=True)

    include_path = (*path, "include_text")
    include = tuple(
        _pattern(item, include_path, _TOKEN_PATH)
        for item in _list(node.get("include_text", []), include_path, 0, MAX_INCLUDE_TEXT)
    )
    if "include_text" in node and not include or len(set(include)) != len(include):
        raise _bad(include_path)
    return Rule(name, match, False, notify, level, title, message, link, window, include)


def _rules(
    value: Any, destinations: dict[str, Destination], default_window: float
) -> tuple[Rule, ...]:
    items = _list(value, ("rules",), 0, MAX_RULES)
    rules = tuple(_rule(i, body, destinations, default_window) for i, body in enumerate(items))
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        if rule.name in seen:
            raise _bad(("rules", str(index), "name"))
        seen.add(rule.name)
    return rules


# --- the rest --------------------------------------------------------------------------------

_TOP_KEYS = frozenset(
    {
        "enabled",
        "destinations",
        "quiet_hours",
        "links",
        "rules",
        "defaults",
        "lock_wait",
        "retention",
        "hooks",
        "cron",
        "log",
    }
)


def _hooks(value: Any) -> tuple[Hook, ...]:
    node = _closed(value, ("hooks",), frozenset({"post-fail"}))
    entries = node.get("post-fail", [])
    if not isinstance(entries, list):
        raise _bad(("hooks", "post-fail"))
    hooks: list[Hook] = []
    for index, body in enumerate(cast("list[Any]", entries)):
        path: Path = ("hooks", "post-fail", str(index))
        entry = _closed(body, path, frozenset({"cmd", "timeout", "async"}))
        cmd = _string(entry.get("cmd"), (*path, "cmd"))
        timeout = _duration(entry.get("timeout", "30s"), (*path, "timeout"), 1.0, _HOOK_TIMEOUT_MAX)
        hooks.append(Hook(cmd, timeout, _boolean(entry.get("async", False), (*path, "async"))))
    return tuple(hooks)


def validate_config(data: dict[str, Any], gateway: Gateway) -> Config:
    """Validate the merged raw config. Raises :class:`ConfigError` (``config_invalid``) naming the
    first problem's key; the input is not modified."""
    top = _closed(data, (), _TOP_KEYS)  # an unknown top-level key is the key "?"
    windows = _quiet_hours(top["quiet_hours"], gateway) if "quiet_hours" in top else {}
    destinations = _destinations(top["destinations"], windows) if "destinations" in top else {}
    links = _links(top["links"]) if "links" in top else Links({}, {})

    defaults = _closed(top.get("defaults", {}), ("defaults",), frozenset({"dedup_window"}))
    dedup = _duration(
        defaults.get("dedup_window", "10m"), ("defaults", "dedup_window"), 1.0, _DAY, long=True
    )
    lock_wait = _duration(top.get("lock_wait", "2s"), ("lock_wait",), 1.0, 60.0)
    retention = _closed(
        top.get("retention", {}), ("retention",), frozenset({"history", "event_ids"})
    )
    history = _duration(
        retention.get("history", "30d"), ("retention", "history"), _DAY, _RETENTION_MAX, long=True
    )
    event_ids = _duration(
        retention.get("event_ids", "14d"),
        ("retention", "event_ids"),
        _DAY,
        _RETENTION_MAX,
        long=True,
    )
    cron = _closed(top.get("cron", {}), ("cron",), frozenset({"env_file"}))
    env_file = _absolute(cron["env_file"], ("cron", "env_file")) if "env_file" in cron else None
    hooks = _hooks(top["hooks"]) if "hooks" in top else ()
    log = _boolean(top.get("log", True), ("log",))
    enabled = _boolean(top.get("enabled", True), ("enabled",))
    rules = _rules(top.get("rules", []), destinations, dedup)
    return Config(
        enabled,
        destinations,
        windows,
        links,
        rules,
        dedup,
        lock_wait,
        history,
        event_ids,
        hooks,
        env_file,
        log,
    )


def load_valid_config(gateway: Gateway, config_path: str | None = None) -> Config | None:
    """Load, merge and validate the config; ``None`` when there is none (unconfigured)."""
    loaded = load_config(gateway, config_path)
    if loaded is None:
        return None
    return validate_config(loaded.data, gateway)
