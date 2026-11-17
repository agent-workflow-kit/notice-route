# notice-route: interface

Status: 0.1.0-design, 2026-11-17. Written before implementation (architecture section 18.1). This document is the single normative source for notice-route's rules; `docs/design.md`, the README and the architecture link to its headings and do not restate them.

notice-route routes one notification at a time to channel notifiers (slack-notice, ha-notice, wsl-notice, or any command with the same call shape) by source, labels, level and time. It decides what is sent, to whom, when and how often. It keeps a private database for the deferred and retry queue, the duplicate checks and `history`. It runs one hook, `post-fail`.

References: architecture (`../awkit-docs/architecture.md`) sections 5.2, 10.4, 13.2, 13.3, 15 and 20; design guide (`../awkit-docs/design-guide.md`). The notifiers' interfaces are the contract for the calls this tool makes: [slack-notice](../../slack-notice/docs/interface.md), [ha-notice](../../ha-notice/docs/interface.md) and [wsl-notice](../../wsl-notice/docs/interface.md), each at its heading `Calling from automation`, `Exit codes` and `Error envelope`.

## Concepts

### Input shapes

`send --stdin-json` reads exactly one JSON object from stdin, of one of two shapes, told apart by keys alone. There is no mode flag.

| Shape | Recognized when | Source | Level |
|---|---|---|---|
| Hook payload | the keys `tool` and `hook` are both strings; `hook` must match `[a-z][a-z0-9_-]{0,63}` (else `payload_invalid`, pointer `/hook`) | `payload.tool` | none in the payload; the matching rule sets it |
| Channel notice | the keys are only `title`, `message`, `level` and `link`, and `message` is present | `--source`, else the fixed word `direct` | the notice's own `level`, used only when the rule sets none |

Anything else exits 20 with reason `payload_unrecognized` and nothing is sent. For a hook payload, `--source` may only repeat `payload.tool`; any other value exits 20 with reason `source_mismatch`. A source is 1 to 64 characters of `[a-z0-9][a-z0-9_-]*`, else `payload_invalid` (pointer `/tool` or `--source`). The source `notice-route` is reserved for the router's own failure alerts: `send` refuses it with exit 20 `source_mismatch`. A notice's `link` is validated (see [Links](#links)) and never sent; links come only from a rule.

Input is read from stdin only for `--stdin-json`; `--file <path>` and `-` follow the same rules (`--file -` is stdin). `--file` is opened with `O_NONBLOCK` and checked on the descriptor to be a regular file (a symlink to one is fine), else exit 2. The plain flags `--title` and `--message` build a channel notice for trusted human use; their text is untrusted like any other, and `--source` applies to them. These combinations are exit 2: `--stdin-json` with `--file`, a JSON source with `--title` or `--message`, `--title` without `--message`, no source and no `--message`, and `--stdin-json` or `--file -` with stdin a terminal. Input must be complete within 5 seconds of start, else exit 20 `payload_invalid` (pointer `""`).

The reader is strict. At most 1048576 bytes are read; more, bytes that are not valid UTF-8, a byte-order mark, invalid JSON, a duplicate key at any depth (compared after unescaping), `NaN`, `Infinity`, an integer literal outside the signed 64-bit range or a float that does not fit a double, a nesting depth above 16, or a top level that is not an object exits 20 `payload_invalid` with `details.pointer` `""`. Any parser failure is `payload_invalid`, never `internal`. A lone surrogate inside a string is replaced by U+FFFD. Hook payloads may carry any other keys; the router reads only the scalar fields a rule or template names, plus `event_id`, `repo`, `labels`, `reads_third_party` and `tainted`.

`labels` in a hook payload is an array of `key=value` strings (split at the first `=`); a `labels` that is not an array is ignored and counted once in `labels_ignored`. At most 64 payload labels are read and the rest counted in `labels_ignored`. `--label key=value` (repeatable, at most 32; a 33rd is a usage error) adds labels in both shapes, merged with the payload's own. A label key is `[a-z0-9][a-z0-9_.-]{0,63}` and a value is 0 to 256 characters of `[A-Za-z0-9._:/#@+=,-]`; a malformed payload label is ignored and counted in `labels_ignored`, a malformed `--label` is exit 20 `payload_invalid` with pointer `--label`. The channel notice's own fields follow the notifiers' rules: `title` at most 100000 characters, `message` 1 to 100000 characters, `level` one of `info`, `warn`, `action`, `error`, `link` rules as in [Links](#links). A violation is exit 20 `payload_invalid` with `details.pointer` `/<field>`.

### Rules

`rules` is an ordered list. For each notification the first rule whose `match` holds decides everything: destinations, template, level and options. No later rule is consulted. A rule that matches with `drop: true` ends the notification with outcome `dropped`, reason `rule_drop`; no matching rule ends it with outcome `dropped`, reason `no_rule`, exit 22. Both write one history row without a destination.

A rule has these keys (all validated at load; an unknown key is `config_invalid`):

| Key | Type | Meaning |
|---|---|---|
| `name` | string, `[a-z0-9][a-z0-9_-]{0,63}`, unique | Required. Shown in output and `history`. |
| `match` | object | Required. All present conditions must hold (below). |
| `drop` | boolean | Default false. When true, no other key except `name` and `match` is allowed. |
| `notify` | list of destination names | Required unless `drop`. At least 1, at most 8, each a defined destination, no duplicates. |
| `level` | `info`, `warn`, `action`, `error` | See [Levels](#levels). |
| `title`, `message`, `link` | template strings | `message` is required unless `drop` or the rule is a pass-through rule (below); `title` needs `message`; `link`: see [Links](#links). See [Templates](#templates). |
| `dedup_window` | duration | Default `10m` (the `defaults.dedup_window` key); range 1s to 24h. |
| `include_text` | list of token paths, at most 16 | Opt-in for untrusted text in the message (see [Untrusted text](#untrusted-text)). |

`match` conditions (closed list in 0.x; no regular expressions, no OR, no negation; a second rule covers an OR):

| Condition | Applies to | Matches when |
|---|---|---|
| `source` | both shapes | the source equals the value, or the value ends in one `*` and the source starts with the text before it |
| `labels` | both shapes | a list of `key=value` strings, each of which is among the notification's labels |
| `hook` | hook payloads only | the payload's `hook` equals the value (same `*` rule) |
| `repo` | hook payloads only | the payload's `repo` equals the value (same `*` rule); a `repo` that is null or absent does not match |
| `where` | hook payloads only | a map from payload field path to a string, integer or boolean; every pair equals; an absent field does not match |
| `level_min` | channel notices only | the notice's own level (`info` when it has none) is at least this level; may not also carry `hook` or `where` |

A condition that does not apply to a shape never matches that shape. Values compare as the same JSON type and equal value: a boolean never equals an integer, a string never equals a number. `where` keys are payload field paths (the template path grammar) and always name payload fields. A `*` is allowed only as the last character (`**` or an inner `*` is `config_invalid`); a lone `*` matches any present value. Matching is exact and case-sensitive. Match values are 1 to 256 characters; `where` has 1 to 16 pairs with string values of at most 1024 characters; `labels` has at most 32 entries. A rule with an empty `match` matches everything and is how a catch-all is written. In 0.x a condition cannot test a list of values, a number range, presence or absence: write one rule per value.

Pass-through rules: a rule without `message` is a pass-through rule. It must have `level_min` (so it matches only channel notices; `level_min: info` matches every notice) and sends the notice's own title and message. The title is cut at the first line break, stripped of the characters of the categories Cc, Cf, Cs, Co, Cn, Zl and Zp, its whitespace runs collapsed, and cut to the limit of [Templates](#templates). The message keeps its line breaks (`\r\n` and `\r` become `\n`), is stripped the same way otherwise, and is cut to the same limit. A rule with `message` never reads a notice's title or message. A pass-through rule may carry its own literal `link`.

### Levels

The final level of a notification is the rule's `level` if it has one, else the channel notice's own `level` if present, else `info`. A hook payload has no level, so a rule that has neither `level` nor `level_min` can match hook payloads without setting one; `validate` warns about it. Nothing else can raise a level: there is no `--level` flag and a payload cannot choose it when the rule sets one.

Levels are ordered `info < warn < action < error`. The order matters for destination floors and quiet hours (below); the notifiers do not use it. The level reaches the notifier in the payload, and the notifiers decide how to show it.

### Destinations

A destination is a named channel and the command that reaches it:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `kind` | `slack-notice`, `ha-notice`, `wsl-notice`, `generic` | required | Selects the retry rules (see [Retries and outcomes](#retries-and-outcomes)). `slack-notice` and `wsl-notice` share the retry rules; they differ in the environment (`WSL_*`), the shared lock and `--probe`. |
| `cmd` | list of strings | required | An argv list of at most 32 elements of at most 4096 characters; no shell. `cmd[0]` is an absolute path (load error otherwise). At each call the router resolves it and requires the file and every parent directory to be owned by the user or root and not group- or world-writable, and the file to have no setuid or setgid bit; otherwise the call ends `failed`, code `exec_failed`. `validate` reports the same check as `cmd_ok`. A script's interpreter (`#!`) is trusted configuration. An element equal to `send`, `--stdin-json`, `--json`, `--actor` or `--dedupe-key`, or starting with one of those flags and `=`, is `config_invalid`. The notifier's `--config <path>` belongs here; one config file per destination. |
| `timeout` | duration | `10s` | The notifier's own timeout or more (the router does not pass it; a shorter value kills calls the notifier would still finish). Range 1s to 120s. The kill deadline is this plus 10 s. |
| `floor` | level | `info` | The lowest level delivered here. |
| `quiet` | name of a `quiet_hours` window | none | The window in which this destination is quiet. |
| `max_per_hour` | integer 0 to 3600 | 30 | The hourly cap (see [Rate limit](#rate-limit)); 0 means no cap. |
| `env` | list of variable names | `[]` | The only variables passed to the notifier besides the fixed set (see [Calling the notifiers](#calling-the-notifiers)). |
| `enabled` | boolean | true | When false, the destination is skipped with outcome `disabled`. |

A destination name is `[a-z0-9][a-z0-9_-]{0,31}`. At most 32 destinations. `generic` assumes only the call shape (JSON on stdin) and treats exit 0 as sent and every other exit as a failure that is surfaced and never retried. A third-party notifier works as `generic` if it reads the notification JSON from stdin; otherwise a small wrapper script in front of it does the adaptation. The router ships no adapter code.

### Quiet hours

`quiet_hours` defines named windows. A window name follows the destination name grammar.

| Key | Type | Meaning |
|---|---|---|
| `timezone` | IANA name | Required. 1 to 64 characters matching `[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}`; no default and no reading of the system zone. Loaded only from the system database (`/usr/share/zoneinfo` and the platform's usual paths; `PYTHONTZPATH` is ignored); `localtime`, `posixrules`, `Factory` and any name that fails to load (any exception) are `config_invalid`. |
| `days` | list of `mon` to `sun` | Default every day. The days on which the window starts. |
| `start`, `end` | `HH:MM` | Required, 24-hour local time, minutes only. `end` earlier than `start` means the window ends the next day; `end` equal to `start` is `config_invalid`. |

A moment is inside a window when its local wall-clock minute of the day is in `[start, end)` on a day that started in the window, computed from the local time of day and weekday and never by building boundary timestamps. The next end of a window is the first whole minute after now that is outside it, found by stepping one minute at a time (at most 2 days), stored as UTC epoch seconds.

Within a destination's quiet window, per level: `info` is dropped (`dropped`, reason `quiet`), `warn` and `action` are deferred, `error` is delivered at once. Quiet applies per destination, so one notification can be delivered to one destination and deferred for another. There is no per-rule override.

### Deferral and digest

A deferred notification is stored rendered (title, message, link, level) in the `queue` with `deliver_after` set to the end of the quiet window, to the start of the next UTC hour for `rate_capped`, or to now for `env_missing`. `flush` delivers the due rows of a destination. It checks quiet at its own time: while the destination is quiet it delivers only rows of level `error`, and a quiet-deferred row that is not delivered gets the window's new end as `deliver_after`.

Due deferred rows of one destination are folded into one digest: title `N held notifications`, one message line per row `<level> <source>: <title>` (the stored title, or the rule name when the row has no title), oldest first, at most 10 lines, then a line `and K more`; the highest level among them; the row's link only when there is exactly one row; no `--dedupe-key`. A destination with one due row gets that row as itself. A digest's result is applied to each of its rows as if the row had been sent alone, except that `queued_retry` leaves the rows deferred with `deliver_after` from the retry schedule of their highest attempt count and adds one attempt to each. Retry rows are never folded: each is sent as itself.

A destination keeps at most 100 queue rows, deferred and retry together. When it already has 100, the row dropped (outcome `dropped`, reason `overflow`) is the oldest of the lowest level among those rows and the new one; a claimed row is never dropped. A row whose age since creation is 24 hours or more when due is dropped with outcome `expired`.

### Dedup

Two checks run per destination, both inside the same database transaction as the quiet and rate decisions.

The **redelivery check**: a payload `event_id` already recorded for this destination, source and hook is dropped with outcome `suppressed`, reason `redelivered`, and not counted. The id is compared as its JSON text: the integer 17 and the string "17" differ. An `event_id` that is not an integer (a boolean is not) or a string of 1 to 256 characters is ignored. Event ids are kept for `retention.event_ids`.

The **repeat check**: the key is the SHA-256 of the UTF-8 text of the source, the hook (empty for a channel notice), the final level, the rendered title, the rendered message and the rendered link (each empty when absent), joined with NUL bytes, after the cuts of [Templates](#templates). Within a fixed window that starts at the first notification with this key (length from the rule's `dedup_window`), the first notification is sent and later ones are `suppressed`, reason `repeat`, and counted. The window is not extended by later repeats.

Both records exist only while the router is responsible for the delivery: they are written for a destination whose notification is sent now, deferred or queued, and never for one that ended `dropped`, `disabled` or `suppressed`. When that delivery ends `failed`, its event-id record and the repeat window it opened are deleted, so a redelivery or a repeat is judged afresh. An `unknown` outcome keeps both.

The hex digest of the key is passed to every destination as `--dedupe-key=<hex>` (the `=` form) and is `dedupe` in `send --json`. The level is part of the key, so a later `warn` cannot replace an earlier `error` in the tag of a notifier that uses one.

### Rate limit

Each destination has counters per UTC clock hour of notifier calls started: a send-now decision, a `flush` call, a digest and a retry each count one, whatever their result. The counter is kept separately for `error` and for the other levels, and each class has its own `max_per_hour`. A counter is read and incremented in the transaction that decides the call, so parallel senders cannot pass it together. When a counter has reached `max_per_hour`, `send` defers the notification (`deferred`, reason `rate_capped`, `deliver_after` the start of the next UTC hour) and `flush` leaves a due row in the queue until then; a retry row loses no attempt. Rows deferred for the cap are folded into the next digest like quiet rows. `max_per_hour: 0` means no cap; to silence a destination, set `enabled: false`. The first `rate_capped` deferral per destination per UTC hour is surfaced (code `rate_capped`, see [Hooks](#hooks)).

### Retries and outcomes

The notifier's exit code and, for exit 12, its error envelope decide the outcome of one call:

| Notifier result | Outcome | Router action |
|---|---|---|
| exit 0 | `sent` | Record. "Sent" never means delivered or displayed. Stdout that does not parse leaves `notifier_outcome` null. |
| exit 22 | `disabled` | Successful no-op, recorded, not an alert. |
| exit 12, `retryable` false, `unknown_outcome` false | `failed` | Never retried; surfaced. |
| exit 12, `retryable` true, `unknown_outcome` false | `queued_retry` | Retried by `flush`. |
| exit 12, `retryable` false, `unknown_outcome` true | `unknown` | Never retried; recorded as possibly sent. |
| exit 12, both true | `ha-notice`: `queued_retry`; any other kind: `unknown` | Retried by `flush` within the cap for `ha-notice` (its tag absorbs a duplicate). |
| exit 20, reason `link_invalid` or `link_scheme` | `queued_retry` | Queued as a retry due now without `link` and the URL appended to the message (reason `link_refused`); the first such retry does not count as an attempt, and a second refusal is `failed`. |
| exit 20, reason `command_keyword` (ha-notice only) | `queued_retry` | Queued as a retry due now with the message prefixed by its source and a colon (reason `keyword_refused`); the first such retry does not count as an attempt, and a second refusal is `failed`. |
| exit 20, any other reason | `failed` | Always `failed`, never retried; surfaced with the notifier's `reason` as the code. An unparseable envelope gives reason null. |
| exit 1 with an envelope whose `unknown_outcome` is true, or exit 1 with no envelope | `unknown` | Never retried. |
| exit 1 with `unknown_outcome` false, or exit 2 | `failed`, code `deployment_fault` | Surfaced. |
| ended by a signal the router did not send (exit 128+N or a signal status) | `queued_retry`, code `signal` | Retried by `flush`. |
| any other exit (10, 11, 13, 21, ...) | `failed`, code `deployment_fault` | Surfaced. |
| killed by the router's deadline, output over the cap, an envelope that does not parse | `unknown` | Never retried. |
| the command cannot be started | `failed`, code `exec_failed` | Never retried. |
| any exit other than 0 from a `generic` destination | `failed` | Overrides every row above except "cannot be started", killed and output over the cap. |

A retry row is retried 1 minute, 5 minutes, 30 minutes and 2 hours after the end of its previous call, at most 5 attempts in total (the first call counts, except the first link or keyword retry). After the last, the row ends as `failed`, code `retries_exhausted`. A retry row is not sent while its destination is quiet unless it is `error`; it waits for the end of the window. `unavailable` and `interop_failed` are retryable by the notifiers' flags and follow the same schedule. A call that could not take the destination's lock within `lock_wait` is a retryable result with reason `lock_busy` and follows the same schedule, counting as an attempt.

### Untrusted text

Payload values are untrusted. A token renders a payload value by these rules. An integer (a boolean is not one), a boolean, or a string matching `[A-Za-z0-9][A-Za-z0-9._/#@+=-]{0,63}` is plain and renders in a title or a message (links follow [Links](#links)); an integer renders as its digits, a boolean as `true` or `false`, another number as its shortest JSON text. Any other string is text: in a title it renders empty; in a message it renders only when the rule lists the token's path in `include_text` and the payload has `reads_third_party: false` and `tainted: false` as booleans, and it is then cut at the first line break, stripped of the characters of the categories Cc, Cf, Cs, Co, Cn, Zl and Zp, its whitespace runs collapsed, cut at 200 characters and wrapped in double quotes with any inner double quote replaced by a single quote. Text that renders empty marks the notification `text_excluded` and is listed in `test` as `excluded`. Derived fields are built by the router and always render. An `include_text` entry that no message token uses has no effect.

A pass-through rule sends the sender's own text; see [Calling from automation](#calling-from-automation) for who may be allowed `send`.

### Templates

A template is a string of 1 to 2000 characters with `{{name}}` tokens. `name` is `labels.` followed by a label key (`[a-z0-9][a-z0-9_.-]{0,63}`), which renders the lexically first value of that label, or any other field path `[A-Za-z_][A-Za-z0-9_]{0,63}` with up to three further `.`-separated parts of the same form. A token has no whitespace (`{{ x }}` is a load error); a `}}` outside a token is literal text; a template cannot contain a literal `{{` that is not a token. Lengths in this document count Unicode code points. Substitution is a single pass over the template text: each token is replaced by its value, and the replaced text is never scanned again, so a value that contains `{{x}}` stays literal. A token that is not closed or has another syntax is a load error. There is no default value, no conditional and no loop. Rendering has no errors.

An absent field, a null, a list or an object renders empty and is reported in `test` as `missing`; other values render as in [Untrusted text](#untrusted-text). Derived fields computed by the router are `source`, `hook`, `level`, `pr_url` and `ticket_url` (see [Links](#links)); the bare names always mean the derived fields, so a payload field of the same name is not reachable from a template.

The rendered title is cut to 256 characters and the rendered message to 4000. If the rendered message is empty after sanitizing, the router uses `<source> <hook>` (or the source alone for a channel notice) so the call never refuses with `empty_message`. The notifiers cut to their own limits and report `truncated`.

### Links

A link is built only from the router's own `links` tables and fixed payload ids, never from a payload field or a notice's `link`.

| Derived field | Computed from | Result |
|---|---|---|
| `pr_url` | the payload's `pr`, matching `<host>[:<port>]/<segment>(/<segment>)*#<n>` (host lowercase labels `[a-z0-9]([a-z0-9-]*[a-z0-9])?` joined by `.`, port 1 to 5 digits, segment `[A-Za-z0-9._-]+` and not `.` or `..`, n `[1-9][0-9]{0,9}`), whose `<host>[:<port>]` is a key of `links.hosts` | `https://<host>[:<port>]/<path>/pull/<n>` when the host is `github`, `https://<host>[:<port>]/<path>/-/merge_requests/<n>` when it is `gitlab` |
| `ticket_url` | the payload's `site`, a key of `links.tickets`, and `key` matching `[A-Za-z][A-Za-z0-9_]*-[0-9]{1,9}` | `links.tickets.<site>` followed by the key |

Anything else renders the field empty. A rule's `link` is exactly `{{pr_url}}`, exactly `{{ticket_url}}`, or a literal `https` URL of 1 to 1024 printable ASCII characters with no token (`config_invalid` otherwise); a link that renders empty means no link and the notice is still sent. A channel notice's own `link` must be 1 to 1024 printable ASCII characters beginning with lowercase `http://` or `https://`. The tables hold only host names and base URLs of the user's own systems.

### Calling the notifiers

For each destination to be sent now the router makes one call. The call is `cmd... send --stdin-json --json --actor auto:notice-route [--dedupe-key=<hex>]`, with the notification on stdin as a JSON object of only the present fields (`title`, `message`, `level`, `link`; absent fields are omitted, never null) and no other flag, so a destination's `cmd` may not contain `send` or any of those flags (`config_invalid`).

- No shell. The child gets its own session and process group, a closed descriptor set beyond stdin, stdout and stderr, an empty working directory (`/`), and a scrubbed environment: `PATH` (the router's own with empty, relative and writable-by-others entries removed), `LANG` and `LC_ALL` (the router's own when they name a UTF-8 locale, else `C.UTF-8`), for a `wsl-notice` destination also `WSL_INTEROP` and `WSL_DISTRO_NAME` when set, and the variables named in the destination's `env`. A name in `env` must match `[A-Za-z_][A-Za-z0-9_]{0,127}` and may not name a variable of the fixed set (`PATH`, `LANG`, `LC_ALL`, `WSL_INTEROP`, `WSL_DISTRO_NAME`) (`config_invalid`). The names are trusted configuration: the router passes only its own value of each. A named variable that is unset is not passed, and `validate` reports it. When one is unset in `send`'s environment, `send` does not call the notifier: it defers the notification (see [`send`](#send)). `flush` never defers for `env_missing`: it calls, and an unset credential fails in the notifier.
- Sends to one destination are serialized across processes by an exclusive `flock` on `locks/<destination>.lock` in the state directory, taken by polling without blocking until `lock_wait` (default 2s, range 1s to 60s) has passed. Every `wsl-notice` destination uses one lock, `locks/wsl-notice.lock`, because parallel PowerShell starts fail with `interop_failed`. The kernel releases the lock when its holder dies, and the router releases it when the call returns, before the result is recorded. A call that cannot take it in time ends as `lock_busy` (see [Retries and outcomes](#retries-and-outcomes)). Sends to different destinations run in parallel.
- The call has one deadline on the monotonic clock: the destination's `timeout` plus 10 seconds, counted from the process start. At the deadline the router sends SIGTERM to the process group it created, waits 5 seconds, then SIGKILL to that group. After the child exits or the group is killed, the router reads for at most 1 second more, closes the pipes and decides from the exit code and what was read; a descendant that left the process group and holds a pipe cannot hold the router. A killed call is `unknown`. The router never signals anything else and never kills by name or pattern.
- Output is read through a bounded reader: at most 65536 bytes of stdout and 16384 bytes of stderr; more than that ends the call as `unknown`, code `output_overflow`. Stdout is decoded strictly as UTF-8 and parsed with the same strict JSON reader as input (duplicate keys, `NaN` and out-of-range numbers rejected) and must be one object; anything else is an unparseable envelope. Stderr is never parsed or stored. A write to a closed stdin (EPIPE) is ignored, and the exit code decides.
- For exit 12 the router reads `error.code`, `error.retryable`, `error.unknown_outcome` and, for refusals, `error.reason`; every flag must be a boolean or the whole envelope is unparseable. A `retryable: true` for a code the notifier documents as non-retryable is believed (the flags decide).
- The router never calls `validate --test-message`, passes no payload text in argv or environment, and never passes `--config` other than what the destination's `cmd` states.

A `send` makes at most one call per destination. Counted from the end of its input, it ends no later than `lock_wait` plus the largest destination `timeout` plus 17 seconds (10 s to the kill deadline, 5 s TERM grace, 1 s final read, 1 s for the database). `validate` prints this figure as `send_worst_case_s` and warns when it exceeds 30, the default hook timeout.

## Commands

### `send`

`notice-route send [--stdin-json | --file <path|->] [--title <t>] [--message <m>] [--source <name>] [--label <k=v>]... [--dry-run]`

Routes one notification. In order:

1. Parse flags, read and validate the input (exit 2 or 20 on error), discriminate the shape.
2. Load and validate the config (exit 2). A config with `enabled: false` is exit 22 reason `disabled` before any state is read. A missing config file or no destinations is exit 22 reason `unconfigured`: it writes no state and prints one line on stderr.
3. Select the first matching rule (exit 22 reason `no_rule` if none; a `drop` rule ends here with outcome `dropped`; both write the history row of [Rules](#rules)).
4. Render the title, message and link, and compute the final level.
5. For each selected destination, in one `BEGIN IMMEDIATE` transaction, decide in this order: disabled (`disabled`); below the destination's `floor` (`dropped`, reason `below_floor`); `info` in the destination's quiet window (`dropped`, reason `quiet`); the redelivery check; the repeat check; `warn` or `action` in quiet (`deferred`); a variable named in the destination's `env` unset in this process's environment (`deferred`, reason `env_missing`, `deliver_after` now); the rate limit (`deferred`, reason `rate_capped`); otherwise send now. The same transaction writes the `deliveries` row of every destination (see [Files](#files)) and, for each destination to be sent now, a `queue` row of kind `retry` holding the rendered notification, claimed by this process (`claimed_by` a random token, `claim_expires` now plus twice `send_worst_case_s`).
6. Call the notifiers for the destinations to send now, in parallel, as described in [Calling the notifiers](#calling-the-notifiers). When a call returns, one transaction records its outcome in the `deliveries` row and deletes the queue row, or, for `queued_retry`, clears its claim and sets its `deliver_after`. Then surface failures (see [Hooks](#hooks)). A `send` that receives SIGTERM, SIGINT or SIGHUP before its calls start sets `claim_expires` of its rows to now and exits 128+N.

`--dry-run` is `test` with the same input: it opens `state.db` read-only if it exists (as empty otherwise), takes no lock, writes nothing and prints the `test` output. Plain `--title` and `--message` are for trusted human use; their values are untrusted text like stdin.

### `rules`

`notice-route rules [--json]`: lists the rules in order with their name, match, destinations, level, `drop` and pass-through flags and dedup window, plus the destinations and quiet windows they reference. Reads only the config. A config error prints the error envelope with `details.key`.

### `test`

`notice-route test [--stdin-json | --file <path|->] [--title <t>] [--message <m>] [--source <name>] [--label <k=v>]... [--json]`: takes the input of `send` (default stdin) and prints what `send` would do. It opens `state.db` read-only if it exists (empty otherwise), takes no lock, writes nothing and never calls a notifier. Output: see [Other outputs](#other-outputs). Exit 0 when a rule matched (a drop rule included), 22 for `no_rule`, `disabled` or `unconfigured`, 20 and 2 as `send`.

### `flush`

`notice-route flush [--destination <name>]... [--dry-run] [--json]`: delivers due rows. For each destination (or those named), in parallel: takes the destination's flush lock without waiting (another `flush` holding it: `already_running`), then delivers the due rows one call at a time, taking the send lock per call as `send` does, building the digest, calling the notifier like `send`, and recording the result. A row whose claim has not expired is not due. A disabled destination is skipped (state `disabled`); its rows wait and expire. `--dry-run` lists what is due. Exit 12 when a call ended `failed` or `unknown`; otherwise 22 when no destination had a due row; otherwise 0 (`already_running` and `quiet` included). An unknown `--destination` is exit 2.

### `history`

`notice-route history [--since <duration|time>] [--source <s>] [--destination <d>] [--level <l>] [--outcome <o>] [--label <k=v>]... [--limit <n>] [--json]`: lists history rows newest first, default limit 100. `--since` is a duration (`[0-9]+(s|m|h|d)`) or an RFC 3339 UTC time; each filter may be given once, except `--label` (all must match, exact `key=value`); `--limit` above 1000 is exit 2; ties order by `id` descending. Filters AND together. The `--json` output is a list validated by `schemas/cli/history.json`. A row holds the rendered title of a templated rule (cut to 100 characters) but never message text.

Outcomes in `history` are the eight of `send` plus `sending` (a row whose call is in progress or whose process died; never in `send` output) and `expired`. Reasons are closed: `below_floor`, `quiet`, `rate_capped`, `overflow`, `lock_busy`, `env_missing`, `rule_drop`, `no_rule`, `digest`, `link_refused`, `keyword_refused`; `send` output and the log also have `redelivered` and `repeat`.

### `validate`

`notice-route validate [--probe] [--json]`: checks the config offline: loads and validates every key, checks that each destination's `cmd[0]` passes the check of [Destinations](#destinations) (`cmd_ok`), that each `env` name is set in the router's own environment (reported, not an error), that quiet windows and links are well formed, reports the worst-case send time and the codes surfaced in the current UTC day for each destination (read from `state.db` read-only), and warns about the conditions listed under [Config](#config). With `--probe` it also runs `<cmd> validate --json` (plus `--probe` for `ha-notice` and `wsl-notice`) for each enabled destination of the three known kinds; `generic` destinations are never probed. It never runs `--test-message`, never prompts and never sends. A probe reports the notifier's `enabled` value and treats its exit 22 as information. Exit 0 when every check passed (warnings allowed), 2 when the config is invalid (the error envelope is printed), 12 when a `cmd_ok` or probe check failed (the full report is printed, outcome `failed`), 22 when there is no config.

### `prune`

`notice-route prune [--dry-run]`: deletes history older than `retention.history`, event ids older than `retention.event_ids`, expired dedup rows, surfaced rows older than 2 days, counters of past hours older than 2 days, and every queue row whose age is 24 hours or more, whatever its destination (outcome `expired`). Beyond that it keeps at most 100000 `deliveries` rows and 100000 `event_ids` rows per destination (rows without a destination count as one), deleting the oldest. It never touches a row with an unexpired claim. Exit 0.

### `cron install` and `cron uninstall`

`notice-route cron install|uninstall [--dry-run]`: writes or removes one tagged block in the user's crontab, in the shape every awkit tool uses: a first line `# notice-route managed v1 flush=1m`, a `PATH=` line (`/usr/local/bin:/usr/bin:/bin` plus the router's directory), a `* * * * *` line running `flush --quiet` and a line at 03:17 daily running `prune --quiet`, each as `. '<env_file>' && '<absolute notice-route>' ...` when `cron.env_file` is set, and a last line `# notice-route managed end`. The executable is the resolved absolute path of the running program. Every interpolated value must match `[A-Za-z0-9_./+-]+` or `cron install` refuses (exit 2). The block is found by the prefix `# notice-route managed` of its first line, replaced idempotently, and a block without the end line is refused. Credentials for the notifiers must be in the env file, because the router passes only the variables a destination names and a `send` without them defers to `flush`.

## Global flags

`--json`, `--config <path>`, `--verbose`, `--quiet`, `--version` where meaningful. `--config <path>` replaces `config.yaml` (drop-ins beside it still apply) and is accepted only by `rules`, `test` and `validate` without `--probe`, which run no command and write nothing; any other command, `send --dry-run` included, refuses it with exit 2 `usage`. `--quiet` suppresses the advisory stderr lines; `--verbose` adds one line per decision. `--dry-run` is accepted by `send`, `flush`, `prune` and `cron`. Abbreviated flags are refused. `--actor` and `--decision-id` are accepted by `send` for audit symmetry with the other tools and are recorded in the log's `actor` and `decision_id`; the call to the notifier always uses `auto:notice-route`. There is no `--yes`: nothing here asks for confirmation.

## Output

With `--json`, `send` prints one object ([schemas/cli/send.json](../schemas/cli/send.json)), also on exit 22:

- `outcome`: `ok` (exit 0), `partial` (exit 12) or `no_op` (exit 22).
- `shape`: `hook` or `notice`. `source`, `hook` (null for a notice), `rule` (the matched rule's name or null), `level` (null when no rule decided it), `reason` (null, `unconfigured`, `no_rule`, `all_disabled`, `disabled` or `rule_drop`), `text_excluded` (boolean), `labels_ignored` (integer).
- `destinations`: a list with one entry per selected destination (empty for `rule_drop`, `no_rule`, `unconfigured` and `disabled`): `name`, `outcome` (`sent`, `deferred`, `queued_retry`, `suppressed`, `dropped`, `disabled`, `failed`, `unknown`), `reason` (a closed string or null, see [`history`](#history)), `attempts`, `code` (a notifier code of pattern `[a-z][a-z0-9_]{0,63}`, untrusted, or a router code from [Hooks](#hooks)), `count` (the repeat count for `suppressed`), `deliver_after` (UTC epoch seconds, set for `deferred` and `queued_retry`, else null), `notifier_outcome` (the notifier's own `outcome` of pattern `[a-z_]{1,32}`, untrusted, for exit 0 or 22, or null).
- `dedupe` (the hex digest used, or null), `duration_ms`.

Exit 20 and exit 2 print the error envelope: `{"error": {"code", "message", "reason", "details"}}`, with `code` one of `usage`, `config_invalid`, `config_unsafe`, `internal`, `refused`, and a fixed `message` (never input text). `details.key` names the key for config errors (a key path, `home`, `--config`, or the file path relative to the config directory for `config_unsafe` and unreadable files); `details.pointer` is set for refusals of `payload_invalid` (`""`, a JSON pointer such as `/title`, or `--source`, `--label`). Without `--json`, stdout is empty and one line per destination goes to stderr; stderr is advisory.

### Other outputs

The other commands' `--json` outputs have schemas under `schemas/cli/`.

- `test` (also `send --dry-run`): `shape`, `source`, `hook`, `rule`, `reason` (as the `reason` of `send`), `title`, `message`, `link`, `missing` (token paths that rendered empty), `excluded` (token paths of text that was left out), `text_excluded`, `level`, `dedupe`, `destinations[]` with `name`, `decision` (`send`, `defer`, `drop`, `suppress`, `disabled`), `reason`, `deliver_after`.
- `flush`: `outcome` (`ok`, `partial`, `no_op`), `dry_run`, `destinations[]` with `name`, `state` (`done`, `already_running`, `nothing_due`, `quiet`, `disabled`), `due_retry`, `due_deferred`, `sent`, `queued_retry`, `failed`, `unknown`, `dropped`, `expired`, `digests`; `duration_ms`.
- `rules`: `rules[]` with `name`, `match`, `destinations`, `level`, `drop`, `pass_through`, `dedup_window_s`; `destinations[]`; `quiet_hours[]`.
- `validate`: `outcome` (`ok` or `failed`), `probe`, `destinations[]` with `name`, `cmd_ok`, `env_unset`, `probe`, `surfaced_today`; `warnings[]`; `send_worst_case_s`.
- `prune`: `dry_run`, `deleted` (a count per table).
- `cron install`: `action` (`installed`, `updated` or `unchanged`), `block`, `dry_run`, `env_file`. `cron uninstall`: `action` (`removed` or `unchanged`), `block`, `dry_run`.

## Exit codes

Reserved codes 0-21 follow design guide section 4.2 (`../awkit-docs/design-guide.md`).

| Exit | Meaning |
|---|---|
| 0 | The router took responsibility for every destination: each ended `sent`, `deferred`, `queued_retry`, `suppressed`, `dropped` or `disabled`. |
| 1 | `internal`. |
| 2 | `usage`, or `config_invalid`, or `config_unsafe` (the config directory, file or a drop-in is not owned by the user or is group- or world-writable). |
| 12 | At least one destination ended `failed` or `unknown` in this call (or, for `validate`, a check failed). The other destinations were still delivered. `partial` in the output. |
| 20 | Refused, nothing sent. Reasons: `payload_unrecognized`, `source_mismatch`, `payload_invalid`, `hook_recursion`. |
| 22 | Nothing to do: no config or no destination (`unconfigured`), no rule matched (`no_rule`), every selected destination disabled (`all_disabled`) or the router disabled (`enabled: false`). A caller treats it as a successful no-op. |
| 128+N | A signal N before any notifier call started. This process sent nothing; destinations it had already decided are in the queue and the next `flush` delivers them. |

Never used: 10, 11, 13, 21. A signal after the first notifier call started is handled as the deadline: the router finishes the calls it started within their own deadlines and then exits with the result.

## Hooks

One hook, `post-fail`, configured under `hooks` in the router's config as in design guide section 8. An entry has exactly `cmd`, `timeout` and `async`.

| Hook | When it fires | Can veto |
|---|---|---|
| `post-fail` | After a destination's failure was surfaced: at most once per code and destination per UTC day, except `rate_capped`, at most once per destination per UTC hour. Async, best effort, at most once (no outbox in 0.x; a hook that fails is logged and the `history` row shows `surfaced`, which marks the row whose failure was the one surfaced). | no |

The payload ([schemas/hooks/post-fail.json](../schemas/hooks/post-fail.json)) has `tool`, `hook`, `object_type` (`destination`), `object_id` (the destination name), `event_id` (the failing `deliveries` row id), `labels` (the failing notification's labels), `code` (a notifier error code, a notifier refusal reason, or one of `retries_exhausted`, `rate_capped`, `deployment_fault`, `exec_failed`, `output_overflow`, `killed`, `signal`, `expired`, `overflow`), `outcome` (`failed`, `unknown`, `deferred` for `rate_capped`, `dropped` for `overflow`, `expired`), `attempts`, `source` and `level` of the notification that failed, and `kind` of the destination. It carries no title, message, link or payload text. The hook environment carries `NOTICE_ROUTE_HOOK`, `NOTICE_ROUTE_ID` and `NOTICE_ROUTE_OBJECT_TYPE`; the command runs as in design guide section 8 (`sh -c`, own process group, timeout 30 s by default) and inherits the router's environment minus every variable named in any destination's `env`. It must not start a long-lived process, which would inherit `NOTICE_ROUTE_HOOK` (the recursion guard).

Failure alerts: when a failure is surfaced, the router also routes the `post-fail` payload through its rules as a hook payload of source `notice-route` and hook `post-fail`, like any notification, except that every destination that would be sent now is queued as a deferred row due now and delivered by the next `flush`. An alert whose own delivery fails is recorded and surfaced but routes no further alert. `send` refuses an input whose source is `notice-route` (exit 20, `source_mismatch`).

`send` refuses with exit 20 and reason `hook_recursion` whenever `NOTICE_ROUTE_HOOK` is present in its own environment. Any process started from a `post-fail` hook, including a producer whose own hook calls `send`, is refused with `hook_recursion`.

## Calling from automation

Wire a producer's hook to the router with one line, for example `notice-route send --source agent-spawn --stdin-json` on a `post-escalate` hook. A permission profile that lets a session notify allows exactly the command prefix `notice-route send`: no flag of `send` selects a config or a command. Allow it only to kinds that do not read third-party content, because a sender chooses the payload (and the text of a pass-through rule). Never allow a notifier. A hook-run `send` should not be wrapped in a shell pipeline that hides its exit code. There is no per-invocation `--hook` flag.

Wiring hazards: wire either agent-spawn's or claude-session's state hooks to notifications, not both; a producer that offers an outbox redelivers at least once, which the redelivery check absorbs; pr-watch events are at-most-once and carry only metadata, so a notification can say who and which PR but not what a comment said.

## Config

Keys and defaults; schema in [schemas/config.json](../schemas/config.json). `config.example.yaml` is a complete worked configuration and `examples/awkit-wiring.yaml` is a drop-in with rules for the awkit producers; both are validated against the schema in CI and rendered against the producers' hook examples.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | true | When false, `send` exits 22 `disabled` before reading state and `flush` does nothing. |
| `destinations` | map | none | See [Destinations](#destinations). |
| `quiet_hours` | map | none | See [Quiet hours](#quiet-hours). |
| `links` | `{hosts: map host -> github or gitlab, tickets: map site -> base URL}` | empty | See [Links](#links). Hosts are `<host>[:<port>]` in the `pr` grammar; ticket sites match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` and their base URLs are `https` and end in `/`. |
| `rules` | list | `[]` | See [Rules](#rules). At most 256. |
| `defaults.dedup_window` | duration | `10m` | The default for a rule's `dedup_window`. |
| `lock_wait` | duration | `2s` | See [Calling the notifiers](#calling-the-notifiers). |
| `retention.history` | duration | `30d` | Age at which `prune` deletes history rows; at least 1d. |
| `retention.event_ids` | duration | `14d` | Age at which `prune` forgets event ids; at least 1d. |
| `hooks` | map | none | `post-fail` entries as in design guide section 8. |
| `cron.env_file` | absolute path | none | A mode-0600 file the cron lines source. |
| `log` | boolean | true | Write `log.jsonl`. |

The grammar of durations is the design guide's (a number of seconds or `[0-9]+(\.[0-9]+)?(ms|s|m)?`, plus the `h` and `d` suffixes for the keys that name hours and days: `retention.*`, `dedup_window`; a message that names the key and never the value). A config error names the key and the problem. Ranges of durations and cross-references (rule names, destinations, windows, time zones) are checked by the loader, not by the schema. The config directory, `config.yaml` and every drop-in must be owned by the user and not group- or world-writable (`config_unsafe`, exit 2), because a drop-in can name a command. A rule naming an unknown destination is a load error.

`validate` warns, without failing, about: a destination no rule uses; a rule without `level` and without `level_min`; a rule unreachable because an earlier rule's conditions are each also in it with the same value, or with a `*` value that is a prefix of its value (`labels` and `where` as subsets); a computed `send_worst_case_s` above 30; and no `# notice-route managed` block in the user's crontab. It knows no producer: the example is checked in CI, and `test` shows `missing` and `excluded` for any real payload.

## Environment variables

None are read for configuration. The router's environment is inside its trust boundary: a caller should not let an agent session set it. The router reads `NOTICE_ROUTE_HOOK` only for the recursion guard and sets `NOTICE_ROUTE_HOOK`, `NOTICE_ROUTE_ID` and `NOTICE_ROUTE_OBJECT_TYPE` for hook commands. It reads the variables named in the destinations' `env` lists only to pass them on, and `PATH`, `LANG`, `LC_ALL`, `WSL_INTEROP` and `WSL_DISTRO_NAME` for the scrubbed child environment.

Config and state are found only from the home directory in the password database; `HOME`, `XDG_CONFIG_HOME` and `XDG_STATE_HOME` are not read (a recorded deviation from design guide section 5, for the same reason as the notifiers: the config selects commands that run with credentials in their environment). A home directory that cannot be resolved, or is relative or empty, is exit 2 `config_invalid` (`details.key` `home`).

## Log

`log.jsonl` under the state directory is public and append-only ([schemas/log.json](../schemas/log.json)): one line per destination outcome (`record` `delivery`; a `drop` rule and `no_rule` write one without a destination) and per `flush` summary (`record` `flush`, with totals), with `ts`, `tool`, `version`, `actor`, `decision_id`, `source`, `hook`, `rule`, `destination`, `level`, `outcome`, `reason`, `code`, `attempts`, the hashes of the title and the message (the first 16 hex of the SHA-256 of the rendered UTF-8 text), `event_id` and the dedupe digest. It never holds title text, message text, links or credentials. It rotates at 10 MiB, keeping three rotated files (`log.1` to `log.3`). It is a record of recent activity, not an audit trail: a flood rotates older lines away. A problem with the log warns on stderr and never changes a result. There is no `audit.jsonl`: a notification changes nothing in a system of record (a recorded deviation from design guide section 6, as for the notifiers).

## Files

Under the state directory (`<home>/.local/state/notice-route/`):

| File | Content |
|---|---|
| `state.db` | SQLite, WAL, private, versioned migrations. `deliveries`: `id`, `ts` (decision time, UTC epoch seconds), `source`, `hook`, `object_id`, `rule`, `destination` (null for `rule_drop` and `no_rule`), `level`, `outcome`, `reason`, `code`, `attempts`, `count`, `event_id` (its JSON text), `dedupe`, `labels`, `surfaced`, `deliver_after`, `title` (the rendered title cut to 100 characters; null for a pass-through row), `title_sha256`, `message_sha256`. `queue`: `id`, `delivery_id`, `kind` (`deferred` or `retry`), the rendered `title`, `message`, `link` and `level`, `source`, `rule`, `dedupe`, `deliver_after`, `created_at`, `claimed_by`, `claim_expires`. `dedup`: destination, key digest, window start, `delivery_id`. `event_ids`: destination, source, hook, event id, seen at. `surfaced`: destination, code, period (see [Hooks](#hooks)). `rate`: destination, UTC hour, class (`error` or other), count. |
| `locks/<destination>.lock` | The per-destination send lock; `locks/wsl-notice.lock` is shared by every `wsl-notice` destination. |
| `locks/<destination>.flush` | The per-destination flush lock. |
| `log.jsonl`, `log.1` to `log.3`, `log.lock` | The public log. |

The router creates the state directory with mode 0700 under `umask 077` and refuses (exit 1) one that is not a directory it owns with no group or world access. It opens every file relative to it with `O_NOFOLLOW` and `O_NONBLOCK` and checks `fstat` for a regular file: a bad `log.jsonl` is skipped with a warning, a bad lock file makes the call `lock_busy`, a bad `state.db` is exit 1. A database that cannot be used (busy for 5 seconds, corrupt, a newer schema, read-only) is exit 1 `internal` with nothing sent. Every value reaches SQL as a bound parameter.

A `deliveries` row is updated in place by every later call for it. A repeat suppressed by the repeat check and a redelivery write no row; a repeat adds 1 to `count` of the row that opened its window. A digest gives each of its rows the digest's outcome and reason `digest`.

Only rows written by `send` carry a claim. A row whose claim has expired is due, and the next `flush` sends it as a retry (one attempt). A notification is therefore delivered at least once: a process killed after its call reached the notifier and before the result was recorded causes one duplicate; a process killed before the call loses nothing. A lease is never longer than `now` plus twice `send_worst_case_s`, even if the clock has moved. The message text of a row is deleted with the row.

Configuration: `<home>/.config/notice-route/config.yaml`, then `config.d/*.yaml` in lexical order: `rules` and `hooks.post-fail` lists append in that order; a rule name used twice, or any other key defined in two files, is `config_invalid`. Each file is opened with `O_NOFOLLOW` and checked on the descriptor (owned by the user, not group- or world-writable, a regular file); a drop-in that is a symlink is refused; the 256 KiB limit applies to the bytes read. At most 256 drop-ins. A YAML document uses the strict loader of the other tools (no anchors, aliases or explicit tags, no duplicate keys, `yes`, `on` and dates are strings).

## Labels

notice-route stores no labeled objects. `--label` and a payload's `labels` are inputs to rule matching and to `history` only.

## TUI

None in 0.x. Every action is a command.

## Residual risks

- A session allowed `send` can forge a hook-shaped payload with any source, hook, labels, `repo`, `pr`, `site` and `key` (none of these is an authorization), and can choose the taint flags. The cap converts a flood into delayed or dropped real notifications, not only into a bound. It can send first a notification that renders like an expected real one, which is then `suppressed` for the rule's window, and reuse a real `event_id` of the same source and hook. The plain-value rule and the opt-in keep free text out of titles; a forger can still choose a word of up to 64 plain characters, and the flags that gate opted-in text are the payload's own claims (they protect against an honest producer, not a forger). A rule without `level` lets a notice choose its own level, `error` included: set `level` on rules that match senders you do not trust. Allow `send` only as Calling from automation says.
- `sent` is not delivered: Slack and Home Assistant do not confirm display, and wsl-notice's `sent` means handed to Windows. `unknown` outcomes may have been delivered; a duplicate is possible after a `send` or `flush` killed during a call.
- Titles and messages appear on lock screens, in toast history and in the destination; the router never puts a credential in them, and free text from a payload only with the explicit opt-in or through a pass-through rule.
- The notifiers' own limits are far below the router's: ha-notice cuts a message at 1000 characters, wsl-notice a title at 100. The router passes the full text and the notifier reports `truncated`.
- Clocks: deadlines use the monotonic clock; quiet windows and `deliver_after` use the wall clock, so a large step moves dedup windows, UTC-hour cap buckets and `deliver_after` by the step, once.
- A destination command and its `env` are trusted configuration; a user who adds a malicious drop-in owns the machine already. The ownership and mode check is the only guard.
- Ordering between destinations is not guaranteed, and a retry can arrive after a later notification.

## Platform notes

Linux and WSL are first-class; macOS is checked locally. The router needs `flock`, `zoneinfo` with the system time-zone database (the `tzdata` package is not needed when the system has `/usr/share/zoneinfo`, and the router refuses a time zone name it cannot load) and `cron` for `flush`. A `wsl-notice` destination exists only on WSL. Under cron, the credentials must be in the env file the cron line sources, because the notifiers read a credential only from their own environment; a `send` without the credentials (an agent session, a producer's cron) defers to `flush`, which runs with the env file. A stale `WSL_INTEROP` in a long-lived tmux or cron breaks `wsl-notice` (the router passes the current value and reports `interop_failed`).
