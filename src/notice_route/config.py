"""Config loading: password-database home, ownership checks, strict YAML and drop-ins (#6).

``load_config(gateway, config_path)`` finds the config only from the home in the password
database (never ``HOME`` or ``XDG_*``), reads ``config.yaml`` and ``config.d/*.yaml`` beside it
through the gateway, parses each file with the strict loader below and returns the merged raw
mapping. Validation of destinations, quiet windows, links, rules and defaults is #7's: this
module checks only what loading needs (file safety, YAML, the shape of the merged sections and
that no name or key is defined twice).

Rules kept here (docs/interface.md, Config and Files):

- The config directory, ``config.d`` and every file must be owned by the user and not group- or
  world-writable; files are opened with ``O_NOFOLLOW``, so a symlink (also a symlinked
  directory) is refused (``config_unsafe``). The 256 KiB limit applies to the bytes read and
  1 MiB to all files together (the file whose read crosses it is ``config_invalid``); at most 256
  drop-ins and 4096 directory entries of any name. A missing config directory or ``config.yaml``
  is "unconfigured" (``None``);
  nothing is ever created.
- The YAML loader is ``SafeLoader``'s machinery with its own resolvers: the only booleans are
  ``true`` and ``false``, the only numbers are decimal integers and decimal floats, so ``yes``,
  ``on``, ``010``, ``1e3``, ``.inf`` and dates stay strings. Anchors, aliases, explicit tags,
  merge keys, duplicate keys, non-string keys, lone surrogates, NUL, documents deeper than
  ``MAX_DEPTH`` and more than one document are refused. An alias is refused at the first one
  met, so no alias can expand.
- A failure never keeps the YAML error text (it quotes the file) and the error key never holds a
  value or an unknown key's name (``?``): ``details.key`` is a known key path, ``home``,
  ``--config``, ``.`` (the config directory) or a file path relative to it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

import yaml

from .gateway import DirHandle, Gateway, HomeError, LimitExceededError, UnsafePathError

__all__ = [
    "MAX_DEPTH",
    "MAX_DROPINS",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "ConfigError",
    "LoadedConfig",
    "load_config",
]

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024  # config.yaml and every drop-in together (the 1 MiB reader limit)
MAX_DROPINS = 256
MAX_DEPTH = 16
_NUMBER_CHARS = 20  # longest integer or float text (sign excluded), whatever the interpreter says
_LIST_LIMIT = 4096  # entries of config.d (of any name) read before refusing the directory
_CONFIG_SUBPATH = "/.config/notice-route"
_KEY_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_FILE_KEY_BAD = re.compile(r"[^A-Za-z0-9._-]")

ErrorCode = Literal["config_invalid", "config_unsafe"]


class ConfigError(Exception):
    """The config cannot be used (exit 2). ``code`` is ``config_invalid`` or ``config_unsafe``;
    ``key`` is ``details.key``. The message is fixed: no file text reaches it."""

    exit_code = 2

    def __init__(self, code: ErrorCode, key: str) -> None:
        super().__init__(
            "unsafe configuration" if code == "config_unsafe" else "invalid configuration"
        )
        self.code: ErrorCode = code
        self.key = key


@dataclass(frozen=True)
class LoadedConfig:
    """The merged raw config (``data``, not yet validated) and the files read, as the keys
    name them: ``config.yaml`` (``--config`` when that replaced it) then ``config.d/<name>``."""

    data: dict[str, Any]
    files: tuple[str, ...]


# --- the strict loader -----------------------------------------------------------------------

# PyYAML's resolver uses ``match`` (anchored at the start only), so each pattern ends in ``\Z``.
_INT = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_FLOAT = re.compile(r"-?(?:0|[1-9][0-9]*)\.[0-9]+\Z")
_BOOL = re.compile(r"(?:true|false)\Z")
_NULL = re.compile(r"(?:~|null|Null|NULL|)\Z")
_BAD_TEXT = re.compile("[\ud800-\udfff\x00]")


def _scalar(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    text = loader.construct_scalar(node)  # pyright: ignore[reportArgumentType]
    if not isinstance(text, str):
        raise ValueError("not a scalar")
    return text


def _construct_str(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    text = _scalar(loader, node)
    if _BAD_TEXT.search(text):
        raise ValueError("a lone surrogate or NUL in a string")
    return text


def _construct_bool(loader: yaml.SafeLoader, node: yaml.Node) -> bool:
    text = _scalar(loader, node)
    if not _BOOL.fullmatch(text):
        raise ValueError("not a boolean")
    return text == "true"


def _construct_int(loader: yaml.SafeLoader, node: yaml.Node) -> int:
    text = _scalar(loader, node)
    if not _INT.fullmatch(text):
        raise ValueError("not an integer")
    if len(text.lstrip("-")) > _NUMBER_CHARS:
        raise ValueError("an integer with too many digits")
    return int(text)


def _construct_float(loader: yaml.SafeLoader, node: yaml.Node) -> float:
    text = _scalar(loader, node)
    if not _FLOAT.fullmatch(text):
        raise ValueError("not a decimal float")
    if len(text.lstrip("-")) > _NUMBER_CHARS:
        raise ValueError("a float with too many characters")
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("not finite")
    return value


def _construct_map(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    data: dict[str, Any] = {}
    yield data
    for key_node, value_node in node.value:  # pyright: ignore[reportAttributeAccessIssue]
        key = loader.construct_object(key_node, deep=True)
        if type(key) is not str:
            raise ValueError("a key that is not a string")
        if key == "<<":
            raise ValueError("a merge key")
        if key in data:
            raise ValueError("duplicate key")
        data[key] = loader.construct_object(value_node, deep=True)


class _StrictLoader(yaml.SafeLoader):
    yaml_implicit_resolvers: dict[Any, Any] = {}
    yaml_constructors = {  # pyright: ignore[reportIncompatibleVariableOverride]
        "tag:yaml.org,2002:null": yaml.SafeLoader.construct_yaml_null,
        "tag:yaml.org,2002:bool": _construct_bool,
        "tag:yaml.org,2002:int": _construct_int,
        "tag:yaml.org,2002:float": _construct_float,
        "tag:yaml.org,2002:str": _construct_str,
        "tag:yaml.org,2002:seq": yaml.SafeLoader.construct_yaml_seq,
        "tag:yaml.org,2002:map": _construct_map,
        None: yaml.SafeLoader.construct_undefined,
    }
    yaml_multi_constructors = {}  # pyright: ignore[reportIncompatibleVariableOverride]

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise ValueError("aliases are not allowed")
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise ValueError("anchors are not allowed")
        if getattr(event, "tag", None) is not None:
            raise ValueError("explicit tags are not allowed")
        container = isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent))
        if container:  # only mappings and sequences count toward the depth
            self._depth += 1
        try:
            if self._depth > MAX_DEPTH:
                raise ValueError("document too deep")
            return super().compose_node(parent, index)
        finally:
            if container:
                self._depth -= 1


_StrictLoader.add_implicit_resolver("tag:yaml.org,2002:bool", _BOOL, list("tf"))
_StrictLoader.add_implicit_resolver("tag:yaml.org,2002:int", _INT, list("-0123456789"))
_StrictLoader.add_implicit_resolver("tag:yaml.org,2002:float", _FLOAT, list("-0123456789"))
_StrictLoader.add_implicit_resolver("tag:yaml.org,2002:null", _NULL, ["~", "n", "N", ""])

_FAILED = object()


def _parse(raw: bytes) -> object:
    """The YAML document, or ``_FAILED``. Nothing about the failure is kept."""
    try:
        loader = _StrictLoader(raw.decode("utf-8"))
        try:
            node = loader.get_single_node()
            if node is None:  # an empty stream or only comments: an empty config
                return {}
            return loader.construct_document(node)
        finally:
            loader.dispose()
    except Exception:  # YAMLError, ValueError, RecursionError, MemoryError: all one outcome
        return _FAILED


# --- merge -----------------------------------------------------------------------------------

# Lists that append in file order, maps whose entries merge by name (a name twice is refused) and
# the containers on the way to them. Every other key may be defined in one file only.
_APPENDED = frozenset({("rules",), ("hooks", "post-fail")})
_NAMED = frozenset({("destinations",), ("quiet_hours",), ("links", "hosts"), ("links", "tickets")})
_CONTAINERS = frozenset({("links",), ("hooks",)})
_KNOWN = frozenset(
    {
        ("enabled",),
        ("lock_wait",),
        ("log",),
        ("defaults",),
        ("defaults", "dedup_window"),
        ("retention",),
        ("retention", "history"),
        ("retention", "event_ids"),
        ("cron",),
        ("cron", "env_file"),
    }
    | _APPENDED
    | _NAMED
    | _CONTAINERS
)


def _key(path: tuple[str, ...]) -> str:
    """The ``details.key`` of a path: a known key, or a plain entry name under a named section,
    else ``?`` (an unknown key may be a secret pasted as a key; nothing after it is shown)."""
    shown: list[str] = []
    for index, segment in enumerate(path):
        if path[: index + 1] in _KNOWN:
            shown.append(segment)
        elif path[:index] in _NAMED and _KEY_SEGMENT.fullmatch(segment):
            shown.append(segment)
        else:
            shown.append("?")
            break
    return ".".join(shown)


def _file_key(rel: str) -> str:
    return "/".join(_FILE_KEY_BAD.sub("?", part) for part in rel.split("/"))


def _invalid(path: tuple[str, ...]) -> ConfigError:
    return ConfigError("config_invalid", _key(path))


def _merge(dst: dict[str, Any], src: dict[str, Any], path: tuple[str, ...]) -> None:
    for name, value in src.items():
        here = (*path, name)
        if here in _APPENDED:
            if not isinstance(value, list):
                raise _invalid(here)
            dst.setdefault(name, []).extend(value)
        elif here in _NAMED:
            if not isinstance(value, dict):
                raise _invalid(here)
            entries: dict[str, Any] = dst.setdefault(name, {})
            for entry, body in value.items():
                if entry in entries:
                    raise _invalid((*here, entry))
                entries[entry] = body
        elif isinstance(value, dict):
            nested = dst.setdefault(name, {})
            if not isinstance(nested, dict):
                raise _invalid(here)
            _merge(nested, value, here)
        elif here in _CONTAINERS or name in dst:
            raise _invalid(here)
        else:
            dst[name] = value


def _check_rule_names(rules: Any) -> None:
    """A rule name defined twice (also within one file) is refused. Rule shape is #7's."""
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        name = rule.get("name") if isinstance(rule, dict) else None
        if not isinstance(name, str):
            continue  # TODO(#7): a rule without a string name is refused by the validator
        if name in seen:
            raise ConfigError("config_invalid", f"rules.{index}.name")
        seen.add(name)


# --- reading ---------------------------------------------------------------------------------


def _config_location(gateway: Gateway, config_path: str | None) -> tuple[str, str]:
    """The config directory and the main file name."""
    if config_path is None:
        failed = False
        home = ""
        try:
            home = str(gateway.home_dir())
        except HomeError:
            failed = True
        if failed:
            raise ConfigError("config_invalid", "home")
        return home.rstrip("/") + _CONFIG_SUBPATH, "config.yaml"
    # The raw last segment decides: PurePosixPath would fold ``x/.`` into ``x``.
    if "\x00" in config_path or config_path.rsplit("/", 1)[-1] in ("", ".", ".."):
        raise ConfigError("config_invalid", "--config")
    path = PurePosixPath(config_path)
    return str(path.parent), path.name


def load_config(gateway: Gateway, config_path: str | None = None) -> LoadedConfig | None:
    """Load and merge the config; ``None`` when there is none (unconfigured: no config
    directory, or no ``config.yaml``; drop-ins alone configure nothing).

    ``config_path`` is the ``--config`` value: it replaces ``config.yaml`` (its directory and
    its ``config.d`` apply, the home is not consulted) and must exist. Raises
    :class:`ConfigError`.
    """
    explicit = config_path is not None
    directory, name = _config_location(gateway, config_path)
    main_key = "--config" if explicit else "config.yaml"

    failure: ConfigError | None = None
    data: dict[str, Any] = {}
    files: list[str] = []
    try:
        dirh = gateway.open_config_dir(directory)
    except UnsafePathError:
        dirh, failure = None, ConfigError("config_unsafe", ".")
    except (OSError, ValueError):
        dirh, failure = None, ConfigError("config_invalid", "--config" if explicit else ".")
    if failure is not None:
        raise failure
    if dirh is None:
        if explicit:
            raise ConfigError("config_invalid", "--config")
        return None
    budget = MAX_TOTAL_BYTES
    with dirh:
        raw = _read(gateway, dirh, name, main_key, budget)
        if raw is None:
            if explicit:
                raise ConfigError("config_invalid", "--config")
            return None
        budget -= len(raw)
        _merge(data, _document(raw, main_key), ())
        files.append(main_key)
        sub = _open_dropin_dir(gateway, dirh)
        if sub is not None:
            with sub:
                for dropin in _dropins(gateway, sub):
                    key = _file_key(f"config.d/{dropin}")
                    raw = _read(gateway, sub, dropin, key, budget)
                    if raw is None:  # removed since the listing
                        raise ConfigError("config_invalid", key)
                    budget -= len(raw)
                    _merge(data, _document(raw, key), ())
                    files.append(key)
    _check_rule_names(data.get("rules", []))
    return LoadedConfig(data, tuple(files))


def _read(gateway: Gateway, parent: DirHandle, name: str, key: str, budget: int) -> bytes | None:
    """One file's bytes, checked on its descriptor; ``None`` when it does not exist. The read
    stops at the file limit or the bytes left of the total budget, whichever is smaller; the
    file whose read crosses either is ``config_invalid``."""
    failure: ConfigError | None = None
    raw: bytes | None = None
    try:
        raw = gateway.read_config_file(parent, name, min(MAX_FILE_BYTES, budget))
    except UnsafePathError:
        failure = ConfigError("config_unsafe", key)
    except (LimitExceededError, OSError, ValueError):
        failure = ConfigError("config_invalid", key)
    if failure is not None:
        raise failure
    return raw


def _document(raw: bytes, key: str) -> dict[str, Any]:
    doc = _parse(raw)
    if doc is _FAILED or not isinstance(doc, dict):
        raise ConfigError("config_invalid", key)
    return doc


def _open_dropin_dir(gateway: Gateway, parent: DirHandle) -> DirHandle | None:
    failure: ConfigError | None = None
    sub: DirHandle | None = None
    try:
        sub = gateway.open_config_dir("config.d", parent=parent)
    except UnsafePathError:
        failure = ConfigError("config_unsafe", "config.d")
    except (OSError, ValueError):
        failure = ConfigError("config_invalid", "config.d")
    if failure is not None:
        raise failure
    return sub


def _dropins(gateway: Gateway, sub: DirHandle) -> list[str]:
    """The drop-in names in lexical order (by code point): visible ``*.yaml`` entries."""
    failure: ConfigError | None = None
    names: list[str] = []
    try:
        names = gateway.list_dir(sub, limit=_LIST_LIMIT)
    except (LimitExceededError, OSError):
        failure = ConfigError("config_invalid", "config.d")
    if failure is not None:
        raise failure
    found = sorted(n for n in names if n.endswith(".yaml") and not n.startswith("."))
    if len(found) > MAX_DROPINS:
        raise ConfigError("config_invalid", "config.d")
    return found
