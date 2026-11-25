"""Rendering (#11): a rule's templates against a notification, the plain and text value rule and
sanitizing.

Pure logic: no clock, no files, no processes. docs/interface.md (Templates, Untrusted text) is
normative. Rendering has no errors: it never raises and never refuses, whatever the payload holds;
what it leaves out is reported in ``missing`` and ``excluded``.

Safety properties:

- Substitution is one pass (``re.sub`` over the template): a replaced value is never scanned
  again, so a value that holds ``{{x}}`` stays literal.
- A payload string reaches a title only when it is plain; other text reaches a message only
  through ``include_text`` with ``reads_third_party`` and ``tainted`` both the boolean ``False``
  (``match.reads_third_party`` and ``match.tainted`` fail closed; they are not recomputed here).
- Every title and message passes :func:`sanitize` last, so template literals and derived values
  are cleaned as well as payload values.
- Every step is linear in the input: a regex without nested quantifiers, ``str.translate`` and
  slicing. A value is resolved once per render however often a template uses it.

Choices where the interface is silent (the narrower reading):

- An absent label is ``missing`` for either shape; a label value that is not plain is
  ``excluded`` (it is never text that ``include_text`` can open).
- For a hook payload an empty derived field (``pr_url``, ``ticket_url``) is ``missing``; for a
  notice ``hook``, ``pr_url`` and ``ticket_url`` are empty without being reported.
- A text value that sanitizes to nothing renders empty and is ``excluded`` (no ``""``).
- A message with nothing but newlines counts as empty and takes the fallback.
- ``text_excluded`` is true exactly when ``excluded`` is not empty.

Link building is ``links.py`` (#12); this module takes the two derived URLs as arguments and
renders no link.
``send.py`` (#22) calls :func:`render` after :func:`~notice_route.match.decide`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .match import reads_third_party, tainted
from .payload import Notification
from .validate import TOKEN_PATH, Rule

__all__ = [
    "MESSAGE_MAX",
    "TEXT_MAX",
    "TITLE_MAX",
    "Rendered",
    "render",
    "sanitize",
]

TITLE_MAX = 256
MESSAGE_MAX = 4000
TEXT_MAX = 200  # characters of an included text value

_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#-]{0,255}")
_PLACEHOLDER = re.compile(r"\{\{(" + TOKEN_PATH + r")\}\}")
_SPACES = re.compile(r" {2,}")
_DERIVED = frozenset({"source", "hook", "level", "pr_url", "ticket_url"})
_NOTICE_EMPTY_OK = frozenset({"hook", "pr_url", "ticket_url"})

# --- sanitize ---------------------------------------------------------------------------------

_SPACE_CATEGORIES = frozenset({"Cc", "Zl", "Zp", "Zs"})
_DELETE_CATEGORIES = frozenset({"Cs", "Co", "Cf"})
_KEEP_CF = 0x200D
_DELETE_EXTRA = frozenset(
    {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0, 0xFFFC}
    | set(range(0x180B, 0x180E))
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
)
_CACHE_BELOW = 0x10000  # the decisions for the BMP are kept; the rest are computed each time


class _Table(dict[int, str | None]):
    """A ``str.translate`` table that decides each code point from its category on demand: a
    space, ``None`` (delete), or ``KeyError`` (keep)."""

    def __init__(self, keep_newline: bool) -> None:
        super().__init__()
        self._keep_newline = keep_newline

    def __missing__(self, cp: int) -> str | None:
        if cp == 0x0A and self._keep_newline:
            raise KeyError(cp)
        if cp == 0x20:
            raise KeyError(cp)
        if cp in _DELETE_EXTRA:
            answer: str | None = None
        else:
            category = unicodedata.category(chr(cp))
            if category in _SPACE_CATEGORIES:
                answer = " "
            elif category in _DELETE_CATEGORIES and cp != _KEEP_CF:
                answer = None
            else:
                answer = ""  # keep
        if answer == "":
            if cp < _CACHE_BELOW:
                self[cp] = chr(cp)
            raise KeyError(cp)
        if cp < _CACHE_BELOW:
            self[cp] = answer
        return answer


_TABLES = {True: _Table(True), False: _Table(False)}


def sanitize(text: str, lines: bool) -> str:
    """Make untrusted text safe to show (docs/interface.md: Templates, Sanitizing).

    ``\\r\\n`` and ``\\r`` become ``\\n``; with ``lines`` false the text is cut at its first
    ``\\n``; Cc (tab, and ``\\n`` when ``lines`` is false), Zl, Zp and Zs become a space; Cs, Co,
    Cf except U+200D and the listed invisible code points are deleted; each run of spaces becomes
    one space and each line is stripped of spaces. Categories are the running Python's."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not lines:
        text = text.partition("\n")[0]
    text = _SPACES.sub(" ", text.translate(_TABLES[lines]))
    if "\n" in text:
        return "\n".join(line.strip(" ") for line in text.split("\n"))
    return text.strip(" ")


# --- values -----------------------------------------------------------------------------------

_MISSING = object()


@dataclass(frozen=True, slots=True)
class Rendered:
    """The rendered title and message (never ``None``; the title is empty without a template).

    ``missing`` lists the token paths that rendered empty because nothing was there; ``excluded``
    the token paths whose text was left out; both are distinct, in first-seen order, title first.
    """

    title: str
    message: str
    missing: tuple[str, ...]
    excluded: tuple[str, ...]
    text_excluded: bool


def _scalar(value: object) -> tuple[str, str]:
    """``("plain", text)``, ``("text", raw)`` or ``("missing", "")``."""
    if isinstance(value, bool):
        return "plain", "true" if value else "false"
    if isinstance(value, int):
        try:
            return "plain", str(value)
        except ValueError:  # beyond the interpreter's digit limit; the reader cannot produce it
            return "missing", ""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return "missing", ""
        return "plain", repr(value)
    if isinstance(value, str):
        if _PLAIN.fullmatch(value):
            return "plain", value
        return "text", value
    return "missing", ""


def _walk(payload: dict[str, Any], path: str) -> object:
    current: object = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]  # pyright: ignore[reportUnknownVariableType]
    return current


class _Run:
    """One render: resolves each token path once and records what was left out."""

    def __init__(
        self, rule: Rule, n: Notification, level: str, pr_url: str, ticket_url: str
    ) -> None:
        self._n = n
        self._include = frozenset(rule.include_text)
        self._text_ok: bool | None = None
        self._derived = {
            "source": n.source,
            "hook": n.hook or "",
            "level": level,
            "pr_url": pr_url,
            "ticket_url": ticket_url,
        }
        self._resolved: dict[str, tuple[str, str]] = {}
        self._quoted: dict[str, str] = {}
        self.missing: dict[str, None] = {}
        self.excluded: dict[str, None] = {}

    def _resolve(self, path: str) -> tuple[str, str]:
        n = self._n
        if path in _DERIVED:
            value = self._derived[path]
            if not value and not (n.shape == "notice" and path in _NOTICE_EMPTY_OK):
                return "missing", ""
            return "derived", value
        if path.startswith("labels."):
            key = path[len("labels.") :]
            values = [v for k, v in n.labels if k == key]
            if not values:
                return "missing", ""
            first = min(values)
            if _PLAIN.fullmatch(first):
                return "plain", first
            return "excluded", ""
        if n.shape != "hook":
            return "missing", ""
        found = _walk(n.payload, path)
        if found is _MISSING:
            return "missing", ""
        return _scalar(found)

    def _text_allowed(self) -> bool:
        if self._text_ok is None:
            self._text_ok = not reads_third_party(self._n) and not tainted(self._n)
        return self._text_ok

    def value(self, path: str, title: bool) -> str:
        if path not in self._resolved:
            self._resolved[path] = self._resolve(path)
        kind, text = self._resolved[path]
        if kind in ("plain", "derived"):
            return text
        if kind == "missing":
            self.missing[path] = None
            return ""
        if kind == "text" and not title and path in self._include and self._text_allowed():
            if path not in self._quoted:
                clean = sanitize(text, False)[:TEXT_MAX].rstrip(" ")
                self._quoted[path] = '"' + clean.replace('"', "'") + '"' if clean else ""
            if self._quoted[path]:
                return self._quoted[path]
        self.excluded[path] = None
        return ""

    def expand(self, template: str, title: bool) -> str:
        return _PLACEHOLDER.sub(lambda m: self.value(m.group(1), title), template)


def render(
    rule: Rule, n: Notification, *, level: str, pr_url: str = "", ticket_url: str = ""
) -> Rendered:
    """Render ``rule``'s title and message for ``n``; ``level`` is the final level.

    A rule without ``message`` is a pass-through rule: the notice's own title and message,
    sanitized and cut like a rendered template, never read as a template. ``pr_url`` and
    ``ticket_url`` are the derived links (built by ``links.py``), empty when there is none."""
    run = _Run(rule, n, level, pr_url, ticket_url)
    if rule.message is None:
        notice = n.shape == "notice"
        title_raw = (n.title or "") if notice else ""
        message_raw = (n.message or "") if notice else ""
    else:
        title_raw = run.expand(rule.title, True) if rule.title is not None else ""
        message_raw = run.expand(rule.message, False)
    title = sanitize(title_raw, False)[:TITLE_MAX].rstrip(" ")
    message = sanitize(message_raw, True)[:MESSAGE_MAX].rstrip(" ")
    if not message.strip("\n \u200d"):
        fallback = f"{n.source} {n.hook}" if n.shape == "hook" and n.hook else n.source
        message = sanitize(fallback, True)[:MESSAGE_MAX].rstrip(" ")
    return Rendered(
        title=title,
        message=message,
        missing=tuple(run.missing),
        excluded=tuple(run.excluded),
        text_excluded=bool(run.excluded),
    )
