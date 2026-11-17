# notice-route: interface

Status: 0.1.0-design, 2026-11-17. Written before implementation (architecture section 18.1). This document is the single normative source for notice-route's rules; `docs/design.md`, the README and the architecture link to its headings and do not restate them.

notice-route routes one notification at a time to channel notifiers (slack-notice, ha-notice, wsl-notice, or any command with the same call shape) by source, labels, level and time. It decides what is sent, to whom, when and how often. It keeps a private database for the deferred and retry queue, the duplicate checks and `history`. It runs one hook, `post-fail`.

References: architecture (`../awkit-docs/architecture.md`) sections 5.2, 10.4, 13.2, 13.3, 15 and 20; design guide (`../awkit-docs/design-guide.md`). The notifiers' interfaces are the contract for the calls this tool makes: [slack-notice](../../slack-notice/docs/interface.md), [ha-notice](../../ha-notice/docs/interface.md) and [wsl-notice](../../wsl-notice/docs/interface.md), each at its heading `Calling from automation`, `Exit codes` and `Error envelope`.

## Concepts

### Input shapes

`send --stdin-json` reads exactly one JSON object from stdin, of one of two shapes, told apart by keys alone. There is no mode flag.

| Shape | Recognized when | Source | Level |
|---|---|---|---|
| Hook payload | the keys `tool` and `hook` are both strings | `payload.tool` | none in the payload; the matching rule sets it |
| Channel notice | the keys are only `title`, `message`, `level` and `link`, and `message` is present | `--source`, else the fixed word `direct` | the notice's own `level`, used only when the rule sets none |

Anything else exits 20 with reason `payload_unrecognized` and nothing is sent. For a hook payload, `--source` may only repeat `payload.tool`; any other value exits 20 with reason `source_mismatch`. A source is 1 to 64 characters of `[a-z0-9][a-z0-9_-]*`, else `payload_invalid` (pointer `/tool` or `--source`).

Input is read from stdin only for `--stdin-json`; `--file <path>` and `-` follow the same rules (`--file -` is stdin). The plain flags `--title` and `--message` build a channel notice for trusted human use; their text is untrusted like any other. A call that mixes a JSON source with `--title` or `--message` is a usage error (exit 2). If stdin is a terminal and no source is given, exit 2.

The reader is strict. At most 1048576 bytes are read; more, bytes that are not valid UTF-8, a byte-order mark, invalid JSON, a duplicate key at any depth, `NaN`, `Infinity` or a number literal outside the 64-bit range, a nesting depth above 16, a string containing a lone surrogate, or a top level that is not an object exits 20 `payload_invalid` with `details.pointer` `""`. Hook payloads may carry any other keys; the router reads only the scalar fields a rule or template names, plus `event_id`, `dedupe_key`, `object_id`, `object_type`, `repo`, `labels`, `reads_third_party`, `tainted` and `ts`.

`labels` in a hook payload is an array of `key=value` strings (split at the first `=`). `--label key=value` (repeatable, at most 32) adds labels in both shapes, merged with the payload's own. A label key is `[a-z0-9][a-z0-9_.-]{0,63}` and a value is 0 to 256 characters of `[A-Za-z0-9._:/#@+=,-]`; a malformed payload label is ignored and counted in `details.labels_ignored`, a malformed `--label` is a usage error. The channel notice's own fields follow the notifiers' rules: `title` at most 100000 characters, `message` 1 to 100000 characters, `level` one of `info`, `warn`, `action`, `error`, `link` rules as in [Links](#links). A violation is exit 20 `payload_invalid` with `details.pointer` `/<field>`.

### Rules

`rules` is an ordered list. For each notification the first rule whose `match` holds decides everything: destinations, template, level and options. No later rule is consulted. A rule that matches with `drop: true` ends the notification with outcome `dropped` and reason `rule_drop`. No matching rule is exit 22 with reason `no_rule`.

A rule has these keys (all validated at load; an unknown key is `config_invalid`):

| Key | Type | Meaning |
|---|---|---|
| `name` | string, `[a-z0-9][a-z0-9_-]{0,63}`, unique | Required. Shown in output and `history`. |
| `match` | object | Required. All present conditions must hold (below). |
| `drop` | boolean | Default false. When true, no other key except `name` and `match` is allowed. |
| `notify` | list of destination names | Required unless `drop`. At least 1, at most 8, each a defined destination, no duplicates. |
| `level` | `info`, `warn`, `action`, `error` | See [Levels](#levels). |
| `title`, `message`, `link` | template strings | See [Templates](#templates). `message` is required unless `drop`; `title` and `link` are optional. |
| `dedup_window` | duration | Default `10m` (the `defaults.dedup_window` key); range 1s to 24h. |
| `include_text` | list of deny-listed top-level field names | Opt-in for untrusted text in the message (see [Untrusted text](#untrusted-text)). |

`match` conditions (closed list in 0.x; no regular expressions, no OR, no negation; a second rule covers an OR):

| Condition | Matches when |
|---|---|
| `source` | the source equals the value, or the value ends in one `*` and the source starts with the text before it |
| `hook` | the hook payload's `hook` equals the value (same `*` rule); a rule with `hook` never matches a channel notice |
| `repo` | the payload's `repo` equals the value (same `*` rule); absent when the payload has none, so the rule does not match |
| `labels` | a list of `key=value` strings, all of which are present among the notification's labels |
| `where` | a map from payload field name (a top-level or dotted scalar path) to a string, integer or boolean; every pair equals; a rule with `where` never matches a channel notice; an absent field does not match |
| `level_min` | the channel notice's own level is at least this level; a rule with `level_min` never matches a hook payload and may not also carry `hook` or `where` |

A `*` is allowed only as the last character. Matching is exact and case-sensitive. A rule with an empty `match` matches everything and is how a catch-all is written; `validate` warns when a rule is unreachable because an earlier rule matches a superset of it.

### Levels

The final level of a notification is the rule's `level` if it has one, else the channel notice's own `level` if present and valid, else `info`. A hook payload has no level, so a rule that can match hook payloads (it has `hook` or `where`) should set one; `validate` warns when it does not. Nothing else can raise a level: there is no `--level` flag and a payload cannot choose it when the rule sets one.

Levels are ordered `info < warn < action < error`. The order matters for destination floors and quiet hours (below); the notifiers do not use it. The level reaches the notifier in the payload, and the notifiers decide how to show it.

### Destinations

A destination is a named channel and the command that reaches it:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `kind` | `slack-notice`, `ha-notice`, `wsl-notice`, `generic` | required | Selects the retry rules (see [Retries and outcomes](#retries-and-outcomes)). |
| `cmd` | list of strings | required | An argv list. `cmd[0]` is an absolute path to an executable owned by the user or root and not group- or world-writable (checked at load and again at each call). No shell is used. The notifier's `--config <path>` belongs here; one config file per destination. |
| `timeout` | duration | `10s` | The notifier's own timeout (the router does not pass it; it must equal what the notifier's config says, and `validate --probe` warns when it is shorter). Range 3s to 120s. The kill deadline is this plus 10 s. |
| `floor` | level | `info` | The lowest level delivered here. |
| `quiet` | name of a `quiet_hours` window | none | The window in which this destination is quiet. |
| `max_per_hour` | integer 0 to 3600 | 30 | The hourly cap (see [Rate limit](#rate-limit)); 0 disables it. |
| `env` | list of variable names | `[]` | The only variables passed to the notifier besides the fixed set (see [Calling the notifiers](#calling-the-notifiers)). |
| `enabled` | boolean | true | When false, the destination is skipped with outcome `disabled`. |

A destination name is `[a-z0-9][a-z0-9_-]{0,31}`. At most 32 destinations. `generic` assumes only the call shape (JSON on stdin) and treats exit 0 as sent and every other exit as a failure that is surfaced once and never retried. A third-party notifier works as `generic` if it reads the notification JSON from stdin; otherwise a small wrapper script in front of it does the adaptation. The router ships no adapter code.

### Quiet hours

`quiet_hours` defines named windows:

| Key | Type | Meaning |
|---|---|---|
| `timezone` | IANA name | Required. 1 to 64 characters matching `[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}`; no default and no reading of the system zone. An unknown name is `config_invalid`. |
| `days` | list of `mon` to `sun` | Default every day. The days on which the window starts. |
| `start`, `end` | `HH:MM` | Required, 24-hour local time, minutes only. `end` equal to or earlier than `start` means the window ends the next day. |

A moment is inside a window when its local wall-clock minute of the day is in `[start, end)` on a day that started in the window, computed from the local time of day and weekday and never by building boundary timestamps. A window that ends in a skipped or repeated hour follows the wall-clock rule, so the hour that repeats at the end of daylight saving time is inside the window for its whole second occurrence when the window is still open at the second occurrence's local time. The next end of a window is stored as a UTC epoch integer.

Within a destination's quiet window, per level: `info` is dropped (`dropped`, reason `quiet`), `warn` and `action` are deferred, `error` is delivered at once. Quiet applies per destination, so one notification can be delivered to one destination and deferred for another. There is no per-rule override.

### Deferral and digest

A deferred notification is stored rendered (title, message, link, level) with `deliver_after` set to the end of the quiet window, or to the end of the current clock hour for `rate_limited`. `flush` delivers the due rows of a destination, if the destination is not quiet at that time.

Due deferred rows of one destination are folded into one digest notification: title `N notifications while quiet`, one message line per row `<level> <source>: <title>` (oldest first, at most 10 lines, then `and K more`), the highest level among them, and the row's link only when there is exactly one row. A destination with exactly one due row gets that row as itself. A destination keeps at most 100 deferred rows; beyond that the oldest are dropped with outcome `dropped`, reason `overflow`, and counted in the digest line. A row older than 24 hours when due is dropped with outcome `expired`. Retry rows are never folded: each is sent as itself.

### Dedup

Two checks run per destination, both inside the same database transaction as the quiet and rate decisions.

The **redelivery check**: a payload `event_id` already routed to this destination (any outcome) is dropped with outcome `suppressed`, reason `redelivered`, and not counted. Event ids are kept for `retention.event_ids` (default 14 days).

The **repeat check**: the key is the payload's `dedupe_key` when it has one. Otherwise it is the SHA-256 of the source, the hook, the `object_id`, the cause fields that exist in the payload (`reason`, `state`, `type`, `needs_attention`, `event`, in that order, each as `name=value`), and the final level, joined with NUL bytes. A channel notice has no payload id; its key is the SHA-256 of the source, the title (empty when absent) and the final level. Within a fixed window that starts at the first notification with this key (length from the rule's `dedup_window`), the first notification is sent and later ones are `suppressed`, reason `repeat`, and counted. The window is not extended by later repeats.

The hex digest of the key, with the destination name appended, is passed to the notifier as `--dedupe-key=<hex>` (the `=` form). The level is part of the key, so a later `warn` cannot replace an earlier `error` in the tag of a notifier that uses one.

### Rate limit

A destination receives at most `max_per_hour` notifications in any sliding 60 minutes, counted from `history` rows with outcome `sent` or `unknown`. A digest counts as one. Over the cap, a notification is deferred with reason `rate_limited` and `deliver_after` at the end of the current clock hour (or the end of the quiet window when one is active). `error` is counted and is not exempt. The first rate-limited notification per destination per hour is surfaced once as failure code `rate_limited` through `post-fail`.

### Retries and outcomes

The notifier's exit code and, for exit 12, its error envelope decide the outcome of one call:

| Notifier result | Outcome | Router action |
|---|---|---|
| exit 0 | `sent` | Record. "Sent" never means delivered or displayed. |
| exit 22 | `disabled` | Successful no-op, recorded, not an alert. |
| exit 12, `retryable` false, `unknown_outcome` false | `failed` | Never retried; surfaced once per code and destination per day. |
| exit 12, `retryable` true, `unknown_outcome` false | `queued_retry` | Retried by `flush`. |
| exit 12, `retryable` false, `unknown_outcome` true | `unknown` | Never retried; recorded as possibly sent. |
| exit 12, both true (ha-notice only, when a tag was sent) | `queued_retry` | Retried within the cap while no newer notification with the same key was sent. |
| exit 20, reason `link_invalid` or `link_scheme` | one immediate retry without `link`, the URL appended to the message | Then `failed` if refused again. |
| exit 20, reason `command_keyword` (ha-notice only) | one immediate retry with the message prefixed by its source and a colon | Then `failed` if refused again. |
| exit 20, any other reason | `failed` | Never retried; surfaced once per reason. |
| exit 2, exit 1 with an envelope, or any other exit | `failed`, code `deployment_fault` | Surfaced once; exit 1 with no envelope is `unknown`. |
| killed by the router's deadline, killed by another signal, output over the cap, an envelope that does not parse | `unknown` | Never retried. |
| the command cannot be started | `failed`, code `exec_failed` | Never retried. |
| any exit other than 0 from a `generic` destination | `failed` | Never retried. |

A retry row is retried after 1 minute, 5 minutes, 30 minutes and 2 hours, at most 5 attempts in total (the first call counts). After the last, the row ends as `failed`, code `retries_exhausted`. A retry row that finds its destination quiet is sent anyway for `error`, and deferred for `warn` and `action`. `unavailable` and `interop_failed` are retryable by the notifiers' flags and follow the same schedule. A call that could not take the destination's lock in time is queued as a retry with reason `lock_busy`, which does not count as an attempt.

### Untrusted text

Payload fields are untrusted unless a producer's interface promises otherwise. A fixed deny-list names the top-level fields that can carry free text: `report`, `message`, `summary`, `details`, `output_tail` and `error`. A template may name any scalar field of the payload by dotted path, except a field on the deny-list or below one. Referring to a deny-listed field in a `title` or `link` is a load error (`config_invalid`). Referring to one in a `message` is a load error unless the rule lists that top-level name in `include_text`.

Even then the text is inserted only when the payload has `reads_third_party: false` and `tainted: false` as booleans. A missing or non-boolean flag is unknown, and the field renders empty with the notification marked `text_excluded`. Inserted text is cut at the first line break, stripped of control and format characters (Unicode categories Cc, Cf, Cs, Co, Cn and the separators Zl, Zp), whitespace runs collapsed to one space, cut at 200 characters, and wrapped in double quotes with any inner double quote replaced by a single quote. The channel notice's own `title` and `message` are not template inputs: they are the message.

### Templates

A template is a string with `{{name}}` tokens. `name` is a field path `[A-Za-z_][A-Za-z0-9_]{0,63}` with up to three further `.`-separated parts of the same form. Substitution is a single pass over the template text: each token is replaced by its value, and the replaced text is never scanned again, so a value that contains `{{x}}` stays literal. A token that is not closed, a token with another syntax, or a template over 2000 characters is a load error. There is no default value, no conditional and no loop.

A value is a string, integer, boolean or number; an absent field, a null, a list or an object renders empty and is reported in `test` as `missing`. `labels.<key>` is the lexically first value of that label. A string value is sanitized like inserted text (single line, no control characters) but is cut at 120 characters, not wrapped. Derived fields computed by the router: `source`, `hook`, `level`, `pr_url` and `ticket_url` (see [Links](#links)). Fields that come from the payload shadow nothing: a payload key named `source` is read as `payload.source` only through the path `payload.source`; the bare names above always mean the derived fields, and the path `payload.<name>` reaches a payload field of the same name.

The rendered title is at most 256 characters and the rendered message 100000; the notifiers cut to their own limits and report `truncated`. If the rendered message is empty after sanitizing, the router uses `<source> <hook>` (or the source alone for a channel notice) so the call never refuses with `empty_message`.

### Links

A link is built only from the router's own `links` tables and fixed payload ids, never from a payload field.

| Derived field | Computed from | Result |
|---|---|---|
| `pr_url` | the payload's `pr` (or `pr_id`) in the form `<host>/<full project path>#<n>` | `https://<host>/<path>/pull/<n>` when `links.hosts.<host>` is `github`, `https://<host>/<path>/-/merge_requests/<n>` when it is `gitlab` |
| `ticket_url` | the payload's `site` and `key` | `links.tickets.<site>` followed by the key |

A host or site that is not in the tables, or a missing id, renders the field empty, and a rule's `link` that renders empty means no link. The notice is still sent. A link must be 1 to 1024 printable ASCII characters of `http` or `https`; the router percent-encodes path characters and gives hosts as punycode. The tables hold only host names and base URLs of the user's own systems.

### Calling the notifiers

For each destination to be sent now the router makes one call. The call is `cmd... send --stdin-json --json --actor auto:notice-route [--dedupe-key=<hex>]`, with the notification on stdin as a JSON object of only the present fields (`title`, `message`, `level`, `link`; absent fields are omitted, never null) and no other flag, so a destination's `cmd` may not contain `send` or any of those flags (`config_invalid`).

- No shell. The child gets its own session and process group, a closed descriptor set beyond stdin, stdout and stderr, an empty working directory (`/`), and a scrubbed environment: `PATH` (the router's own with empty, relative and writable-by-others entries removed), `LANG` and `LC_ALL` (the router's own when they name a UTF-8 locale, else `C.UTF-8`), for a `wsl-notice` destination also `WSL_INTEROP` and `WSL_DISTRO_NAME` when set, and the variables named in the destination's `env`. A name in `env` must match `[A-Za-z_][A-Za-z0-9_]{0,127}` and may not be `HOME`, `PATH`, `IFS`, `ENV`, `BASH_ENV`, `OPENSSL_CONF`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, a proxy variable, or start with `LD_`, `PYTHON`, `XDG_`, `LC_` or `NOTICE_ROUTE_` (`config_invalid`). A named variable that is unset is simply not passed, and `validate` reports it.
- Sends to one destination are serialized across processes by an exclusive `flock` on `locks/<destination>.lock` in the state directory, taken by polling without blocking until `lock_wait` (default 5s, range 1s to 60s) has passed. The kernel releases the lock when its holder dies. A call that cannot take it in time is queued as `lock_busy` (see [Retries and outcomes](#retries-and-outcomes)). Sends to different destinations run in parallel.
- The call has one deadline on the monotonic clock: the destination's `timeout` plus 10 seconds, counted from the process start. At the deadline the router sends SIGTERM to the process group it created, waits 5 seconds, then SIGKILL to that group, then reads what is left. A killed call is `unknown`. The router never signals anything else and never kills by name or pattern.
- Output is read through a bounded reader: at most 65536 bytes of stdout and 16384 bytes of stderr; more than that ends the call as `unknown`, code `output_overflow`. Stdout is decoded strictly as UTF-8 and parsed with the same strict JSON reader as input (duplicate keys, `NaN` and out-of-range numbers rejected) and must be one object; anything else is an unparseable envelope. Stderr is never parsed or stored. A write to a closed stdin (EPIPE) is ignored, and the exit code decides.
- For exit 12 the router reads `error.code`, `error.retryable`, `error.unknown_outcome` and, for refusals, `error.reason`; every flag must be a boolean or the whole envelope is unparseable. A `retryable: true` for a code the notifier documents as non-retryable is believed (the flags, not the code name, decide).
- The router never calls `validate --test-message`, passes no payload text in argv or environment, and never passes `--config` other than what the destination's `cmd` states.

A hook-run `send` ends no later than `lock_wait` plus the largest destination `timeout` plus 11 seconds, plus a second for the database. `validate` prints this figure as `send_worst_case_s`, and warns when it exceeds the default hook timeout of 30 seconds, because a hook that wires `send` should set a longer `timeout` when it does.

## Commands

### `send`

`notice-route send [--stdin-json | --file <path|->] [--title <t>] [--message <m>] [--source <name>] [--label <k=v>]... [--dry-run]`

Routes one notification. In order:

1. Parse flags, read and validate the input (exit 2 or 20 on error), discriminate the shape.
2. Load and validate the config (exit 2). A missing config file or no destinations is exit 22 reason `unconfigured`.
3. Select the first matching rule (exit 22 reason `no_rule` if none; a `drop` rule ends here with outcome `dropped`).
4. Render the title, message and link, and compute the final level (render errors are exit 20 `payload_invalid`; there are none for missing fields).
5. For each selected destination, in one `BEGIN IMMEDIATE` transaction, decide in this order: disabled (`disabled`); below the destination's `floor` (`dropped`, reason `below_floor`); `info` in the destination's quiet window (`dropped`, reason `quiet`); the redelivery check; the repeat check; `warn` or `action` in quiet (`deferred`); the rate limit (`deferred`, reason `rate_limited`); otherwise send now. Rows for deferred and dropped outcomes are written in the same transaction.
6. Call the notifiers for the destinations to send now, in parallel, as described in [Calling the notifiers](#calling-the-notifiers), record each result and surface failures (`post-fail`).

`--dry-run` stops after step 5, reads no state and takes no lock: it prints what each destination would do, with the quiet and rate decisions computed from the current database read-only, and is the same as `test` with the payload from the same input. Plain `--title` and `--message` are for trusted human use; their values are untrusted text like stdin.

### `rules`

`notice-route rules [--json]`: lists the rules in order with their name, match, destinations, level, `drop` flag and dedup window, plus the destinations and quiet windows they reference. Reads only the config. A config error prints the error envelope with `details.key`.

### `test`

`notice-route test [--input <file|->] [--source <name>] [--label <k=v>]... [--json]`: takes a payload (default stdin) and prints what `send` would do, with no state change, no lock and no notifier call. Output: the shape, the source, the rule that matched (or why none), the rendered title, message and link and their `missing` fields, the final level, the dedup key digest, and for each destination its decision and the reason (send, defer until a time, drop, suppress, rate limit). It never sends, with or without any flag.

### `flush`

`notice-route flush [--destination <name>]... [--dry-run] [--json]`: delivers due rows. For each destination (or those named): claims due retry and deferred rows with a lease (see [State](#files)), checks that the destination is not quiet now (for deferred rows), builds the digest, calls the notifier like `send`, and records the result. Rows of one destination are delivered one call at a time. A second concurrent `flush` for the same destination exits 0 reporting `already_running` for it. `--dry-run` lists what is due. Exit 0 when every due row ended `sent`, `queued_retry` or was dropped or expired; 12 when a call ended `failed` or `unknown`; 22 when nothing was due.

### `history`

`notice-route history [--since <duration|time>] [--source <s>] [--destination <d>] [--level <l>] [--outcome <o>] [--label <k=v>] [--limit <n>] [--json]`: lists history rows newest first, default limit 100, maximum 1000. Filters AND together. The `--json` output is a list validated by `schemas/cli/history.json`. A row never contains message text.

### `validate`

`notice-route validate [--probe] [--json]`: checks the config offline: loads and validates every key, checks that each destination's `cmd[0]` exists and is acceptable, that each `env` name is set in the router's own environment (reported, not an error), that quiet windows and links are well formed, reports the worst-case send time, and warns about the conditions listed under [Config](#config). With `--probe` it also runs each enabled destination's own `<cmd> validate --json` (the notifier's `cmd` with `validate --json` in place of the call flags; never `--test-message`, and `--probe` is passed only to a `ha-notice` or `wsl-notice` destination), reports its `enabled` value and treats its exit 22 as information. It never prompts and never sends. Exit 0 when every check passed; 2 when the config is invalid; 12 when a probe failed.

### `prune`

`notice-route prune [--dry-run]`: deletes history older than `retention.history`, event ids older than `retention.event_ids`, expired dedup rows, surfaced rows older than 2 days and finished queue rows. Never touches a claimed row. Exit 0.

### `cron install` and `cron uninstall`

`notice-route cron install|uninstall [--dry-run]`: writes or removes one tagged block in the user's crontab, in the shape every awkit tool uses: a first line `# notice-route managed v1 flush=5m`, a `PATH=` line, a `*/5 * * * *` line running `flush --quiet` and a daily line running `prune --quiet`, each as `. '<env_file>' && '<absolute notice-route>' ...` when `cron.env_file` is set, and a last line `# notice-route managed end`. The block is replaced idempotently and a block without the end line is refused. Credentials for the notifiers must be in the env file, because the router passes only the variables a destination names.

## Global flags

`--json`, `--config <path>`, `--verbose`, `--quiet`, `--version` where meaningful. `--config` replaces `config.yaml` (drop-ins beside it still apply); it is for a human at a terminal and an agent profile must not allow it. `--dry-run` is accepted by `send`, `flush`, `prune` and `cron`. Abbreviated flags are refused. `--actor` and `--decision-id` are accepted by `send` for audit symmetry with the other tools and are ignored apart from the log; the call to the notifier always uses `auto:notice-route`. There is no `--yes`: nothing here asks for confirmation.

## Output

With `--json`, `send` prints one object ([schemas/cli/send.json](../schemas/cli/send.json)):

- `outcome`: `ok` (exit 0), `partial` (exit 12) or `no_op` (exit 22).
- `shape`: `hook` or `notice`. `source`, `hook` (null for a notice), `rule` (the matched rule's name or null), `level`, `text_excluded` (boolean), `labels_ignored` (integer).
- `destinations`: a list with one entry per selected destination: `name`, `outcome` (`sent`, `deferred`, `queued_retry`, `suppressed`, `dropped`, `disabled`, `failed`, `unknown`), `reason` (a fixed string or null), `attempts`, `code` (the notifier's error code or a router code, or null), `count` (the repeat count for `suppressed`), `deliver_after` (UTC epoch seconds or null), `notifier_outcome` (the notifier's own `outcome` for exit 0 or 22, or null).
- `dedupe` (the hex digest used, or null), `duration_ms`.

Exit 22 prints `{"outcome": "no_op", "reason": "unconfigured" | "no_rule" | "all_disabled" | "disabled"}`. Exit 20 and exit 2 print the error envelope: `{"error": {"code", "message", "reason", "details"}}`, with `code` `refused` or `usage` or `config_invalid` and a fixed `message` (never input text). Without `--json`, stdout is empty and one line per destination goes to stderr; stderr is advisory. The other commands' `--json` outputs have schemas under `schemas/cli/`.

## Exit codes

Reserved codes 0-21 follow design guide section 4.2 (`../awkit-docs/design-guide.md`).

| Exit | Meaning |
|---|---|
| 0 | The router took responsibility for every destination: each ended `sent`, `deferred`, `queued_retry`, `suppressed`, `dropped` or `disabled`. |
| 1 | `internal`. |
| 2 | `usage`, or `config_invalid`, or `config_unsafe` (the config directory, file or a drop-in is not owned by the user or is group- or world-writable). |
| 12 | At least one destination ended `failed` or `unknown` in this call. The other destinations were still delivered. `partial` in the output. |
| 20 | Refused, nothing sent. Reasons: `payload_unrecognized`, `source_mismatch`, `payload_invalid`, `hook_recursion`. |
| 22 | Nothing to do: no config or no destination (`unconfigured`), no rule matched (`no_rule`), every selected destination disabled (`all_disabled`) or the router disabled. A caller treats it as a successful no-op. |
| 128+N | A signal N before any notifier was called; nothing was sent. |

Never used: 10, 11, 13, 21. A signal after the first notifier call started is handled as the deadline: the router finishes the calls it started within their own deadlines and then exits with the result.

## Hooks

One hook, `post-fail`, configured under `hooks` in the router's config as in design guide section 8.

| Hook | When it fires | Can veto |
|---|---|---|
| `post-fail` | After a destination's failure was surfaced: once per error code and destination per day, plus `rate_limited` once per destination per hour. Async, best effort, at most once (no outbox in 0.x; a hook that fails is logged and the `history` row shows `surfaced`). | no |

The payload ([schemas/hooks/post-fail.json](../schemas/hooks/post-fail.json)) has `tool`, `hook`, `object_type` (`destination`), `object_id` (the destination name), `event_id`, `code` (a notifier error code or one of `retries_exhausted`, `rate_limited`, `deployment_fault`, `exec_failed`, `output_overflow`, `killed`), `outcome` (`failed` or `unknown`), `attempts`, `source` and `level` of the notification that failed, and `kind` of the destination. It carries no title, message, link or payload text. The hook environment carries `NOTICE_ROUTE_HOOK`, `NOTICE_ROUTE_ID` and `NOTICE_ROUTE_OBJECT_TYPE`, and the command follows the guide's execution rules (own process group, 30 s default timeout, no token variables).

`send` refuses with exit 20 and reason `hook_recursion` whenever `NOTICE_ROUTE_HOOK` is present in its own environment, so a `post-fail` hook cannot route through the router. The documented way to be alerted about a failing channel is a hook command that calls another notifier directly.

## Calling from automation

Wire a producer's hook to the router with one line, for example `notice-route send --source agent-spawn --stdin-json` on a `post-state-change` hook. The router is the only notification command an agent profile may allow; the notifiers must not be exposed to an agent, and a profile that allows the router must not allow `--config`, `test`, `rules`, `history`, `flush` or `cron`. A hook-run `send` should not be wrapped in a shell pipeline that hides its exit code.

Wiring hazards: wire either agent-spawn's or claude-session's state hooks to notifications, not both (`validate` warns when two rules from those sources point at one destination); a producer that offers an outbox redelivers at least once, which the redelivery check absorbs; pr-watch events are at-most-once and carry only metadata, so a notification can say who and which PR but not what a comment said.

## Config

Keys and defaults; schema in [schemas/config.json](../schemas/config.json). `config.example.yaml` is a complete worked configuration and is validated against the schema in CI.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `destinations` | map | none | See [Destinations](#destinations). |
| `quiet_hours` | map | none | See [Quiet hours](#quiet-hours). |
| `links` | `{hosts: map host -> github or gitlab, tickets: map site -> base URL}` | empty | See [Links](#links). Hosts are lowercase names; ticket base URLs are `https` and end in `/`. |
| `rules` | list | `[]` | See [Rules](#rules). At most 256. |
| `defaults.dedup_window` | duration | `10m` | The default for a rule's `dedup_window`. |
| `lock_wait` | duration | `5s` | See [Calling the notifiers](#calling-the-notifiers). |
| `retention.history` | duration | `30d` | Age at which `prune` deletes history rows. |
| `retention.event_ids` | duration | `14d` | Age at which `prune` forgets event ids. |
| `hooks` | map | none | `post-fail` entries as in design guide section 8. |
| `cron.env_file` | absolute path | none | A mode-0600 file the cron lines source. |
| `log` | boolean | true | Write `log.jsonl`. |

The grammar of durations is the design guide's (a number of seconds or `[0-9]+(\.[0-9]+)?(ms|s|m)?`, plus the `h` and `d` suffixes for the keys that name hours and days: `retention.*`, `dedup_window`; a message that names the key and never the value). A config error names the key and the problem. The config directory, `config.yaml` and every drop-in must be owned by the user and not group- or world-writable (`config_unsafe`, exit 2), because a drop-in can name a command.

`validate` warns, without failing, about: a rule that names an unknown destination (this one is an error), a destination no rule uses, a hook-payload rule without a `level`, a rule unreachable behind an earlier rule, a template field a known producer's payload does not have, a `dedup_window` longer than the destination's cap hour, a destination `timeout` shorter than the notifier's own, a computed `send_worst_case_s` above 30, and the duplicate-wiring hazard.

## Environment variables

None are read for configuration. The router's environment is inside its trust boundary: a caller should not let an agent session set it. The router reads `NOTICE_ROUTE_HOOK` only for the recursion guard and sets `NOTICE_ROUTE_HOOK`, `NOTICE_ROUTE_ID` and `NOTICE_ROUTE_OBJECT_TYPE` for hook commands. It reads the variables named in the destinations' `env` lists only to pass them on, and `PATH`, `LANG`, `LC_ALL`, `WSL_INTEROP` and `WSL_DISTRO_NAME` for the scrubbed child environment.

Config and state are found only from the home directory in the password database; `HOME`, `XDG_CONFIG_HOME` and `XDG_STATE_HOME` are not read (a recorded deviation from design guide section 5, for the same reason as the notifiers: the config selects commands that run with credentials in their environment). A home directory that cannot be resolved, or is relative or empty, is exit 2 `config_invalid` (`details.key` `home`).

## Log

`log.jsonl` under the state directory is public and append-only ([schemas/log.json](../schemas/log.json)): one line per destination outcome and per `flush` summary, with `ts`, `source`, `hook`, `rule`, `destination`, `level`, `outcome`, `reason`, `code`, `attempts`, hashes of the title and the message, `event_id` and the dedupe digest. It never holds title text, message text, links or credentials. It rotates at 10 MiB, keeping 3 files. A state problem warns on stderr and never changes a result. There is no `audit.jsonl`: a notification changes nothing in a system of record (a recorded deviation from design guide section 6, as for the notifiers).

## Files

Under the state directory (`<home>/.local/state/notice-route/`, mode 0700):

| File | Content |
|---|---|
| `state.db` | SQLite, WAL, private, versioned migrations. Tables `deliveries` (one row per notification per destination: the history fields), `queue` (rendered title, message, link, level, `deliver_after` as UTC epoch seconds, `attempts`, `reason`, `claimed_by`, `claim_expires`), `dedup` (destination, key digest, window start, count), `event_ids` (destination, event id, seen at) and `surfaced` (destination, code, day). |
| `locks/<destination>.lock` | The per-destination send lock. |
| `log.jsonl`, `log.1` to `log.3`, `log.lock` | The public log. |

A claim on a queue row is a lease of twice the destination's worst-case call time, written in the claiming transaction; a claim whose lease has run out may be taken by the next `flush`, and a lease is never longer than `now` plus that figure even if the clock has moved. A row is marked delivered after the notifier's call returns, so a process killed between a successful call and the mark delivers that row a second time (at least once). The message text of a delivered or dropped row is deleted with the row.

Configuration: `<home>/.config/notice-route/config.yaml` and `config.d/*.yaml` (lexical order, maps merge, hook lists append), at most 256 drop-ins of at most 256 KiB each. A YAML document uses the strict loader of the other tools (no anchors, aliases or explicit tags, no duplicate keys, `yes`, `on` and dates are strings).

## Labels

notice-route stores no labeled objects. `--label` and a payload's `labels` are inputs to rule matching and to `history` only.

## TUI

None in 0.x. Every action is a command.

## Residual risks

- A prompt-injected agent that can run the router can send a hook-shaped payload with any source and reason. The per-destination rate cap bounds the flood, and the deny-list and the explicit-flag rule keep untrusted text out of titles. It could also reuse a real `event_id` to make the router drop a real event as `redelivered`. Keep the router out of agent profiles.
- `sent` is not delivered: Slack and Home Assistant do not confirm display, and wsl-notice's `sent` means handed to Windows. `unknown` outcomes may have been delivered; a duplicate is possible after a killed `flush`.
- Titles and messages appear on lock screens, in toast history and in the destination; the router never puts a credential in them, and text from a payload only with the explicit opt-in.
- The notifiers' own limits are far below the router's: ha-notice cuts a message at 1000 characters, wsl-notice a title at 100. The router passes the full text and the notifier reports `truncated`.
- Clocks: deadlines use the monotonic clock; quiet windows and `deliver_after` use the wall clock, so a large clock step can defer or deliver early by that step once.
- A destination command and its `env` are trusted configuration; a user who adds a malicious drop-in owns the machine already. The ownership and mode check is the only guard.
- Ordering between destinations is not guaranteed, and a retry can arrive after a later notification.

## Platform notes

Linux and WSL are first-class; macOS is checked locally. The router needs `flock`, `zoneinfo` with the system time-zone database (the `tzdata` package is not needed when the system has `/usr/share/zoneinfo`, and the router refuses a time zone name it cannot load) and `cron` for `flush`. A `wsl-notice` destination exists only on WSL. Under cron, the credentials must be in the env file the cron line sources, because the notifiers read a credential only from their own environment, and a stale `WSL_INTEROP` in a long-lived tmux or cron breaks `wsl-notice` (the router passes the current value and reports `interop_failed`).
