# Fake notifier: fidelity notes

`notifier.py` stands in for a channel notifier (slack-notice, ha-notice, or any command with
the same call shape) in notice-route's tests. It is a test aid, not a re-implementation. This
file says what it follows, where it is simpler than the real programs, and what has not been
checked against them. Items under "Unverified" go to the live-verification issue (#31).

The sources are the notifiers' interfaces (`Calling from automation`, `Exit codes`,
`Error envelope`, `Notification`, `Output`) and their schemas, pinned under
`tests/sibling-schemas/` (see each `PIN.json` for the version and source commit).

## How it is built and run

- Standard library only. `tests/fakes.py` writes a stub per destination: the first line is the
  absolute test interpreter (`-I`), and the path of a JSON behavior file is baked in, so the
  router's scrubbed environment (`PATH`, `LANG`, `LC_ALL`, named variables) cannot lose it.
  A destination's `cmd` is the stub's path.
- Behavior is read from the file at every call, so a test can change it between calls
  (`Notifier.set`). `sequence` gives call-by-call behavior (the call number is taken under a
  `flock`, so parallel calls get distinct numbers).
- Every call appends JSON lines to the log: `invoke` (PID, start time, process group and
  session, cwd, argv, full environment, open descriptors), `stdin`, `signal`, `grandchild`,
  `hang_started`, `exit`. A process killed by a signal writes no `exit`.
- Safety: a hang, sleep or linger never outlives `max_seconds` (default 60, exit 124); a hang
  also ends when its parent changes (not when the parent was already PID 1); a grandchild ends
  after `lifetime` (default 20 s, at most 120). Tests signal only recorded PIDs whose start
  time still matches (`Notifier.kill_tracked`).
- A broken setup (unknown key, bad type, a relative `log`) is one `fakebin: ...` line on stderr
  and exit 70, never a traceback.

## Behaviors

| Key | Effect |
|---|---|
| `exit` | the exit code, any 0 to 255 |
| `error` | `{code, ...}`: the error envelope with the per-code flag and details defaults of the interfaces; the exit code follows the code (usage and config_invalid 2, internal 1, refused 20, the rest 12) unless `exit` is set |
| `result` | fields merged into the default success object |
| `stdout`, `stdout_b64`, `no_output`, `stderr` | any output, byte for byte, or none |
| `stdout_bytes`, `stderr_bytes` | that many bytes of padding (oversized output) |
| `sleep`, `linger`, `hang`, `hang_before_read` | time before the answer, after it, until killed, and before stdin is read |
| `signals` | per TERM, HUP or INT: `ignore`, `default` or `exit:N` |
| `die_by_signal` | the fake kills itself with that signal after its output |
| `grandchild` | a descendant: `setsid`, `hold_stdout`, `hold_stderr`, `double_fork`, `lifetime` |
| `close_stdin` | closes stdin without reading it (a writer then sees EPIPE) |
| `disabled` | exit 22 and `{"outcome":"disabled"}` before any input is read |
| `validate_input` | `false` skips the payload checks below |
| `tool` | `slack-notice` (default) or `ha-notice`: the shape of the success object and a few input rules |
| `sequence` | a list of the above, one entry per call, the last one repeating |

## Modeled from the interfaces

- Argument grammar: exact flags (no abbreviations), `--flag=value`, a separate value may not
  start with `-`, a value flag at most once (`--decision-id` repeats), no bare `--`, no short
  option but `-h`, boolean flags refuse `=value`, `--verbose` with `--quiet`, the actor
  pattern, a dedupe key of 1 to 1024 characters, the source-combination rules of `send`. Any
  `--json` or `--json=...` token selects the envelope, found before parsing.
- Exit codes 0, 1, 2, 12, 20, 22 and the envelope; stdout is written before the stderr line.
- Input: strict reader (duplicate keys, NaN, a BOM, invalid UTF-8, more than 1 MiB, nesting
  over 100), the field checks and their pointers, the link rule, an empty message after the
  sanitizer's removals, and for `ha-notice` the command keywords. For `slack-notice` any
  number is a parse failure (pointer `""`); for `ha-notice` it is a type error at the field.
- The pinned `send` and `error` schemas validate the fake's default outputs in tests.

## Simplifications

- Not modeled (exit 1, code `fake_unsupported`, a code in no sibling schema): `clear`,
  `validate`, `--dry-run`, `--file` and the text flags as input sources, ha-notice `actions`.
- The sanitizer is reduced to the emptiness test (removed characters, whitespace and marks).
  Text is never cut, so `truncated` is always false, and `bytes` is an approximation of the
  request body.
- No config, no credential check, no network, no send lock, no spacing, no log file of its own.
- `duration_ms` is 0 and `http_status` 200. The messages of envelopes are fixed fake strings.
- A signal before the send starts exits 128+N only when scripted (`signals`); by default the
  fake dies by the signal.
- The pinned slack-notice error schema rejects its own documented `"details": null` for every
  code but `unavailable` (`not: {required: [phase]}` is true for null). `tests/sibling_schemas.py`
  tolerates exactly that message (`KNOWN_DEFECTS`) and a test fails when a re-pin fixes it.
- Authored hook payload examples (`examples/` under agent-spawn's pin) are written here: the
  producer publishes schemas but no examples. They validate against the pinned schemas.

## Unverified (for the live-verification issue, #31)

- wsl-notice: no repository or interface exists yet in the workspace; its call shape, exit
  codes and envelope are assumed to be the same as the other two. Its `WSL_INTEROP` and
  `WSL_DISTRO_NAME` handling and its lock behavior are not modeled.
- slack-notice is pinned at its design version (0.1.0-design, no implementation released):
  everything about it comes from its interface and schemas. ha-notice is pinned at 0.28.0 but
  the fake was not compared with a real run.
- That a real notifier prints exactly one line (trailing newline) on stdout, and the exact
  usage message text and stderr lines.
- The real exit code and output when a signal arrives (exit 128+N before the send starts, the
  deadline treatment afterwards) and when stdout or stdin is closed early.
- Whether a real notifier accepts `--config`/`--actor` placed before the subcommand in every
  combination, the handling of a value flag given on both sides of the subcommand, and
  abbreviations (the interfaces say they are refused; checked from the text only).
- The sanitizer's exact empty-message and command-keyword behavior beyond the documented
  tables, and the `bytes` and `truncated` values.
