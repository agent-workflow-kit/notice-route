"""Command-line entry point for notice-route: parsing, commands, output and exit codes.

``main(argv, gateway)`` is the call shape; tests pass a gateway with a private home.

Output rules (docs/interface.md, Output and Exit codes):

- ``--json`` prints one document on stdout. A config, state or internal error prints the error
  envelope on stdout under ``--json`` and always one fixed line on stderr; the message never
  holds config text, and ``details.key`` names a key, never a value.
- Without ``--json`` the commands print readable lines on stdout; ``--quiet`` drops the
  advisory stderr line of an unconfigured run.
- ``--config`` is accepted only by the commands that run nothing and write nothing (``rules`` and
  ``validate`` and ``test``); a missing file is ``config_invalid``, key ``--config``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from typing import Any, NoReturn

from . import __version__
from .config import ConfigError
from .cron import CronError, run_cron
from .decide import instant
from .dryrun import dry_run_document
from .flush import FlushOptions, FlushResult, run_flush
from .gateway import Gateway, HomeError, Interrupted, StateError
from .history import (
    DEFAULT_LIMIT,
    DESTINATION,
    LABEL,
    LEVELS,
    MAX_LIMIT,
    OUTCOMES,
    SOURCE,
    HistoryFilters,
    parse_since,
    run_history,
)
from .payload import InputFlags, PayloadError, UsageError, load_notification
from .prune import PruneOptions, run_prune
from .report import rules_document, validate_document
from .send import SendOptions, SendResult, run_send
from .store import StoreError, open_readonly
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
    parser.add_argument(
        "--config", metavar="PATH", default=argparse.SUPPRESS, help="config file to use"
    )


def _add_audit_flags(parser: argparse.ArgumentParser) -> None:
    """``--actor`` and ``--decision-id`` of the commands that act (``send``; ``flush`` #23)."""
    parser.add_argument(
        "--actor", default=None, metavar="ACTOR", help="human:<name> or auto:<name>"
    )
    parser.add_argument(
        "--decision-id", action="append", default=[], metavar="ID", help="a caller's decision id"
    )


class _ArgvError(Exception):
    """The command line could not be parsed; carries no text (argparse would echo the input)."""


class _Parser(argparse.ArgumentParser):
    """An argument parser whose usage errors never print argparse's text, which echoes argv."""

    def error(self, message: str) -> NoReturn:
        raise _ArgvError


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, description=DESCRIPTION, allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    _add_global_flags(parser, top=True)
    # A global flag, accepted before or after the command; main refuses it for every command
    # except rules, test and validate.
    parser.add_argument("--config", metavar="PATH", default=None, help="config file to use")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    rules = commands.add_parser("rules", help="list the rules in order", allow_abbrev=False)
    _add_global_flags(rules, top=False)
    _add_config_flag(rules)

    validate = commands.add_parser("validate", help="check the config offline", allow_abbrev=False)
    _add_global_flags(validate, top=False)
    _add_config_flag(validate)
    # TODO(#29): --probe (it refuses --config, like every command that runs a notifier).

    test = commands.add_parser(
        "test", help="show what send would do, without doing it", allow_abbrev=False
    )
    _add_global_flags(test, top=False)
    _add_config_flag(test)
    _add_input_flags(test)

    send = commands.add_parser("send", help="route one notification", allow_abbrev=False)
    _add_global_flags(send, top=False)
    _add_input_flags(send)
    send.add_argument("--dry-run", action="store_true", help="same as test; changes nothing")
    _add_audit_flags(send)
    # Parsed only to refuse it with exit 2 `usage`: send never reads a config from a flag.
    send.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    flush = commands.add_parser("flush", help="deliver the due queue rows", allow_abbrev=False)
    _add_global_flags(flush, top=False)
    flush.add_argument(
        "--destination", action="append", default=[], metavar="NAME", help="only this destination"
    )
    flush.add_argument("--dry-run", action="store_true", help="list what is due; change nothing")
    _add_audit_flags(flush)
    flush.add_argument(
        "--config", metavar="PATH", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    history = commands.add_parser(
        "history", help="list recent decisions, newest first", allow_abbrev=False
    )
    _add_global_flags(history, top=False)
    for flag, meta in (
        ("--since", "DURATION|TIME"),
        ("--source", "NAME"),
        ("--destination", "NAME"),
        ("--level", "LEVEL"),
        ("--outcome", "OUTCOME"),
        ("--limit", "N"),
    ):
        # appended, so that a repeated filter can be refused (exit 2) instead of one winning
        history.add_argument(flag, action="append", default=[], metavar=meta)
    history.add_argument(
        "--label", action="append", default=[], metavar="K=V", help="exact key=value; repeatable"
    )
    history.add_argument(
        "--config", metavar="PATH", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    prune = commands.add_parser(
        "prune", help="delete old history and expire old queue rows", allow_abbrev=False
    )
    _add_global_flags(prune, top=False)
    prune.add_argument("--dry-run", action="store_true", help="count what would go; change nothing")
    prune.add_argument(
        "--config", metavar="PATH", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    cron = commands.add_parser(
        "cron", help="install or remove the crontab block", allow_abbrev=False
    )
    _add_global_flags(cron, top=False)
    cron.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    cron_actions = cron.add_subparsers(dest="cron_action", metavar="<action>", required=True)
    for name, text in (("install", "write the block"), ("uninstall", "remove the block")):
        action = cron_actions.add_parser(name, help=text, allow_abbrev=False)
        _add_global_flags(action, top=False)
        action.add_argument("--dry-run", action="store_true", help="show; change nothing")
        action.add_argument(
            "--config", metavar="PATH", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )
    return parser


def _add_input_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--stdin-json", action="store_true", help="read one JSON object on stdin")
    parser.add_argument("--file", metavar="PATH", default=None, help="read the JSON from a file")
    parser.add_argument("--title", default=None, help="a plain title (untrusted text)")
    parser.add_argument("--message", default=None, help="a plain message (untrusted text)")
    parser.add_argument("--source", default=None, help="the source of a channel notice")
    parser.add_argument("--label", action="append", default=[], metavar="K=V", help="a label")


# --- output ----------------------------------------------------------------------------------


class _OutputClosed(Exception):
    """Standard output cannot be written (a closed pipe); nothing more is written to it."""


def _out(text: str) -> None:
    if sys.stdout is None:  # closed with `>&-`
        raise _OutputClosed
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except OSError:
        raise _OutputClosed from None


def _err(text: str) -> None:
    if sys.stderr is None:
        return
    sys.stderr.write(text)
    sys.stderr.flush()


def _json(document: object) -> None:
    _out(json.dumps(document, separators=(",", ":")) + "\n")


def _safe(value: object) -> str:
    """Text for a terminal: ASCII only, control characters escaped, a backslash doubled so an
    escape never reads as the text itself."""
    out: list[str] = []
    for c in str(value):
        n = ord(c)
        if c == "\\":
            out.append("\\\\")
        elif c.isascii() and c.isprintable():
            out.append(c)
        elif n <= 0xFF:
            out.append(f"\\x{n:02x}")
        elif n <= 0xFFFF:
            out.append(f"\\u{n:04x}")
        else:  # the interface allows only \xNN and \uNNNN: a surrogate pair
            n -= 0x10000
            out.append(f"\\u{0xD800 + (n >> 10):04x}\\u{0xDC00 + (n & 0x3FF):04x}")
    return "".join(out)


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


def _list_text(items: Sequence[str]) -> str:
    return ",".join(items) or "none"


def _dry_run_text(doc: dict[str, Any]) -> None:
    def show(value: object) -> str:
        return "none" if value is None else str(value)

    lines = [
        f"shape: {doc['shape']}",
        f"source: {doc['source']}",
        f"hook: {show(doc['hook'])}",
        f"rule: {show(doc['rule'])}",
        f"reason: {show(doc['reason'])}",
        f"level: {show(doc['level'])}",
        f"title: {show(doc['title'])}",
        f"message: {show(doc['message'])}",
        f"link: {show(doc['link'])}",
        f"missing: {_list_text(doc['missing'])}",
        f"excluded: {_list_text(doc['excluded'])}",
        f"text_excluded: {str(doc['text_excluded']).lower()}",
        f"labels_ignored: {doc['labels_ignored']}",
        f"dedupe: {show(doc['dedupe'])}",
    ]
    for d in doc["destinations"]:
        why = f" ({d['reason']})" if d["reason"] else ""
        after = "" if d["deliver_after"] is None else f" deliver_after={d['deliver_after']}"
        lines.append(f"destination {d['name']}: {d['decision']}{why}{after}")
    for line in lines:
        _out(_safe(line) + "\n")


def _run_dry_run(args: argparse.Namespace, gateway: Gateway) -> int:
    """``test`` and ``send --dry-run``: send steps 1 to 6 for the config alone. Nothing is
    written, locked, spawned or sent; ``state.db`` is opened read-only once the router is enabled
    and configured (nothing is created, migrated or locked); one that cannot be used is exit 1."""
    started = gateway.monotonic()
    flags = InputFlags(
        stdin_json=args.stdin_json,
        file=args.file,
        title=args.title,
        message=args.message,
        source=args.source,
        labels=tuple(args.label),
    )
    is_test = args.command == "test"
    if not is_test:
        _audit(args, gateway)  # send --dry-run takes --actor and --decision-id like send
    notification = load_notification(gateway, flags, started, default_stdin=is_test)
    config = load_valid_config(gateway, args.config if is_test else None)
    doc, code = dry_run_document(
        config,
        notification,
        lambda: open_readonly(gateway),
        instant(gateway.wall()),
        dict(gateway.environ()),
    )
    if args.json:
        _json(doc)
    else:
        _dry_run_text(doc)
    if doc["reason"] == "unconfigured" and not args.quiet:
        _err(f"{PROG}: no config\n")
    return code


_ACTOR = re.compile(r"(?:human|auto):[A-Za-z0-9._@/-]{1,64}")
_DECISION_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_NAME_OUTSIDE = re.compile(r"[^A-Za-z0-9._@-]")


def _default_actor(gateway: Gateway) -> str:
    """``human:<login name>`` when stdin is a terminal, else ``auto:unknown``."""
    user = _NAME_OUTSIDE.sub("_", gateway.login_name())[:64]
    return f"human:{user}" if user and gateway.stdin_is_tty() else "auto:unknown"


def _audit(args: argparse.Namespace, gateway: Gateway) -> tuple[str, str | None]:
    """The actor and the decision id of the call (design guide section 6): ``--actor``, else
    ``human:<login name>`` when stdin is a terminal, else ``auto:unknown``."""
    actor = args.actor
    if actor is None:
        actor = _default_actor(gateway)
    elif not _ACTOR.fullmatch(actor):
        raise UsageError("actor")
    if len(args.decision_id) > 1 or (
        args.decision_id and not _DECISION_ID.fullmatch(args.decision_id[0])
    ):
        raise UsageError("decision_id")
    return actor, (args.decision_id[0] if args.decision_id else None)


def _run_send(args: argparse.Namespace, gateway: Gateway) -> int:
    """``send`` steps 1 to 8. The trap is armed for the input and config reads, where a signal
    exits 128+N at once; once the decision is written a signal is handled by ``run_send``."""
    started = gateway.monotonic()
    actor, decision_id = _audit(args, gateway)
    flags = InputFlags(
        stdin_json=args.stdin_json,
        file=args.file,
        title=args.title,
        message=args.message,
        source=args.source,
        labels=tuple(args.label),
    )
    with gateway.trapped_signals() as trap:
        trap.armed = True
        try:
            notification = load_notification(gateway, flags, started)
            config = load_valid_config(gateway, None)
            result = run_send(
                gateway,
                config,
                notification,
                SendOptions(actor, decision_id, args.verbose),
                started,
                trap,
            )
        except Interrupted as exc:
            return 128 + exc.signum
        finally:
            trap.armed = False
        # The trap stays installed (a signal only sets a flag) until the output is written, so a
        # late signal never kills the process before the document is printed. During the output
        # a signal ends the process at once (128+N): the result is final and a reader that
        # stopped reading must not hold the process until SIGKILL.
        trap.armed = True
        try:
            _send_output(args, gateway, result)
        except Interrupted as exc:
            gateway.discard_stdout()
            gateway.discard_stderr()
            return 128 + exc.signum
        finally:
            trap.armed = False
    return result.exit_code


def _run_flush(args: argparse.Namespace, gateway: Gateway) -> int:
    """``flush``: the flags (exit 2), the recursion guard (exit 20), the config, then the run. The
    trap is armed for the config load; during the run it only sets a flag."""
    started = gateway.monotonic()
    actor, decision_id = _audit(args, gateway)
    if gateway.env_present("NOTICE_ROUTE_HOOK"):
        raise PayloadError("hook_recursion")
    options = FlushOptions(
        actor, decision_id, args.dry_run, tuple(dict.fromkeys(args.destination)), args.verbose
    )
    with gateway.trapped_signals() as trap:
        trap.armed = True
        try:
            config = load_valid_config(gateway, None)
            result = run_flush(gateway, config, options, started, trap)
        except Interrupted as exc:
            return 128 + exc.signum
        finally:
            trap.armed = False
        trap.armed = True
        try:
            _send_output(args, gateway, result)
        except Interrupted as exc:
            gateway.discard_stdout()
            gateway.discard_stderr()
            # 128+N only when no call started: after one, the exit is the run's result
            return result.exit_code if result.called else 128 + exc.signum
        finally:
            trap.armed = False
    return result.exit_code


_LIMIT = re.compile(r"[1-9][0-9]{0,3}")


def _history_filters(args: argparse.Namespace, now: int) -> HistoryFilters:
    """The filters of ``history``; anything outside its grammar is exit 2 (``usage``), the text
    is never echoed."""
    once: dict[str, str] = {}
    for name in ("since", "source", "destination", "level", "outcome", "limit"):
        given: list[str] = getattr(args, name)
        if len(given) > 1:
            raise UsageError("filter")
        if given:
            once[name] = given[0]
    since = None
    if "since" in once:
        since = parse_since(once["since"], now)
        if since is None:
            raise UsageError("filter")
    for name, pattern in (("source", SOURCE), ("destination", DESTINATION)):
        if name in once and not pattern.fullmatch(once[name]):
            raise UsageError("filter")
    if "level" in once and once["level"] not in LEVELS:
        raise UsageError("filter")
    if "outcome" in once and once["outcome"] not in OUTCOMES:  # suppressed writes no row
        raise UsageError("filter")
    limit = DEFAULT_LIMIT
    if "limit" in once:
        if not _LIMIT.fullmatch(once["limit"]) or int(once["limit"]) > MAX_LIMIT:
            raise UsageError("limit")
        limit = int(once["limit"])
    for label in args.label:
        if not LABEL.fullmatch(label):
            raise UsageError("filter")
    return HistoryFilters(
        since,
        once.get("source"),
        once.get("destination"),
        once.get("level"),
        once.get("outcome"),
        tuple(dict.fromkeys(args.label)),
        limit,
    )


def _history_line(row: dict[str, Any]) -> str:
    """One row for a terminal: every field but the title is within a closed grammar; the title
    is untrusted text, cut, quoted and escaped by :func:`_safe`."""

    def show(value: object) -> str:
        return "-" if value is None else str(value)

    reason = "" if row["reason"] is None else f"/{row['reason']}"
    title = "" if row["title"] is None else ' "' + _safe(row["title"]).replace('"', '\\"') + '"'
    return (
        _safe(
            f"{row['id']} {row['ts']} {row['outcome']}{reason} {row['source']}"
            f" {show(row['destination'])} {show(row['level'])} rule={show(row['rule'])}"
            f" attempts={row['attempts']} count={row['count']}"
        )
        + title
    )


def _run_history(args: argparse.Namespace, gateway: Gateway) -> int:
    filters = _history_filters(args, int(gateway.wall()))
    result = run_history(gateway, filters)
    if args.json:
        _json(result.rows)
    else:
        for row in result.rows:
            _out(_history_line(row) + "\n")
    if result.skipped and not args.quiet:
        _err(f"{PROG}: warning: {result.skipped} unreadable history rows left out\n")
    return EXIT_OK


_PRUNE_TABLES = ("history", "event_ids", "dedup", "surfaced", "rate", "queue")


def _run_prune(args: argparse.Namespace, gateway: Gateway) -> int:
    """``prune``: the flags (exit 2), the config, then the run. It needs no ``--actor``: the
    log lines it writes carry the default actor."""
    config = load_valid_config(gateway, None)
    result = run_prune(gateway, config, PruneOptions(_default_actor(gateway), args.dry_run))
    doc = result.document
    assert doc is not None
    for text in result.warnings:  # as send and flush: a warning is printed under --quiet too
        _guarded(gateway, "err", lambda text=text: _err(f"{PROG}: warning: {_safe(text)}\n"))
    if args.json:
        _json(doc)
    elif not args.quiet:
        for table in _PRUNE_TABLES:
            _out(f"{table}: {doc['deleted'][table]}\n")
    return result.exit_code


def _run_cron(args: argparse.Namespace, gateway: Gateway) -> int:
    """``cron install|uninstall``: install reads the config (``cron.env_file``), uninstall does
    not. Nothing is logged and no state directory is touched."""
    try:
        config = load_valid_config(gateway, None) if args.cron_action == "install" else None
        result = run_cron(gateway, config, args.cron_action, args.dry_run)
    except CronError as exc:
        _error(exc.code, exc.message, None, as_json=args.json)
        return exc.exit_code
    if args.json:
        _json(result.document)
    elif not args.quiet:
        for line in result.lines:
            _out(_safe(line) + "\n")
    return EXIT_OK


def _guarded(gateway: Gateway, stream: str, write: Callable[[], None]) -> None:
    """One write after the send is done: a failure changes nothing (the exit code is the
    result's), and the stream is pointed at the null device so its buffered text cannot fail
    again when the interpreter exits."""
    try:
        write()
    except (OSError, ValueError, _OutputClosed):
        if stream == "out":
            gateway.discard_stdout()
        else:
            gateway.discard_stderr()


def _send_output(
    args: argparse.Namespace, gateway: Gateway, result: SendResult | FlushResult
) -> None:
    def err(text: str) -> None:
        _guarded(gateway, "err", lambda: _err(f"{PROG}: {_safe(text)}\n"))

    for text in result.warnings:
        err(f"warning: {text}")
    if not args.quiet:
        for text in result.advisories:
            err(text)
    if args.verbose:
        for text in result.verbose:
            err(text)
    # a clean ``flush`` under --quiet (a cron line) prints no per-destination line: only
    # warnings and the lines of a run that failed
    lines = (
        []
        if args.quiet and isinstance(result, FlushResult) and result.exit_code in (0, 22)
        else result.lines
    )
    if result.error is not None:
        code, message = result.error
        if args.json:
            envelope = {
                "error": {"code": code, "message": message, "reason": None, "details": None}
            }
            _guarded(gateway, "out", lambda: _json(envelope))
        else:
            for text in lines:
                err(text)
        _guarded(gateway, "err", lambda: _err(f"{PROG}: {code}: {message}\n"))
    elif result.document is not None:
        if args.json:
            document = result.document
            _guarded(gateway, "out", lambda: _json(document))
        else:
            for text in lines:
                err(text)


def _refused(exc: PayloadError | UsageError, *, as_json: bool) -> int:
    """Exit 20 (refused) or 2 (usage): the fixed message, and a pointer word for a refusal."""
    if as_json:
        _json(exc.to_error())
    pointer = getattr(exc, "pointer", None)
    code = "refused" if isinstance(exc, PayloadError) else "usage"
    _err(
        f"{PROG}: {code}: {exc.message}"
        + (f" (pointer {pointer or '.'})" if pointer is not None else "")
        + "\n"
    )
    return exc.exit_code


def main(argv: Sequence[str] | None = None, gateway: Gateway | None = None) -> int:
    gateway = Gateway() if gateway is None else gateway
    try:
        pending: SystemExit | None = None
        try:
            code = _main(argv, gateway)
        except SystemExit as exit_:  # --help / --version: argparse prints, then exits
            pending, code = exit_, 0
        if sys.stdout is not None:
            try:
                sys.stdout.flush()
            except OSError:
                raise _OutputClosed from None
        if pending is not None:
            raise pending
        return code
    except _OutputClosed:
        gateway.discard_stdout()
        try:
            _err(f"{PROG}: internal: cannot write to standard output\n")
        except OSError:
            pass
        return EXIT_INTERNAL


def _main(argv: Sequence[str] | None, gateway: Gateway | None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except _ArgvError:
        given = sys.argv[1:] if argv is None else argv
        return _refused(UsageError("argv"), as_json="--json" in given)
    if args.command is not None and args.verbose and args.quiet:
        return _refused(UsageError("verbose_quiet"), as_json=args.json)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    gateway = Gateway() if gateway is None else gateway
    try:
        if args.command == "rules":
            return _run_rules(args, gateway)
        if args.config is not None and args.command not in ("test", "validate"):
            raise UsageError("config")  # only the commands that run nothing take --config
        if args.command == "test" or (args.command == "send" and args.dry_run):
            return _run_dry_run(args, gateway)
        if args.command == "send":
            return _run_send(args, gateway)
        if args.command == "flush":
            return _run_flush(args, gateway)
        if args.command == "prune":
            return _run_prune(args, gateway)
        if args.command == "history":
            return _run_history(args, gateway)
        if args.command == "cron":
            return _run_cron(args, gateway)
        return _run_validate(args, gateway)
    except (PayloadError, UsageError) as exc:
        return _refused(exc, as_json=args.json)
    except ConfigError as exc:
        _error(exc.code, str(exc), {"key": exc.key}, as_json=args.json)
        return EXIT_USAGE
    except HomeError:
        _error("config_invalid", "invalid configuration", {"key": "home"}, as_json=args.json)
        return EXIT_USAGE
    except (StateError, StoreError) as exc:
        # a prune that ran out of lock waits says so (a cron line is then worth a rerun)
        busy = args.command == "prune" and isinstance(exc, StoreError) and exc.reason == "busy"
        message = "state database is busy" if busy else "state database cannot be read"
        _error("internal", message, None, as_json=args.json)
        return EXIT_INTERNAL
    except _OutputClosed:
        raise
    except Exception:  # an unexpected failure: a fixed message, never its text
        _error("internal", "internal error", None, as_json=args.json)
        return EXIT_INTERNAL
