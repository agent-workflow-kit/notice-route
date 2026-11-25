"""Input: the strict JSON reader, the input flags, the two shapes and the labels (#8).

docs/interface.md (Input shapes, ``send`` steps 1 to 3) is normative. This module is pure: the
bytes come from the :class:`~notice_route.gateway.Gateway` (stdin with a deadline, ``--file``),
and every refusal is a fixed word, never an input value (C-40: no message, pointer or exception
chain carries text from the input, a flag or a path).

The reader (:func:`parse_json_object`) is the one strict JSON reader of the tool. The notifier
output parser reads with the same function, so it is written and tested once.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .gateway import Gateway, InputFileError, InputRead

__all__ = [
    "DEPTH_MAX",
    "INPUT_DEADLINE_S",
    "INPUT_MAX",
    "InputFlags",
    "Notification",
    "PayloadError",
    "StrictJsonError",
    "UsageError",
    "check_flags",
    "classify",
    "load_notification",
    "notice_from_flags",
    "parse_json_object",
]

INPUT_MAX = 1048576  # bytes read from one input (1 MiB)
INPUT_DEADLINE_S = 5.0  # stdin must be complete this long after the start of the command
DEPTH_MAX = 16  # nesting of arrays and objects; the top-level object is depth 1
INT_MIN = -(2**63)
INT_MAX = 2**63 - 1
_INT64_DIGITS = 19  # digits of the largest magnitude, so a longer literal is refused unparsed


class StrictJsonError(ValueError):
    """The bytes are not one strict JSON object. The message is fixed and carries no input; the
    exception has no ``__cause__`` and no ``__context__``."""

    def __init__(self) -> None:
        super().__init__("input is not one strict JSON object")


# --- the reader ----------------------------------------------------------------------------

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
# The next quote or bracket, and the rest of a string after its opening quote.
_NEXT = re.compile(r'["\[\]{}]')
_STRING_REST = re.compile(r'[^"\\]*(?:\\.[^"\\]*)*"', re.DOTALL)


def _refuse_constant(_name: str) -> object:
    raise ValueError


def _parse_int(text: str) -> int:
    if len(text.lstrip("-")) > _INT64_DIGITS:  # before int(): no quadratic parse of a huge literal
        raise ValueError
    value = int(text)
    if not INT_MIN <= value <= INT_MAX:
        raise ValueError
    return value


def _parse_float(text: str) -> float:
    value = float(text)
    if value != value or value in (float("inf"), float("-inf")):  # 1e999 overflows to inf
        raise ValueError
    return value


def _clean(text: str) -> str:
    return _LONE_SURROGATE.sub("\ufffd", text)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # Keys are compared after unescaping and after the replacement of lone surrogates, so two
    # keys that would merge into one are a duplicate.
    out = {_clean(key): value for key, value in pairs}
    if len(out) != len(pairs):
        raise ValueError
    return out


def _walk(value: object) -> object:
    """Replace lone surrogates in every string value (keys were replaced by ``_object``). The
    depth is at most ``DEPTH_MAX`` here, so the recursion is shallow."""
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, list):
        return [_walk(item) for item in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return {key: _walk(item) for key, item in value.items()}  # pyright: ignore[reportUnknownVariableType]
    return value


def _too_deep(text: str) -> bool:
    """A linear scan of the nesting outside strings, before the decode: the C scanner can
    overflow a small C stack long before ``RecursionError``. A string that is not closed ends the
    scan (the decode refuses it), so no input makes the scan restart and go quadratic."""
    depth = 0
    pos = 0
    while True:
        found = _NEXT.search(text, pos)
        if found is None:
            return False
        char = found.group()
        pos = found.end()
        if char == '"':
            rest = _STRING_REST.match(text, pos)
            if rest is None:
                return False
            pos = rest.end()
        elif char in "[{":
            depth += 1
            if depth > DEPTH_MAX:
                return True
        else:
            depth -= 1


_DECODER = json.JSONDecoder(
    object_pairs_hook=_object,
    parse_constant=_refuse_constant,
    parse_int=_parse_int,
    parse_float=_parse_float,
    strict=True,
)


def _decode(data: bytes, max_bytes: int) -> dict[str, Any] | None:
    if len(data) > max_bytes or data.startswith(b"\xef\xbb\xbf"):
        return None
    try:
        text = data.decode("utf-8")
        if _too_deep(text):
            return None
        value = _walk(_DECODER.decode(text))
    except (ValueError, RecursionError, OverflowError):
        return None
    if not isinstance(value, dict):
        return None
    return value  # pyright: ignore[reportUnknownVariableType]


def parse_json_object(data: bytes, *, max_bytes: int = INPUT_MAX) -> dict[str, Any]:
    """Parse ``data`` as exactly one strict JSON object.

    Refused with :class:`StrictJsonError`: more than ``max_bytes`` bytes, bytes that are not valid
    UTF-8, a byte-order mark, invalid JSON (trailing data included), a duplicate key at any depth
    (compared after unescaping), ``NaN``, ``Infinity``, an integer outside the signed 64-bit
    range, a float that does not fit a double, a nesting depth above 16 and a top level that is
    not an object. A lone surrogate in a string or a key becomes U+FFFD.
    """
    value = _decode(data, max_bytes)
    if value is None:  # raised outside the handlers: no context carries the input
        raise StrictJsonError
    return value


# --- refusals ------------------------------------------------------------------------------

REFUSAL_MESSAGES = {
    "payload_unrecognized": "the input is neither a hook payload nor a channel notice",
    "source_mismatch": "the source does not match the payload or is reserved",
    "payload_invalid": "the input is not valid",
    "hook_recursion": "send is refused inside a router hook",
}
USAGE_MESSAGES = {
    "flags": "the input flags cannot be combined this way",
    "labels": "too many --label flags",
    "terminal": "standard input is a terminal",
    "input_file": "the --file path cannot be used",
}
# The pointers a refusal may carry: fixed strings, never built from the input.
POINTERS = frozenset(
    {"", "/tool", "/hook", "/title", "/message", "/level", "/link", "--source", "--label"}
)


class PayloadError(Exception):
    """Exit 20, code ``refused``. ``reason`` is ``payload_unrecognized``, ``source_mismatch``,
    ``payload_invalid`` or ``hook_recursion``. ``pointer`` is set exactly for ``payload_invalid``
    (``""`` for the whole input, ``/<field>``, ``--source`` or ``--label``). The message and the
    pointer are fixed words; nothing from the input reaches either."""

    exit_code = 20

    def __init__(self, reason: str, pointer: str | None = None) -> None:
        if reason not in REFUSAL_MESSAGES:
            raise ValueError("unknown refusal reason")
        if (reason == "payload_invalid") != (pointer is not None) or (
            pointer is not None and pointer not in POINTERS
        ):
            raise ValueError("pointer does not fit the reason")
        super().__init__(REFUSAL_MESSAGES[reason])
        self.reason = reason
        self.pointer = pointer
        self.message = REFUSAL_MESSAGES[reason]

    def to_error(self) -> dict[str, Any]:
        details = None if self.pointer is None else {"pointer": self.pointer}
        return {
            "error": {
                "code": "refused",
                "message": self.message,
                "reason": self.reason,
                "details": details,
            }
        }


class UsageError(Exception):
    """Exit 2, code ``usage``: a flag combination or a ``--file`` path that cannot be used."""

    exit_code = 2

    def __init__(self, reason: Literal["flags", "labels", "terminal", "input_file"]) -> None:
        super().__init__(USAGE_MESSAGES[reason])
        self.reason = reason
        self.message = USAGE_MESSAGES[reason]

    def to_error(self) -> dict[str, Any]:
        return {
            "error": {"code": "usage", "message": self.message, "reason": None, "details": None}
        }


# --- grammar -------------------------------------------------------------------------------

RESERVED_SOURCE = "notice-route"
DEFAULT_SOURCE = "direct"
LEVELS = ("info", "warn", "action", "error")
TEXT_MAX = 100000
LINK_MAX = 1024
LABELS_FLAG_MAX = 32
LABELS_EXAMINED = 64
NOTICE_KEYS = frozenset({"title", "message", "level", "link"})

_SOURCE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_HOOK = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_LABEL = re.compile(r"([a-z0-9][a-z0-9_.-]{0,63})=([A-Za-z0-9._:/#@+=,-]{0,256})")
_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#-]{0,255}")
_LINK = re.compile(r"https?://[\x21-\x7e]*")


@dataclass(frozen=True)
class Notification:
    """One notification that passed the input checks.

    ``shape`` is ``hook`` or ``notice``. ``source`` is the payload's ``tool``, ``--source`` or
    ``direct``. A notice carries ``title``, ``message``, ``level`` and ``link`` (``None`` when
    absent); a hook payload carries ``hook``, ``event_id`` and ``object_id`` (an ``int``, never a
    bool, or a plain ``str``, else ``None``), ``repo`` (a string or ``None``) and its whole
    ``payload``, which the rules and templates read (``match.reads_third_party`` and
    ``match.tainted`` derive the two provenance flags from it; TODO(#11): templates use them).
    ``labels`` are the distinct ``(key, value)`` pairs in first-seen order, the payload's before
    the flags'; ``labels_ignored`` counts what was dropped.
    """

    shape: Literal["hook", "notice"]
    source: str
    hook: str | None = None
    title: str | None = None
    message: str | None = None
    level: str | None = None
    link: str | None = None
    labels: tuple[tuple[str, str], ...] = ()
    labels_ignored: int = 0
    event_id: int | str | None = None
    object_id: int | str | None = None
    repo: str | None = None
    payload: dict[str, Any] = field(default_factory=dict[str, Any])


def _invalid(pointer: str) -> PayloadError:
    return PayloadError("payload_invalid", pointer)


def _text(obj: Mapping[str, Any], name: str, *, minimum: int) -> str | None:
    if name not in obj:
        return None
    value = obj[name]
    if type(value) is not str or not minimum <= len(value) <= TEXT_MAX:
        raise _invalid("/" + name)
    return value


def _notice_fields(obj: Mapping[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    title = _text(obj, "title", minimum=0)
    message = _text(obj, "message", minimum=1)
    level = obj.get("level")
    if "level" in obj and (type(level) is not str or level not in LEVELS):
        raise _invalid("/level")
    link = obj.get("link")
    if "link" in obj and (
        type(link) is not str or len(link) > LINK_MAX or _LINK.fullmatch(link) is None
    ):
        raise _invalid("/link")
    return title, message, level, link


def _plain_id(value: object) -> int | str | None:
    if type(value) is int:  # a bool is not an integer here
        return value
    if type(value) is str and _PLAIN.fullmatch(value):
        return value
    return None


def _payload_labels(obj: Mapping[str, Any]) -> tuple[list[tuple[str, str]], int]:
    if "labels" not in obj:
        return [], 0
    value = obj["labels"]
    if type(value) is not list:
        return [], 1
    items: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
    found: list[tuple[str, str]] = []
    ignored = max(0, len(items) - LABELS_EXAMINED)
    for item in items[:LABELS_EXAMINED]:
        match = _LABEL.fullmatch(item) if type(item) is str else None
        if match is None:
            ignored += 1
        elif (match[1], match[2]) not in found:
            found.append((match[1], match[2]))
    return found, ignored


def _flag_labels(flags: Sequence[str]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for item in flags:
        match = _LABEL.fullmatch(_clean(item))
        if match is None:
            raise _invalid("--label")
        if (match[1], match[2]) not in found:
            found.append((match[1], match[2]))
    return found


def _merge(
    first: list[tuple[str, str]], second: list[tuple[str, str]]
) -> tuple[tuple[str, str], ...]:
    return tuple(first + [pair for pair in second if pair not in first])


def classify(obj: Mapping[str, Any], *, source: str | None, labels: Sequence[str]) -> Notification:
    """Tell the shape of a parsed object and validate it, in the order of ``send`` step 3: the
    shape (else ``payload_unrecognized``), ``/tool``, ``/hook``, ``--source``, ``source_mismatch``,
    the notice's fields, then ``--label``. The first fault is the refusal."""
    tool = obj.get("tool")
    hook = obj.get("hook")
    is_hook = type(tool) is str and type(hook) is str
    if not is_hook and not ("message" in obj and set(obj) <= NOTICE_KEYS):
        raise PayloadError("payload_unrecognized")
    if is_hook:
        assert isinstance(tool, str) and isinstance(hook, str)
        if _SOURCE.fullmatch(tool) is None:
            raise _invalid("/tool")
        if _HOOK.fullmatch(hook) is None:
            raise _invalid("/hook")
    if source is not None and _SOURCE.fullmatch(_clean(source)) is None:
        raise _invalid("--source")
    if is_hook:
        assert isinstance(tool, str)
        final = tool
        if source is not None and source != tool:
            raise PayloadError("source_mismatch")
    else:
        final = DEFAULT_SOURCE if source is None else source
    if final == RESERVED_SOURCE:
        raise PayloadError("source_mismatch")
    if is_hook:
        found, ignored = _payload_labels(obj)
        repo = obj.get("repo")
        note = Notification(
            shape="hook",
            source=final,
            hook=hook if isinstance(hook, str) else None,
            event_id=_plain_id(obj.get("event_id")),
            object_id=_plain_id(obj.get("object_id")),
            repo=repo if type(repo) is str else None,
            payload=dict(obj),
        )
    else:
        title, message, level, link = _notice_fields(obj)
        found, ignored = [], 0
        note = Notification(
            shape="notice",
            source=final,
            title=title,
            message=message,
            level=level,
            link=link,
        )
    merged = _merge(found, _flag_labels(labels))
    return _with_labels(note, merged, ignored)


def _with_labels(
    note: Notification, labels: tuple[tuple[str, str], ...], ignored: int
) -> Notification:
    return Notification(
        shape=note.shape,
        source=note.source,
        hook=note.hook,
        title=note.title,
        message=note.message,
        level=note.level,
        link=note.link,
        labels=labels,
        labels_ignored=ignored,
        event_id=note.event_id,
        object_id=note.object_id,
        repo=note.repo,
        payload=note.payload,
    )


def notice_from_flags(
    *, title: str | None, message: str | None, source: str | None, labels: Sequence[str]
) -> Notification:
    """The channel notice of ``--title`` and ``--message``, checked like the same JSON fields.
    A lone surrogate (an undecodable byte of the command line) becomes U+FFFD, as in JSON."""
    obj: dict[str, Any] = {}
    if title is not None:
        obj["title"] = _clean(title)
    if message is not None:
        obj["message"] = _clean(message)
    return classify(obj, source=source, labels=labels)


# --- flags and reading ----------------------------------------------------------------------


@dataclass(frozen=True)
class InputFlags:
    """The input flags of ``send`` and ``test`` as parsed (``file`` is a path or ``-``)."""

    stdin_json: bool = False
    file: str | None = None
    title: str | None = None
    message: str | None = None
    source: str | None = None
    labels: tuple[str, ...] = ()


def check_flags(flags: InputFlags, gateway: Gateway) -> Literal["stdin", "file", "flags"]:
    """``send`` step 1: refuse the exit 2 combinations and name where the input comes from.

    Refused: ``--stdin-json`` with ``--file``; a JSON source with ``--title`` or ``--message``;
    ``--title`` without ``--message``; no source and no ``--message``; more than 32 ``--label``;
    and stdin from a terminal when stdin is the source. TODO(#13): ``test`` defaults to stdin.
    """
    json_source = flags.stdin_json or flags.file is not None
    if flags.stdin_json and flags.file is not None:
        raise UsageError("flags")
    if json_source and (flags.title is not None or flags.message is not None):
        raise UsageError("flags")
    if flags.title is not None and flags.message is None:
        raise UsageError("flags")
    if not json_source and flags.message is None:
        raise UsageError("flags")
    if len(flags.labels) > LABELS_FLAG_MAX:
        raise UsageError("labels")
    if flags.stdin_json or flags.file == "-":
        if gateway.stdin_is_tty():
            raise UsageError("terminal")
        return "stdin"
    return "file" if flags.file is not None else "flags"


def load_notification(gateway: Gateway, flags: InputFlags, started: float) -> Notification:
    """``send`` steps 1 to 3: check the flags (exit 2), refuse ``hook_recursion`` before reading
    anything, read the input and validate it (exit 20).

    ``started`` is the gateway's monotonic time at the start of the command; stdin must be
    complete ``INPUT_DEADLINE_S`` after it. A regular ``--file`` has no deadline. A ``--file``
    that cannot be opened or is not a regular file is :class:`UsageError`; every other input
    failure (a timeout, a closed stdin, too many bytes, the strict reader's refusals) is
    ``payload_invalid`` with the pointer ``""``.
    """
    plan = check_flags(flags, gateway)
    if gateway.env_present("NOTICE_ROUTE_HOOK"):
        raise PayloadError("hook_recursion")
    if plan == "flags":
        return notice_from_flags(
            title=flags.title, message=flags.message, source=flags.source, labels=flags.labels
        )
    if plan == "stdin":
        result = gateway.read_stdin(INPUT_MAX, started + INPUT_DEADLINE_S)
    else:
        assert flags.file is not None
        result = _read_file(gateway, flags.file)
    obj = _decode(result.data, INPUT_MAX) if result.status == "ok" else None
    if obj is None:  # raised outside the handlers: no context carries the input
        raise _invalid("")
    return classify(obj, source=flags.source, labels=flags.labels)


def _read_file(gateway: Gateway, path: str) -> InputRead:
    failed = False
    try:
        return gateway.read_input_file(path, INPUT_MAX)
    except InputFileError:
        failed = True
    assert failed
    raise UsageError("input_file")
