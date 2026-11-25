"""Command-line entry point for notice-route: parsing, commands, output and exit codes.

``main(argv, gateway)`` is the call shape; tests pass a gateway with a private home.

Output rules (docs/interface.md, Output and Exit codes):

- ``--json`` prints one document on stdout. A config, state or internal error prints the error
  envelope on stdout under ``--json`` and always one fixed line on stderr; the message never
  holds config text, and ``details.key`` names a key, never a value.
- Without ``--json`` the commands print readable lines on stdout; ``--quiet`` drops the
  advisory stderr line of an unconfigured run.
- ``--config`` is accepted only by the commands that run nothing and write nothing (``rules`` and
  ``validate`` now, ``test`` later); a missing file is ``config_invalid``, key ``--config``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__
from .config import ConfigError
from .gateway import Gateway, HomeError, StateError
from .report import rules_document, validate_document
from .validate import load_valid_config

PROG = "notice-route"
DESCRIPTION = "Routes notifications to channel tools by level, source, labels and time."

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_FAILED = 12
EXIT_NOOP = 22


def _add_global_flags(parser: argparse.ArgumentParser, *, top: bool) -> None:
    """Global flags on the top parser (real defaults) and on each subparser (SUPPRESS, so a
    subparser never overwrites a value given before the command)."""
    default: Any = False if top else argparse.SUPPRESS
    parser.add_argument(
        "--json", action="store_true", default=default, help="machine-readable JSON"
    )
    parser.add_argument("--verbose", action="store_true", default=default, help="more output")
    parser.add_argument("--quiet", action="store_true", default=default, help="less output")


def _add_config_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", metavar="PATH", default=None, help="config file to use")


def build_parser() -> argparse.ArgumentParser:
    # TODO(#22): argparse usage errors print plain text; under --json they must emit the
    # {"error": {"code": "usage", ...}} envelope with exit 2 (interface.md, error envelope).
    parser = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION, allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    _add_global_flags(parser, top=True)
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    rules = commands.add_parser("rules", help="list the rules in order", allow_abbrev=False)
    _add_global_flags(rules, top=False)
    _add_config_flag(rules)

    validate = commands.add_parser("validate", help="check the config offline", allow_abbrev=False)
    _add_global_flags(validate, top=False)
    _add_config_flag(validate)
    # TODO(#29): --probe (it refuses --config, like every command that runs a notifier).
    # TODO(#13): the test command takes --config too. TODO(#22): send refuses it (exit 2 usage).
    return parser


# --- output ----------------------------------------------------------------------------------


def _out(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _err(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()


def _json(document: object) -> None:
    _out(json.dumps(document, separators=(",", ":")) + "\n")


def _safe(value: object) -> str:
    """Text for a terminal: ASCII only, control characters escaped."""
    text = str(value).encode("ascii", "backslashreplace").decode("ascii")
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in text)


def _error(code: str, message: str, details: dict[str, str] | None, *, as_json: bool) -> None:
    if as_json:
        _json({"error": {"code": code, "message": message, "reason": None, "details": details}})
    _err(
        f"{PROG}: {code}: {message}" + (f" (key {_safe(details['key'])})" if details else "") + "\n"
    )


def _match_text(match: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in match.items():
        if isinstance(value, list):
            value = ",".join(str(v) for v in value)  # pyright: ignore[reportUnknownArgumentType]
        elif isinstance(value, dict):
            value = ",".join(f"{k}={v}" for k, v in value.items())  # pyright: ignore[reportUnknownVariableType]
        parts.append(f"{key}={value}")
    return " ".join(parts) or "(everything)"


def _rules_text(doc: dict[str, Any]) -> None:
    for index, rule in enumerate(doc["rules"], 1):
        if rule["drop"]:
            action = "drop"
        else:
            kind = " pass-through" if rule["pass_through"] else ""
            level = rule["level"] or "from notice"
            targets = ",".join(rule["destinations"])
            action = f"-> {targets} level={level} dedup={rule['dedup_window_s']}s{kind}"
        _out(_safe(f"{index}. {rule['name']}: {_match_text(rule['match'])} {action}") + "\n")
    for d in doc["destinations"]:
        state = "enabled" if d["enabled"] else "disabled"
        quiet = d["quiet"] or "none"
        _out(
            _safe(
                f"destination {d['name']}: {d['kind']} floor={d['floor']} quiet={quiet} "
                f"max_per_hour={d['max_per_hour']} {state}"
            )
            + "\n"
        )
    for w in doc["quiet_hours"]:
        _out(
            _safe(
                f"quiet {w['name']}: {w['start']}-{w['end']} {w['timezone']} {','.join(w['days'])}"
            )
            + "\n"
        )


def _validate_text(doc: dict[str, Any]) -> None:
    _out(f"outcome: {doc['outcome']}\n")
    for d in doc["destinations"]:
        unset = ",".join(d["env_unset"]) or "none"
        surfaced = ",".join(d["surfaced_today"]) or "none"
        _out(
            _safe(
                f"destination {d['name']}: cmd_ok={str(d['cmd_ok']).lower()} "
                f"env_unset={unset} surfaced_today={surfaced}"
            )
            + "\n"
        )
    for w in doc["warnings"]:
        where = w["rule"] or w["destination"]
        _out(
            _safe(f"warning {w['code']}" + (f" ({where})" if where else "") + f": {w['message']}")
            + "\n"
        )
    age = "never" if doc["flush_age_s"] is None else f"{doc['flush_age_s']}s ago"
    _out(f"send_worst_case_s: {doc['send_worst_case_s']}\nlast flush: {age}\n")


# --- commands --------------------------------------------------------------------------------


def _run_rules(args: argparse.Namespace, gateway: Gateway) -> int:
    config = load_valid_config(gateway, args.config)
    doc = rules_document(config)
    if args.json:
        _json(doc)
    else:
        _rules_text(doc)
    if config is None:
        if not args.quiet:
            _err(f"{PROG}: no config\n")
        return EXIT_NOOP
    return EXIT_OK


def _run_validate(args: argparse.Namespace, gateway: Gateway) -> int:
    config = load_valid_config(gateway, args.config)
    doc, code = validate_document(config, gateway)
    if args.json:
        _json(doc)
    else:
        _validate_text(doc)
    if config is None and not args.quiet:
        _err(f"{PROG}: no config\n")
    return code


def main(argv: Sequence[str] | None = None, gateway: Gateway | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if args.verbose and args.quiet:
        parser.error("--verbose and --quiet cannot be combined")
    gateway = Gateway() if gateway is None else gateway
    try:
        if args.command == "rules":
            return _run_rules(args, gateway)
        return _run_validate(args, gateway)
    except ConfigError as exc:
        _error(exc.code, str(exc), {"key": exc.key}, as_json=args.json)
        return EXIT_USAGE
    except HomeError:
        _error("config_invalid", "invalid configuration", {"key": "home"}, as_json=args.json)
        return EXIT_USAGE
    except StateError:
        _error("internal", "state database cannot be read", None, as_json=args.json)
        return EXIT_INTERNAL
    except Exception:  # an unexpected failure: a fixed message, never its text
        _error("internal", "internal error", None, as_json=args.json)
        return EXIT_INTERNAL
