"""Rule matching (#10): which rule decides a notification, and its final level.

Pure logic over the typed objects of ``validate.py`` and ``payload.py``: no clock, no files, no
processes. Nothing here evaluates user text: a condition is an exact string, an exact string with
one trailing ``*`` (a prefix test), a ``key=value`` pair looked up in a set, a JSON scalar
compared by type and value, or a level compared by rank. There are no regular expressions, so
matching time is linear in the value length (docs/interface.md: Rules, Levels).

Choices where the interface is silent (the narrower reading):

- A ``where`` path walks JSON objects only (a list, a string or a null stops the walk) and reads
  the payload mapping, never a derived ``Notification`` attribute.
- A notice level outside the four known levels never satisfies ``level_min`` and counts as
  ``info`` for the final level (the reader already refuses such a level).
- ``reads_third_party`` and ``tainted`` are ``False`` only when the payload holds the boolean
  ``False``; absent, null, non-boolean and notices all read as ``True`` (fail closed).

``render.py`` (#11) consumes :func:`reads_third_party` and :func:`tainted`.
Destination decisions (floors, quiet hours, caps; ``decide.py``) start from :class:`Decision`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .payload import Notification
from .validate import LEVELS, Level, Match, Rule, WhereValue

__all__ = [
    "Decision",
    "decide",
    "final_level",
    "matches",
    "reads_third_party",
    "rule_covers",
    "tainted",
    "wildcard_matches",
]

_RANK = {level: rank for rank, level in enumerate(LEVELS)}
_MISSING = object()


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of the rule walk.

    ``outcome`` is ``route`` (``rule`` notifies, at ``level``) or ``dropped`` with ``reason``
    ``rule_drop`` (a drop rule matched) or ``no_rule`` (none matched). ``rule`` and ``index``
    are ``None`` only for ``no_rule``; ``level`` is ``None`` unless the outcome is ``route``.
    """

    rule: Rule | None
    index: int | None
    outcome: Literal["route", "dropped"]
    reason: Literal["rule_drop", "no_rule"] | None
    level: Level | None
    pass_through: bool = False


def wildcard_matches(pattern: str, value: str) -> bool:
    """Exact equality, or a prefix test when ``pattern`` ends in ``*``."""
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return pattern == value


def _where_value(payload: dict[str, Any], path: str) -> object:
    current: object = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]  # pyright: ignore[reportUnknownVariableType]
    return current


def _same(want: WhereValue, have: object) -> bool:
    # ``type(...) is`` keeps a bool apart from an int and an int apart from a float.
    return type(have) is type(want) and have == want


def _notice_level(n: Notification) -> Level | None:
    level = n.level or "info"
    return level if level in _RANK else None  # pyright: ignore[reportReturnType]


def matches(match: Match, n: Notification) -> bool:
    """Whether every present condition holds. A condition outside its shape never matches."""
    hook_shape = n.shape == "hook"
    if match.source is not None and not wildcard_matches(match.source, n.source):
        return False
    if match.labels:
        have = set(n.labels)
        # A label key never holds ``=``, so the first one splits key from value.
        if not all(tuple(label.split("=", 1)) in have for label in match.labels):
            return False
    if match.hook is not None and (
        not hook_shape or n.hook is None or not wildcard_matches(match.hook, n.hook)
    ):
        return False
    if match.repo is not None and (
        not hook_shape or n.repo is None or not wildcard_matches(match.repo, n.repo)
    ):
        return False
    if match.where:
        if not hook_shape:
            return False
        if not all(_same(want, _where_value(n.payload, key)) for key, want in match.where):
            return False
    if match.level_min is not None:
        level = _notice_level(n)
        if n.shape != "notice" or level is None or _RANK[level] < _RANK[match.level_min]:
            return False
    return True


def final_level(rule: Rule, n: Notification) -> Level:
    """The rule's level, else the notice's own level, else ``info``; nothing else raises it."""
    if rule.level is not None:
        return rule.level
    if n.shape == "notice":
        level = _notice_level(n)
        if level is not None:
            return level
    return "info"


def decide(rules: Sequence[Rule], n: Notification) -> Decision:
    """The first matching rule decides; later rules are never consulted."""
    for index, rule in enumerate(rules):
        if not matches(rule.match, n):
            continue
        if rule.drop:
            return Decision(rule, index, "dropped", "rule_drop", None)
        return Decision(
            rule, index, "route", None, final_level(rule, n), pass_through=rule.message is None
        )
    return Decision(None, None, "dropped", "no_rule", None)


def _flag(n: Notification, name: str) -> bool:
    if n.shape != "hook":
        return True
    value = n.payload.get(name)
    return value is not False


def reads_third_party(n: Notification) -> bool:
    return _flag(n, "reads_third_party")


def tainted(n: Notification) -> bool:
    return _flag(n, "tainted")


def rule_covers(earlier: Match, later: Match) -> bool:
    """Every condition of ``earlier`` is also in ``later``, so ``later`` never sees a
    notification ``earlier`` did not take (used by ``validate`` for ``rule_unreachable``)."""
    for key in ("source", "hook", "repo"):
        a, b = getattr(earlier, key), getattr(later, key)
        if a is not None and (b is None or not wildcard_matches(a, b)):
            return False
    if not set(earlier.labels) <= set(later.labels):
        return False
    later_where = {(k, type(v), v) for k, v in later.where}
    if any((k, type(v), v) not in later_where for k, v in earlier.where):
        return False
    return earlier.level_min is None or earlier.level_min == later.level_min
