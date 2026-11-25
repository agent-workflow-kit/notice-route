# notice-route: interface

Status: 0.1.0-design, 2026-11-17. Written before implementation (architecture section 18.1). This document is the single normative source for notice-route's rules; `docs/design.md`, the README and the architecture link to its headings and do not restate them.

notice-route routes one notification at a time to channel notifiers (slack-notice, ha-notice, wsl-notice, or any command with the same call shape) by source, labels, level and time. It decides what is sent, to whom, when and how often. It keeps a private database for the queue of held and single rows, the duplicate checks and `history`. It runs one hook, `post-fail`, from `flush`.

References: [architecture](../../awkit-docs/architecture.md) sections 5.2, 10.4, 13.2, 13.3, 15 and 20; [design guide](../../awkit-docs/design-guide.md). The notifiers' interfaces are the contract for the calls this tool makes: [slack-notice](../../slack-notice/docs/interface.md), [ha-notice](../../ha-notice/docs/interface.md) and [wsl-notice](../../wsl-notice/docs/interface.md), each at its heading `Calling from automation`, `Exit codes` and `Error envelope`.

## Concepts

### Input shapes

`send --stdin-json` reads exactly one JSON object from stdin, of one of two shapes, told apart by keys alone. There is no mode flag.

| Shape | Recognized when | Source | Level |
|---|---|---|---|
| Hook payload | the keys `tool` and `hook` are both strings; `hook` must match `[a-z][a-z0-9_-]{0,63}` (else `payload_invalid`, pointer `/hook`) | `payload.tool` | none in the payload; the matching rule sets it |
| Channel notice | the keys are only `title`, `message`, `level` and `link`, and `message` is present | `--source`, else the fixed word `direct` | the notice's own `level`, used only when the rule sets none |

Anything else exits 20 with reason `payload_unrecognized` and nothing is sent. For a hook payload, `--source` may only repeat `payload.tool`; any other value exits 20 with reason `source_mismatch`. A source is 1 to 64 characters of `[a-z0-9][a-z0-9_-]*`, else `payload_invalid` (pointer `/tool` or `--source`). The source `notice-route` is reserved for the router's own failure alerts: `send` refuses it with exit 20 `source_mismatch`. A notice's `link` is validated (see [Links](#links)) and never sent; links come only from a rule.

Input is read from stdin only for `--stdin-json`; `--file <path>` and `-` follow the same rules (`--file -` is stdin). `--file` is opened with `O_NONBLOCK` and checked on the descriptor to be a regular file (a symlink to one is fine), else exit 2. The plain flags `--title` and `--message` build a channel notice for trusted human use; their text is untrusted like any other, and `--source` applies to them. These combinations are exit 2: `--stdin-json` with `--file`, a JSON source with `--title` or `--message`, `--title` without `--message`, no source and no `--message`, and `--stdin-json` or `--file -` with stdin a terminal. Input read from stdin (`--stdin-json`, `--file -`) must be complete within 5 seconds of the start of the command, else exit 20 `payload_invalid` (pointer `""`); a regular file has no such deadline.

The reader is strict. At most 1048576 bytes are read; more, bytes that are not valid UTF-8, a byte-order mark, invalid JSON, a duplicate key at any depth (compared after unescaping), `NaN`, `Infinity`, an integer literal outside the signed 64-bit range or a float that does not fit a double, a nesting depth above 16 (the top-level object is depth 1), or a top level that is not an object exits 20 `payload_invalid` with `details.pointer` `""`. Any parser failure is `payload_invalid`, never `internal`. A lone surrogate inside a string is replaced by U+FFFD. Hook payloads may carry any other keys; the router reads only the scalar fields a rule or template names, plus `event_id`, `object_id`, `repo`, `labels`, `reads_third_party` and `tainted`. `object_id` is stored only when it is an integer or plain ([Untrusted text](#untrusted-text)), else null.

`labels` in a hook payload is an array of `key=value` strings (split at the first `=`); a `labels` that is not an array is ignored and counted once in `labels_ignored`. The first 64 elements are examined; each one that is not a string or not a valid label, and each element beyond 64, adds 1 to `labels_ignored`. A label pair given twice counts once. `--label key=value` (repeatable, at most 32; a 33rd is a usage error) adds labels in both shapes, merged with the payload's own. A label key is `[a-z0-9][a-z0-9_.-]{0,63}` and a value is 0 to 256 characters of `[A-Za-z0-9._:/#@+=,-]`; a malformed `--label` is exit 20 `payload_invalid` with pointer `--label`. The channel notice's own fields follow the notifiers' rules: `title` at most 100000 characters, `message` 1 to 100000 characters, `level` one of `info`, `warn`, `action`, `error`, `link` rules as in [Links](#links). A violation is exit 20 `payload_invalid` with `details.pointer` `/<field>`.

### Rules

`rules` is an ordered list. For each notification the first rule whose `match` holds decides everything: destinations, template, level and options. No later rule is consulted. A rule that matches with `drop: true` ends the notification with outcome `dropped`, reason `rule_drop`; no matching rule ends it with outcome `dropped`, reason `no_rule`, exit 22. Both write one history row without a destination.

A rule has these keys (all validated at load; an unknown key is `config_invalid`):

| Key | Type | Meaning |
|---|---|---|
| `name` | string, `[a-z0-9][a-z0-9_-]{0,63}`, unique | Required. Shown in output and `history`. |
| `match` | object | Required. All present conditions must hold (below). |
| `drop` | boolean | Default false. When true, no other key except `name` and `match` is allowed. |
| `notify` | list of destination names | Required unless `drop`. At least 1, at most 8, each a defined destination, no duplicates. |
| `level` | `info`, `warn`, `action`, `error` | Required unless `drop` or `match.level_min`. See [Levels](#levels). |
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
| `level_min` | channel notices only | the notice's own level (`info` when it has none) is at least this level; may not also carry `hook`, `repo` or `where` |

A condition that does not apply to a shape never matches that shape. Values compare as the same JSON type and equal value: a boolean never equals an integer, a string never equals a number, and an integer is a number written without a fraction or exponent, so `1.0` never equals `1`. `where` keys are payload field paths (the template path grammar) and always name payload fields. A `*` is allowed only as the last character (`**` or an inner `*` is `config_invalid`); a lone `*` matches any present value. Matching is exact and case-sensitive. Match values are 1 to 256 characters; `where` has 1 to 16 pairs with string values of at most 1024 characters; `labels` has at most 32 entries. A rule with an empty `match` matches everything and is how a catch-all is written. In 0.x a condition cannot test a list of values, a number range, presence or absence: write one rule per value.

Pass-through rules: a rule without `message` is a pass-through rule. It must have `level_min` (so it matches only channel notices; `level_min: info` matches every notice) and sends the notice's own title and message. The notice's title and message are the rendered title and message ([Templates](#templates) sanitizes and cuts them). A rule with `message` never reads a notice's title or message. A pass-through rule may carry its own literal `link`.

### Levels

The final level of a notification is the rule's `level` if it has one, else the channel notice's own `level` if present, else `info`. A hook payload has no level, so a rule without `match.level_min` (which can match hook payloads) must set `level` (`config_invalid` otherwise; a `drop` rule needs none). Nothing else can raise a level: there is no `--level` flag and a payload cannot choose it when the rule sets one.

Levels are ordered `info < warn < action < error`. The order matters for destination floors and quiet hours (below); the notifiers do not use it. The level reaches the notifier in the payload, and the notifiers decide how to show it.

### Destinations

A destination is a named channel and the command that reaches it:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `kind` | `slack-notice`, `ha-notice`, `wsl-notice`, `generic` | required | Selects the retry rules (see [Retries and outcomes](#retries-and-outcomes)). `slack-notice` and `wsl-notice` share the retry rules; they differ in the environment (`WSL_*`), the shared lock and `--probe`. |
| `cmd` | list of strings | required | An argv list of at most 32 elements of at most 4096 characters; no shell. `cmd[0]` is an absolute path (load error otherwise). At each call the router resolves it with `realpath` (following symlinks), requires the resolved file and every directory of the resolved path to be owned by the user or root and not group- or world-writable, and the file to have no setuid or setgid bit; otherwise the call ends `failed`, code `exec_failed`. The resolved path is executed. `validate` reports the same check as `cmd_ok`. A script's interpreter (`#!`) is trusted configuration. An element equal to `send`, `--stdin-json`, `--json`, `--actor` or `--dedupe-key`, or starting with one of those flags and `=`, is `config_invalid`. The notifier's `--config <path>` belongs here; one config file per destination. A script that wraps the notifier must `exec` it, so that a signal reaches the router as a signal and not as an exit code 128+N. |
| `timeout` | duration | `10s` | The notifier's own timeout or more (the router does not pass it; a shorter value kills calls the notifier would still finish). Range 1s to 120s. The kill deadline is this plus 10 s. |
| `floor` | level | `info` | The lowest level delivered here. |
| `quiet` | name of a `quiet_hours` window | none | The window in which this destination is quiet. |
| `max_per_hour` | integer 0 to 3600 | 30 | The hourly cap (see [Rate limit](#rate-limit)); 0 means no cap. |
| `env` | list of variable names | `[]` | The only variables passed to the notifier besides the fixed set (see [Calling the notifiers](#calling-the-notifiers)); at most 4096 entries. |
| `enabled` | boolean | true | When false, the destination is skipped with outcome `disabled`. |

A destination name is `[a-z0-9][a-z0-9_-]{0,31}`. At most 32 destinations. `generic` assumes only the call shape: the command accepts `send --stdin-json --json --actor <actor> [--dedupe-key=<hex>]` and reads the notification JSON on stdin; it treats exit 0 as sent and every other exit, 22 included, as `failed` (code `generic_failed`), surfaced and never retried. A third-party notifier works as `generic` if it accepts those words and reads the notification JSON from stdin; otherwise a small wrapper script accepts those words, ignores what it does not need and does the adaptation. The router ships no adapter code.

### Quiet hours

`quiet_hours` defines named windows. A window name follows the destination name grammar.

| Key | Type | Meaning |
|---|---|---|
| `timezone` | IANA name | Required. 1 to 64 characters matching `[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}`; no default and no reading of the system zone. Loaded only from the system database (`/usr/share/zoneinfo` and the platform's usual paths; `PYTHONTZPATH` is ignored); `localtime`, `posixrules`, `Factory` and any name that fails to load (any exception) are `config_invalid`. |
| `days` | list of `mon` to `sun` | Default every day. The days on which the window starts. |
| `start`, `end` | `HH:MM` | Required, 24-hour local time, minutes only. `end` earlier than `start` means the window ends the next day; `end` equal to `start` is `config_invalid`. |

A moment is inside a window, judged from its local weekday and minute of the day and never by building boundary timestamps, when: for `start` before `end`, its weekday is in `days` and its minute is in `[start, end)`; for `start` after `end`, either its weekday is in `days` and its minute is at least `start`, or the previous weekday is in `days` and its minute is before `end`. The next end of a window is the first whole minute after now that is outside it, found by stepping one minute at a time (at most 2 days), stored as UTC epoch seconds. Instants are UTC epoch seconds (finite, floored); the next end is a multiple of 60 epoch seconds strictly after the instant; it can be absent (`None`) only if no minute in the next 2 days is outside, which a valid window never produces, and a caller treats absence as an internal error rather than skipping the check.

Within a destination's quiet window, per level: `info` is dropped (`dropped`, reason `quiet`), `warn` and `action` are deferred, `error` is delivered at once. Quiet applies per destination, so one notification can be delivered to one destination and deferred for another. There is no per-rule override.

### Deferral and digest

A queue row is `held` or `single`. A `held` row is a notification deferred for a quiet window (`deliver_after` the window's end) or for the cap (`deliver_after` the start of the next UTC hour); due `held` rows are folded into a digest. Every other row is `single` and is sent as itself: a send-now row (`deliver_after` now), a retry, a refused-link or keyword retry, an `env_missing` row (`deliver_after` now) and a failure alert. A row is stored rendered (title, message, link, level) and is due when it is unclaimed and its `deliver_after` has passed. [`flush`](#flush) delivers due rows.

Due `held` rows of one destination are folded into one digest: title `N held notifications`, where N is the number of rows folded; one message line per row `<level> <source>: <head>`, where `<head>` is the stored title (or the rule name when the row has none), ` - ` and the first line of the stored message, cut together to 120 characters; lines ordered by level from highest, then oldest first; at most 10 lines, then a line `and K more`; the highest level among the rows; the row's link only when there is exactly one row; no `--dedupe-key`. A destination with one due `held` row gets that row as itself. A digest claims all its rows and counts in the rate class of its highest level. A digest's result is applied to each of its rows as if the row had been sent alone, except that `queued_retry` leaves the rows `held` with `deliver_after` from the retry schedule of their highest attempt count and adds one attempt to each. A digest that ends `failed` or `unknown` is surfaced once, for its first row (the highest level, oldest), whose source, labels and level fill the payload. `single` rows are never folded.

A destination keeps at most 100 unclaimed queue rows, `held` and `single` together; claimed rows are not counted. A claim whose lease has lapsed (expired, missing claimant or expiry, or an expiry beyond now plus the lease length, for instance after a backward clock step) counts as unclaimed for `flush` and for the cap, and may be evicted; its result, if sent, then goes to the log only. Oldest means lowest row id. Every transaction that leaves a row unclaimed (a row queued for later, a call recorded as `queued_retry`, or the release of claims by a signal in `send` or `flush`) applies the cap: while the destination has more than 100 unclaimed rows, the row dropped is the oldest of the lowest level among them, a row just queued included (outcome `dropped`, reason `overflow`, code `overflow`).

### Dedup

Two checks run per destination, both inside the same database transaction as the quiet and rate decisions.

The **redelivery check**: a payload whose `event_id` and repeat digest (the `dedupe` of the same notification, below) were both recorded for this destination, source and hook is dropped with outcome `suppressed`, reason `redelivered`, and not counted. The id is compared as its JSON text (`json.dumps` of the value): the integer 17 and the string "17" differ. An `event_id` that is not an integer (a boolean is not) or a plain string ([Untrusted text](#untrusted-text)) is ignored. A producer's real redelivery renders identically, so it matches; a forged future id does not. The check is not applied to the router's own failure alerts (source `notice-route`). Event ids are kept for `retention.event_ids`.

The **repeat check**: the key is the SHA-256 of the UTF-8 text of the source, the hook (empty for a channel notice), the final level, the rendered title, the rendered message and the rendered link (each empty when absent), joined with NUL bytes, after the cuts of [Templates](#templates). Within a fixed window that starts at the first notification with this key, the half-open interval `[first, first + dedup_window)` in epoch seconds with the length of the rule that opened it (stored with the window and never changed), the first notification is sent and later ones are `suppressed`, reason `repeat`, and counted. The window is not extended by later repeats.

Both records belong to one delivery: they are written, with its `delivery_id`, for a destination that ends in decision (6) to (9) of [`send`](#send), and they are kept while that delivery is pending and after it ends `sent` or `unknown`. Any other end (`failed`, `dropped`, `expired`, or `disabled` by the notifier's own exit 22) deletes the records with its `delivery_id`, so a redelivery or a repeat is judged afresh.

The hex digest of the key is passed to every destination as `--dedupe-key=<hex>` (the `=` form) and is `dedupe` in `send --json`. The level is part of the key, so a later `warn` cannot replace an earlier `error` in the tag of a notifier that uses one. The repeat check applies to `--title` and `--message` input too: a second identical `send --message hi` within the window is `suppressed`; use `test` or `--dry-run` to try a rule.

### Rate limit

Each destination has counters per UTC clock hour, one for `error` and one for the other levels, each capped by `max_per_hour`. The transaction that claims a row for a call increments the counter of the row's class (a digest: the class of its highest level), so parallel senders cannot pass it together; a call never made because the lock was busy gives its count back, and a row resent after its claim lapsed counts again. When the class counter has reached `max_per_hour`, `send` defers the notification (a `held` row, `deferred`, reason `rate_capped`, `deliver_after` the start of the next UTC hour) and `flush` leaves a due `single` row until then (a retry loses no attempt). A digest is never held by the cap; it still counts. `max_per_hour: 0` means no cap; to silence a destination, set `enabled: false`. The first `rate_capped` deferral per destination per UTC hour is surfaced (code `rate_capped`, see [Hooks](#hooks)); a deferral that the queue cap drops at once (`overflow`) is not surfaced and does not use the hour's one surfacing.

### Retries and outcomes

The first row that applies decides the outcome of one call. `failed` and `unknown` are surfaced ([Hooks](#hooks)); `queued_retry` is not.

| # | Result of the call | Outcome | `code` |
|---|---|---|---|
| 1 | the command cannot be started (including the `cmd[0]` check) | `failed` | `exec_failed` |
| 2 | killed at the router's deadline | `unknown` | `killed` |
| 3 | stdout or stderr over its cap (the router kills the group at once) | `unknown` | `output_overflow` |
| 4 | ended by a signal (no exit status) | `unknown` | `signal` |
| 5 | `generic`: exit 0 | `sent` | null |
| 6 | `generic`: any other exit | `failed` | `generic_failed` |
| 7 | exit 0 | `sent` (stdout that does not parse leaves `notifier_outcome` null) | null |
| 8 | exit 22 | `disabled` (a successful no-op; `notifier_outcome` from stdout) | null |
| 9 | exit 12 or 20 or 1 whose stdout is not a parseable envelope | exit 20: `failed`; exit 12 and 1: `unknown` | `envelope_invalid` |
| 10 | exit 12, `retryable` false, `unknown_outcome` false | `failed` | `error.code` |
| 11 | exit 12, `retryable` true, `unknown_outcome` false | `queued_retry` | `error.code` |
| 12 | exit 12, `unknown_outcome` true | `ha-notice` with `retryable` true and a `--dedupe-key`: `queued_retry`; otherwise `unknown` | `error.code` |
| 13 | exit 20, reason `link_invalid` or `link_scheme` | `queued_retry`, reason `link_refused` (see below) | `error.reason` |
| 14 | exit 20, reason `command_keyword`, kind `ha-notice` | `queued_retry`, reason `keyword_refused` (see below) | `error.reason` |
| 15 | exit 20, any other reason | `failed` | `error.reason` |
| 16 | exit 1, `unknown_outcome` false | `failed` | `deployment_fault` |
| 17 | exit 1, `unknown_outcome` true | `unknown` | `error.code` |
| 18 | exit 128+N (129 to 159) | with empty stdout `queued_retry` (the notifier stopped before sending); otherwise `unknown` | `signal` |
| 19 | any other exit (2, 10, 11, 13, 21, ...) | `failed` | `deployment_fault` |

"Sent" never means delivered or displayed. A `queued_retry` row is a `single` row. For `link_refused` the queued row has no `link` and the message `<message>`, a line break and the URL; for `keyword_refused` the message is `<source>: <message>`; the result is cut to 4000 characters. Each counts as an attempt and goes through the normal cap and quiet checks. A row whose reason is already `link_refused` or `keyword_refused` that is refused again, for either reason, ends `failed`, and so does a link refusal of a row that has no link (nothing can cure it); both end with the refusal reason as `code`, even on the last attempt. A refusal retry follows the retry schedule below like any other retry.

A retry row is retried 1 minute, 5 minutes, 30 minutes and 2 hours after the end of its previous call, at most 5 attempts in total (3 for `wsl-notice`; the first call counts). After the last, the row ends as `failed`, code `retries_exhausted`. `unavailable` and `interop_failed` are retryable by the notifiers' flags and follow the same schedule. A call not made because the destination's lock was not free within `lock_wait` (any kind) is `queued_retry`, reason `lock_busy`, code null; it counts no attempt, gives its hourly count back and is due again 60 seconds later. After a call to a destination ends `queued_retry`, `failed` or `unknown`, `flush` makes no further call to that destination in the same run; its other due rows keep their `deliver_after` and the next run calls them.

### Untrusted text

Payload values are untrusted. A token renders a payload value by these rules. A number (finite; a boolean is not one), a boolean, or a string matching `[A-Za-z0-9][A-Za-z0-9._/#-]{0,255}` is plain and renders in a title or a message (links follow [Links](#links)); an integer (a number written without a fraction or exponent) renders as its digits, another number as its shortest round-trip text, a boolean as `true` or `false`. A label value is plain by the same rule. Any other string is text: in a title it renders empty; in a message it renders only when the rule lists the token's path in `include_text` and the payload has `reads_third_party: false` and `tainted: false` as booleans, and it is then `sanitize(value, false)` ([Templates](#templates)), cut at 200 characters, stripped of trailing spaces and wrapped in double quotes with any inner double quote replaced by a single quote. Text that renders empty marks the notification `text_excluded` and is listed in `test` as `excluded`. Derived fields are built by the router and always render. An `include_text` entry that no message token uses has no effect.

A pass-through rule sends the sender's own text; see [Calling from automation](#calling-from-automation) for who may be allowed `send`.

### Templates

A template is a string of 1 to 2000 characters with `{{name}}` tokens. `name` is `labels.` followed by a label key (`[a-z0-9][a-z0-9_.-]{0,63}`), which renders the lexically first value of that label when it is plain (else empty, listed in `excluded`), or any other field path `[A-Za-z_][A-Za-z0-9_]{0,63}` with up to three further `.`-separated parts of the same form. A token has no whitespace (`{{ x }}` is a load error); a `}}` outside a token is literal text; a template cannot contain a literal `{{` that is not a token. Lengths in this document count Unicode code points. Substitution is a single pass over the template text: each token is replaced by its value, and the replaced text is never scanned again, so a value that contains `{{x}}` stays literal. A token that is not closed or has another syntax is a load error. There is no default value, no conditional and no loop. Rendering has no errors.

Sanitizing: `sanitize(text, lines)` turns `\r\n` and `\r` into `\n`; when `lines` is false it cuts the text at its first `\n`; it replaces every character of the categories Cc (tab, and `\n` when `lines` is false), Zl, Zp and Zs by a space; it deletes every character of Cs and Co, every Cf except U+200D ZERO WIDTH JOINER, and U+034F, U+115F, U+1160, U+17B4, U+17B5, U+180B to U+180D, U+2800, U+3164, U+FE00 to U+FE0F, U+FFA0, U+FFFC and U+E0100 to U+E01EF; it turns each run of spaces into one space and strips each line. Categories are those of the running Python's `unicodedata`. The rendered title is `sanitize(title, false)` cut to 256 characters; the rendered message is `sanitize(message, true)` cut to 4000. After each cut (title, message and the 200-character text value below) trailing spaces are removed, so a cut never leaves a trailing space (for the text value, before its quotes are added).

An absent field, a null, a list or an object renders empty and is reported in `test` as `missing`; other values render as in [Untrusted text](#untrusted-text). Derived fields computed by the router are `source`, `hook`, `level`, `pr_url` and `ticket_url` (see [Links](#links)); the bare names always mean the derived fields, so a payload field of the same name is not reachable from a template. For a channel notice `hook`, `pr_url` and `ticket_url` render empty and are not reported as `missing`; other tokens except `labels.` render empty and are reported as `missing`.

If the rendered message is empty (it has no character other than newline, space and U+200D), the router uses `<source> <hook>` (or the source alone for a channel notice) so the call never refuses with `empty_message`. The notifiers cut to their own limits and report `truncated`.

### Links

A link is built only from the router's own `links` tables and fixed payload ids, never from a payload field or a notice's `link`.

| Derived field | Computed from | Result |
|---|---|---|
| `pr_url` | the payload's `pr`, matching `<host>[:<port>]/<segment>(/<segment>)*#<n>` (host lowercase labels `[a-z0-9]([a-z0-9-]*[a-z0-9])?` joined by `.`, port 1 to 5 digits, segment `[A-Za-z0-9._-]+` and not `.` or `..`, n `[1-9][0-9]{0,9}`), whose `<host>[:<port>]` is a key of `links.hosts` | `https://<host>[:<port>]/<path>/pull/<n>` when the host is `github`, `https://<host>[:<port>]/<path>/-/merge_requests/<n>` when it is `gitlab` |
| `ticket_url` | the payload's `site`, a key of `links.tickets`, and `key` matching `[A-Za-z][A-Za-z0-9_]*-[0-9]{1,9}` | `links.tickets.<site>` followed by the key |

Anything else renders the field empty. A rule's `link` is exactly `{{pr_url}}`, exactly `{{ticket_url}}`, or a literal URL `https://<host>[:<port>][/<path>]` of at most 1024 characters (host and port as in `pr_url`, port 1 to 65535, path characters `[A-Za-z0-9._~/%?=&+-]`; `config_invalid` otherwise); a link that renders empty means no link and the notice is still sent. A channel notice's own `link` must be 1 to 1024 printable ASCII characters, no space, beginning with lowercase `http://` or `https://`. The tables hold only host names and base URLs of the user's own systems.

### Calling the notifiers

For each destination to be sent now the router makes one call. The call is `cmd... send --stdin-json --json --actor auto:notice-route [--dedupe-key=<hex>]`, with the notification on stdin as a JSON object of only the present fields (`title`, `message`, `level`, `link`; absent fields are omitted, never null) and no other flag, so a destination's `cmd` may not contain `send` or any of those flags (`config_invalid`). A notification whose message is empty, or that holds a lone surrogate in `title`, `message` or `link`, is never sent: the call ends `not_started` internally (the router's rendering guarantees neither occurs).

- No shell. The child gets its own session and process group, a closed descriptor set beyond stdin, stdout and stderr, an empty working directory (`/`), and a scrubbed environment: `PATH` (the router's own with empty, relative, missing, non-directory, not-owned-by-the-user-or-root and writable-by-others entries removed, judging only the entry itself after symlinks and never its ancestors; `/usr/bin:/bin` when none remains), `LANG` and `LC_ALL` (the router's own when they name a UTF-8 locale, else `C.UTF-8`), for a `wsl-notice` destination also `WSL_INTEROP` and `WSL_DISTRO_NAME` when set, and the variables named in the destination's `env`. A name in `env` must match `[A-Za-z_][A-Za-z0-9_]{0,127}` and may not name a variable of the fixed set (`PATH`, `LANG`, `LC_ALL`, `WSL_INTEROP`, `WSL_DISTRO_NAME`) (`config_invalid`). The names are trusted configuration: the router passes only its own value of each. A named variable that is unset or set to the empty string counts as unset and is not passed. Neither `send` nor `flush` calls a destination while one of its named variables is unset: `send` queues the notification as a `single` row due now (`deferred`, reason `env_missing`); `flush` leaves the destination's rows untouched (state `env_missing`, no attempt counted) and surfaces code `env_missing`. `validate` lists unset names in `env_unset`.
- Sends to one destination are serialized across processes by an exclusive `flock` on `locks/<destination>.lock` in the state directory, taken by polling every 50 ms without blocking until `lock_wait` (default 2s, range 1s to 60s) has passed. Every `wsl-notice` destination uses one lock, `locks/_wsl.lock` (no destination name starts with `_`), because parallel PowerShell starts fail with `interop_failed`; two `wsl-notice` destinations of one notification are therefore sent one after the other, the second waiting up to `lock_wait`. The kernel releases the lock when its holder dies, and the router releases it when the call returns, before the result is recorded. A call that cannot take it in time is `lock_busy` (see [Retries and outcomes](#retries-and-outcomes)). Sends to different destinations run in parallel.
- The call has one deadline on the monotonic clock: the destination's `timeout` plus 10 seconds, counted from the process start; writing stdin is part of the call. At the deadline the router sends SIGTERM to the process group it created, waits 5 seconds, then SIGKILL to that group. When the child (the group leader) exits, the router sends SIGKILL to the group it created, so a same-group descendant is killed and its later output is dropped; after the child exits or the group is killed, the router reads for at most 1 second more, closes the pipes and decides from the exit code and what was read; a descendant that left the process group and holds a pipe cannot hold the router. A killed call is `unknown`. The router never signals anything else and never kills by name or pattern.
- Output is read through a bounded reader: at most 65536 bytes of stdout and 16384 bytes of stderr; more than that makes the router kill the call's process group at once; the outcome is `unknown`, code `output_overflow`. Stdout is decoded strictly as UTF-8 and parsed with the same strict JSON reader as input (duplicate keys, `NaN` and out-of-range numbers rejected) and must be one object; anything else is an unparseable envelope. Stderr is never parsed or stored. A write to a closed stdin (EPIPE) is ignored, and the exit code decides.
- For exit 1, 12 and 20 the router reads `error.code`, `error.retryable`, `error.unknown_outcome` and, for refusals, `error.reason`; on exit 1 and 12 `retryable` and `unknown_outcome` must both be booleans, or the whole envelope is unparseable (on exit 20 they are ignored); an `error.code` that does not match `[a-z][a-z0-9_]{0,63}` makes the envelope unparseable on exit 1 too, so it is `unknown`, code `envelope_invalid`, even when `unknown_outcome` is false. A `retryable: true` for a code the notifier documents as non-retryable is believed (the flags decide).
- The router never calls `validate --test-message`, passes no payload text in argv or environment, and never passes `--config` other than what the destination's `cmd` states.

A `send` makes at most one call per destination. Counted from the end of its input, it ends no later than `lock_wait` plus the largest destination `timeout` plus 17 seconds (10 s to the kill deadline, 5 s TERM grace, 1 s final read, and 1 s for the database: `send` has two transactions, each with a 0.5 s busy timeout). The figure excludes reading the input (at most 5 s, [Input shapes](#input-shapes)), which the caller controls. `validate` prints this figure as `send_worst_case_s` and warns when it exceeds 30, the default hook timeout.

## Commands

### `send`

`notice-route send [--stdin-json | --file <path|->] [--title <t>] [--message <m>] [--source <name>] [--label <k=v>]... [--dry-run]`

Routes one notification. In order:

1. Parse flags and check their combinations (exit 2).
2. Refuse with exit 20 `hook_recursion` when `NOTICE_ROUTE_HOOK` is set, before reading anything.
3. Read the input and validate it in the order `/tool`, `/hook`, `--source`, `source_mismatch`, the notice's fields, `--label` (exit 20 on the first fault), and discriminate the shape.
4. Load and validate the config (exit 2). A config with `enabled: false` is exit 22 reason `disabled` before any state is read. A missing config file or no destinations is exit 22 reason `unconfigured`: it writes no state and prints one line on stderr.
5. Select the first matching rule (exit 22 reason `no_rule` if none; a `drop` rule ends here with outcome `dropped`; both write the history row of [Rules](#rules)).
6. Render the title, message and link, and compute the final level.
7. For each selected destination, in one `BEGIN IMMEDIATE` transaction, the first of these that applies decides: (1) disabled: `disabled`; (2) below the destination's `floor`: `dropped`, reason `below_floor`; (3) `info` in the destination's quiet window: `dropped`, reason `quiet`; (4) the redelivery check: `suppressed`, reason `redelivered`; (5) the repeat check: `suppressed`, reason `repeat`; (6) `warn` or `action` in quiet: a `held` row, `deferred`, reason `quiet`; (7) a variable named in the destination's `env` unset in this process's environment: a `single` row due now, `deferred`, reason `env_missing`; (8) the class counter at `max_per_hour`: a `held` row, `deferred`, reason `rate_capped`; (9) otherwise a `single` row due now, claimed by this process (`claimed_by` a random token, `claim_expires` now plus twice `send_worst_case_s`), and the counter incremented (send now). A row queued unclaimed in (6) to (8) then meets the cap of [Deferral and digest](#deferral-and-digest) and may end `dropped`, reason `overflow`. The transaction writes a `deliveries` row for every destination except a suppressed one (a repeat adds 1 to `count` of the row that opened its window), and the records of [Dedup](#dedup) for every destination that ends in (6) to (9) and was not dropped. When it queues a row for later and either no `flush` has recorded a start in the last 5 minutes or the last full `flush` skipped a destination as `env_missing` (`flush_env_missing`), `send` prints one advisory line on stderr (`--quiet` suppresses it). When every selected destination is disabled the result is exit 22 reason `all_disabled`.
8. Call the notifiers for the destinations to send now, in parallel, as described in [Calling the notifiers](#calling-the-notifiers). When a call returns, one transaction records its outcome in the `deliveries` row and deletes the queue row, or, for `queued_retry`, clears its claim and sets its `deliver_after`; it changes only rows still claimed by this process, and when none is, the result goes to the log only. The same transaction records a surfaced failure ([Hooks](#hooks)); `send` runs no hook. A `send` that receives SIGTERM, SIGINT or SIGHUP before its calls start sets `claim_expires` of its rows to now and exits 128+N.

`--dry-run` is `test` with the same input: it opens `state.db` read-only if it exists (as empty otherwise), takes no lock, writes nothing and prints the `test` output. When a `-wal` file exists, a read-only open may create or touch the SQLite shared-memory index `state.db-shm`; `state.db` and `state.db-wal` are never changed. Plain `--title` and `--message` are for trusted human use; their values are untrusted text like stdin.

### `rules`

`notice-route rules [--json]`: lists the rules in order with their name, match, destinations, level, `drop` and pass-through flags and dedup window, plus the destinations and quiet windows they reference. Reads only the config. A config error prints the error envelope with `details.key`. With no config it prints empty lists and exits 22.

### `test`

`notice-route test [--stdin-json | --file <path|->] [--title <t>] [--message <m>] [--source <name>] [--label <k=v>]... [--json]`: takes the input of `send` (default stdin) and prints what `send` would do. It opens `state.db` read-only if it exists (empty otherwise), takes no lock, writes nothing and never calls a notifier. When a `-wal` file exists, a read-only open may create or touch the SQLite shared-memory index `state.db-shm`; `state.db` and `state.db-wal` are never changed. The open comes after the `disabled` and `unconfigured` checks and before the rule is matched, so a `state.db` that cannot be used is exit 1 `internal` whatever the notification would decide (a `no_rule`, a `drop` rule, all destinations disabled included). Output: see [Other outputs](#other-outputs). It checks its input in the order of `send` steps 1 to 4. Exit 0 when a rule matched (a drop rule included), 22 for `no_rule`, `all_disabled`, `disabled` or `unconfigured`, 20 and 2 as `send`.

### `flush`

`notice-route flush [--destination <name>]... [--dry-run] [--json] [--actor <a>] [--decision-id <id>]`: delivers due rows ([Deferral and digest](#deferral-and-digest)). A run without `--dry-run` and without `--destination` (one that covers every destination) first records its start time as `flush_last` in `state.db`, and at its end records `flush_env_missing`: its start time when it skipped a destination as `env_missing`, else null. Every run then handles new surfaced failures ([Hooks](#hooks)). Then, for each destination (or those named; an unknown `--destination` is exit 2), in parallel: (1) a disabled destination is skipped, state `disabled` (its rows wait and expire); (2) one with a named variable unset is skipped, state `env_missing`; (3) it takes the destination's flush lock without waiting, else state `already_running`; (4) every unclaimed row 24 hours old or more ends `expired`; (5) while the destination is quiet, a due `info` row ends `dropped`, reason `quiet`, and a due `warn` or `action` row gets the window's end as `deliver_after`; (6) it sends the due `single` rows, oldest first, one call each, as [Calling the notifiers](#calling-the-notifiers) says (claim, send lock, call, record); (7) it sends the due `held` rows as one digest, or one such row as itself. State `done` when it called, dropped or expired any row, `quiet` when quiet held every due row, else `nothing_due`. `--dry-run` lists what is due and changes nothing. Exit 12 when a call ended `failed` or `unknown`; 22 when every destination is `nothing_due` or `disabled`, or there is no config, or `enabled` is false (outcome `no_op`, `destinations` empty for the last two); otherwise 0. A SIGTERM, SIGINT or SIGHUP lets the call in progress finish within its deadline; `flush` then makes no further call, releases its other claims and exits with its result, or 128+N when no call had started.

### `history`

`notice-route history [--since <duration|time>] [--source <s>] [--destination <d>] [--level <l>] [--outcome <o>] [--label <k=v>]... [--limit <n>] [--json]`: lists history rows newest first, default limit 100. `--since` is a duration (`[0-9]+(s|m|h|d)`) or an RFC 3339 UTC time; each filter may be given once, except `--label` (all must match, exact `key=value`); `--limit` is 1 to 1000 (else exit 2); a repeated filter other than `--label` is exit 2; `--outcome` takes the outcomes below (`--outcome suppressed` is exit 2); ties order by `id` descending. Filters AND together. The `--json` output is a list validated by `schemas/cli/history.json`. A row holds the rendered title of a templated rule (cut to 100 characters) but never message text. With no `state.db` the list is empty (exit 0).

Outcomes in `history` are those of `send` except `suppressed` (a suppressed notification writes no row; it is in the log and in `count`), plus `sending` (a row whose call is in progress, or whose process died during it) and `expired`. Reasons are closed: `below_floor`, `quiet`, `rate_capped`, `overflow`, `lock_busy`, `env_missing`, `rule_drop`, `no_rule`, `digest`, `link_refused`, `keyword_refused`; `send` output and the log also have `redelivered` and `repeat`.

### `validate`

`notice-route validate [--probe] [--json]`: checks the config offline: loads and validates every key, checks that each destination's `cmd[0]` passes the check of [Destinations](#destinations) (`cmd_ok`), that each `env` name is set to a non-empty value in the router's own environment (unset names are reported in `env_unset`, not an error), that quiet windows and links are well formed, reports `flush_age_s` (seconds since the last `flush` started, null when none has run), checks that `cron.env_file`, when set, is a regular file (by `lstat`, so not a symlink) owned by the user with mode 0600, in a directory that passes the directory rule of `cmd[0]` (it is never read), reports the worst-case send time and the codes surfaced in the current UTC day for each destination (read from `state.db` read-only), and warns about the conditions listed under [Config](#config). With `--probe` it also runs `<cmd> validate --json` (plus `--probe` for `ha-notice` and `wsl-notice`) for each enabled destination of the three known kinds as a call of [Calling the notifiers](#calling-the-notifiers) (the same environment, process group, deadline, output caps and send lock); a destination with a name in `env_unset` is not probed. Exit 0 is `ok`, 22 is `disabled` (information), anything else (a kill or a busy lock included) is `failed`; `generic` destinations are never probed. It never runs `--test-message`, never prompts and never sends. A probe reports the notifier's `enabled` value. Exit 0 when every check passed (warnings allowed), 2 when the config is invalid (the error envelope is printed), 12 when a `cmd_ok` or probe check failed (the full report is printed, outcome `failed`), 22 when there is no config (the object is printed with outcome `no_op` and no destinations).

### `prune`

`notice-route prune [--dry-run]`: deletes history older than `retention.history`, event ids older than `retention.event_ids`, expired dedup rows, surfaced rows older than 2 days, counters of past hours older than 2 days, and every unclaimed queue row whose age is 24 hours or more, whatever its destination (outcome `expired`). It never touches a row with an unexpired claim. It deletes at most 1000 rows per transaction. With no `state.db` it deletes nothing (exit 0). Exit 0.

### `cron install` and `cron uninstall`

`notice-route cron install|uninstall [--dry-run]`: writes or removes one tagged block in the user's crontab, in the shape every awkit tool uses: a first line `# notice-route managed v1 flush=1m`, a `PATH=` line (`/usr/local/bin:/usr/bin:/bin` plus the router's directory), a `* * * * *` line running `flush --quiet` and a line at 03:17 daily running `prune --quiet`, each as `set -a && . '<env_file>' && set +a && '<absolute notice-route>' ...` when `cron.env_file` is set (so a plain `NAME=value` line is exported), and a last line `# notice-route managed end`. The executable is the resolved absolute path of the running program. Every interpolated value must match `[A-Za-z0-9_./+-]+` or `cron install` refuses. A refused block or value, or a `cron.env_file` that fails the `env_file_unsafe` check, is exit 2 `usage`; a crontab that cannot be read or written is exit 1 `internal`. The block starts at the line that begins with `# notice-route managed v` (the end line does not) and ends at the line that is exactly `# notice-route managed end`; it is replaced idempotently. A crontab with more than one of either line, only one of them, or the end line first is refused (exit 2 `usage`) by `install` and `uninstall`. It runs `crontab` from the scrubbed `PATH` with `LC_ALL=C`; only a `crontab -l` that exits 1 saying `no crontab for` is an empty crontab, and any other failure aborts with exit 1. It reads the crontab again just before writing and aborts (exit 1) when it changed. Credentials for the notifiers must be in the env file: `send` without them queues the notification for `flush`, and `flush` without them delivers nothing to that destination.

## Global flags

`--json`, `--config <path>`, `--verbose`, `--quiet`, `--version` where meaningful. `--config <path>` replaces `config.yaml` (drop-ins beside it still apply) and is accepted only by `rules`, `test` and `validate` without `--probe`, which run no command and write nothing; any other command, `send --dry-run` included, refuses it with exit 2 `usage`. `--quiet` suppresses the advisory stderr lines; `--verbose` adds one line per decision. `--dry-run` is accepted by `send`, `flush`, `prune` and `cron`. Abbreviated flags are refused. `--actor` (grammar and default as in design guide section 6: `human:<user>` on a terminal, else `auto:unknown`) and `--decision-id` (one value; a second is exit 2) are accepted by `send` and `flush` and recorded in the log's `actor` and `decision_id`; the call to the notifier always uses `auto:notice-route`. There is no `--yes`: nothing here asks for confirmation.

## Output

With `--json`, `send` prints one object ([schemas/cli/send.json](../schemas/cli/send.json)), also on exit 22:

- `outcome`: `ok` (exit 0), `partial` (exit 12) or `no_op` (exit 22).
- `shape`: `hook` or `notice`. `source`, `hook` (null for a notice), `rule` (the matched rule's name or null), `level` (null when no rule decided it), `reason` (null, `unconfigured`, `no_rule`, `all_disabled`, `disabled` or `rule_drop`), `text_excluded` (boolean), `labels_ignored` (integer).
- `destinations`: a list with one entry per selected destination (empty for `rule_drop`, `no_rule`, `unconfigured` and `disabled`): `name`, `outcome` (`sent`, `deferred`, `queued_retry`, `suppressed`, `dropped`, `disabled`, `failed`, `unknown`), `reason` (a closed string or null, see [`history`](#history)), `attempts`, `code` (a notifier code of pattern `[a-z][a-z0-9_]{0,63}`, untrusted, or a router code from [Hooks](#hooks)), `count` (the repeat count for `suppressed`), `deliver_after` (UTC epoch seconds, set for `deferred` and `queued_retry`, else null), `notifier_outcome` (the notifier's own `outcome` of pattern `[a-z_]{1,32}`, untrusted, for exit 0 or 22, or null).
- `outcome` is `partial` exactly when a destination ended `failed` or `unknown`. A `failed` or `unknown` destination has a non-null `code`; `deferred`, `dropped` and `suppressed` have a non-null `reason`; `sent` and `disabled` have `code` null. Destination names are unique. `reason` `all_disabled` means every selected destination has `enabled: false` in the router's config; a notifier's own exit 22 is outcome `disabled`, with `notifier_outcome` non-null when the notifier's stdout is a valid `{outcome}` and null otherwise, and exit 0.
- `dedupe` (the hex digest used, or null), `duration_ms`.

Exit 1, 2 and 20 print the error envelope (exit 128+N prints nothing): `{"error": {"code", "message", "reason", "details"}}`, with `code` one of `usage`, `config_invalid`, `config_unsafe`, `internal`, `refused`, and a fixed `message` (never input text). `details.key` names the key for config errors (a key path, `home`, `--config`, `.` for the config directory itself, or the file path relative to the config directory for `config_unsafe` and unreadable files); `details.pointer` is set for refusals of `payload_invalid` (`""`, a JSON pointer such as `/title`, or `--source`, `--label`). Without `--json`, stdout is empty and one line per destination goes to stderr; stderr is advisory.

### Other outputs

The other commands' `--json` outputs have schemas under `schemas/cli/`.

- `test` (also `send --dry-run`): `shape`, `source`, `hook`, `rule`, `reason` (as the `reason` of `send`), `title`, `message`, `link`, `missing` (token paths that rendered empty), `excluded` (token paths of text that was left out), `text_excluded`, `labels_ignored`, `level`, `dedupe`, `destinations[]` with `name`, `decision` (`send`, `defer`, `drop`, `suppress`, `disabled`), `reason`, `deliver_after`.
- `flush`: `outcome` (`ok`, `partial`, `no_op`), `dry_run`, `destinations[]` with `name`, `state` (`done`, `already_running`, `nothing_due`, `quiet`, `disabled`, `env_missing`), `due_single`, `due_held`, `sent`, `queued_retry`, `failed`, `unknown`, `dropped`, `expired`, `digests`; `duration_ms`.
- `rules`: `rules[]` with `name`, `match`, `destinations`, `level`, `drop`, `pass_through`, `dedup_window_s`; `destinations[]`; `quiet_hours[]`.
- `validate`: `outcome` (`ok`, `failed` or `no_op`), `probe`, `destinations[]` with `name`, `cmd_ok`, `env_unset`, `probe`, `surfaced_today`; `warnings[]`; `send_worst_case_s`; `flush_age_s`.
- `prune`: `dry_run`, `deleted` (a count per table).
- `cron install`: `action` (`installed`, `updated` or `unchanged`), `block`, `dry_run`, `env_file`. `cron uninstall`: `action` (`removed` or `unchanged`), `block`, `dry_run`.

## Exit codes

Reserved codes 0-21 follow design guide section 4.2 ([design guide](../../awkit-docs/design-guide.md)).

| Exit | Meaning |
|---|---|
| 0 | The router took responsibility for every destination: each ended `sent`, `deferred`, `queued_retry`, `suppressed`, `dropped` or `disabled`. |
| 1 | `internal`. |
| 2 | `usage`, or `config_invalid`, or `config_unsafe` (the config directory, `config.d`, the file or a drop-in is not owned by the user or is group- or world-writable). |
| 12 | At least one destination ended `failed` or `unknown` in this call (or, for `validate`, a check failed). The other destinations were still delivered. `partial` in the output. |
| 20 | Refused, nothing sent. Reasons: `payload_unrecognized`, `source_mismatch`, `payload_invalid`, `hook_recursion`. |
| 22 | Nothing to do: no config or no destination (`unconfigured`), no rule matched (`no_rule`), every selected destination disabled (`all_disabled`) or the router disabled (`enabled: false`). A caller treats it as a successful no-op. |
| 128+N | A signal N before any notifier call started. This process sent nothing; destinations it had already decided are in the queue and the next `flush` delivers them. |

Never used: 10, 11, 13, 21. A signal after the first notifier call started is handled as the deadline: the router finishes the calls it started within their own deadlines and then exits with the result.

## Hooks

One hook, `post-fail`, configured under `hooks` in the router's config as in design guide section 8. An entry has exactly `cmd` (a string of 1 to 4096 characters), `timeout` (1s to 1h, default 30s) and `async`.

| Hook | When it fires | Can veto |
|---|---|---|
| `post-fail` | Run by the next `flush` after a destination's failure was surfaced: at most once per code and destination per UTC day, except `rate_capped`, `env_missing`, `overflow` and `expired`, at most once per destination per UTC hour; at most 8 distinct other codes per destination per UTC day (the four hourly codes are not counted), after which further codes are surfaced once that day as `other`. Best effort, at most once (no outbox in 0.x; a hook that fails is logged and the `history` row shows `surfaced`, which marks the row whose failure was the one surfaced). | no |

The payload ([schemas/hooks/post-fail.json](../schemas/hooks/post-fail.json)) has `tool`, `hook`, `object_type` (`destination`), `object_id` (the destination name), `event_id` (the failing `deliveries` row id, never reused), `labels` (the failing notification's labels), `code` (a notifier error code or refusal reason, or one of `retries_exhausted`, `rate_capped`, `env_missing`, `overflow`, `expired`, `deployment_fault`, `exec_failed`, `generic_failed`, `envelope_invalid`, `output_overflow`, `killed`, `signal`, `other`), `outcome` (`failed`, `unknown`, `deferred` for `rate_capped` and `env_missing`, `dropped` for `overflow`, `expired` for `expired`), `attempts`, `notification_source` and `notification_level` of the notification that failed (the bare names `source` and `level` are the derived fields of [Templates](#templates)), and `kind` of the destination. It carries no title, message, link or payload text. The hook environment is `PATH`, `LANG` and `LC_ALL` as for a notifier call, plus `NOTICE_ROUTE_HOOK`, `NOTICE_ROUTE_ID` and `NOTICE_ROUTE_OBJECT_TYPE`, and nothing else; a hook that needs a credential reads its own file. It must not start a long-lived process, which would inherit `NOTICE_ROUTE_HOOK` (the recursion guard).

Surfacing: the transaction that records a surfaced outcome (`failed`, `unknown`, or a `rate_capped`, `env_missing`, `overflow` or `expired` event) inserts a `surfaced` row when the limits above allow, and marks the `deliveries` row `surfaced`; `send`, `flush` and `prune` only insert it. The next `flush` takes each new `surfaced` row once and then (1) routes its payload as a failure alert, (2) starts the `post-fail` hooks. A hook runs via `sh -c` in its own process group with only stdin, stdout and stderr open, receives the payload on stdin and the environment above, and is killed (its group) at its `timeout` (default 30 s); `flush` waits for its hooks before it exits unless the entry says `async: true`. An `expired` row is surfaced only when its destination is configured and enabled, whether `flush` or `prune` expired it.

Failure alerts: the router routes the `post-fail` payload through its rules as a hook payload of source `notice-route` and hook `post-fail`, like any notification (decisions (1) to (6) and (9) of [`send`](#send), without the redelivery check), except that a destination that would be sent now gets an unclaimed `single` row due now, which the same `flush` delivers. That delivery claims and counts like any call, so the hourly cap and an unset credential apply there: an alert over the cap waits for the next UTC hour. A surfaced row whose failing notification has source `notice-route` (an alert's failure, cap, overflow or expiry) runs the hooks but routes no further alert. An alert about the only destination its rule names cannot arrive; it is in `history` and the hook. `send` refuses an input whose source is `notice-route` (exit 20, `source_mismatch`).

`send` refuses with exit 20 and reason `hook_recursion` whenever `NOTICE_ROUTE_HOOK` is present in its own environment. Any process started from a `post-fail` hook, including a producer whose own hook calls `send`, is refused with `hook_recursion`.

## Calling from automation

Wire a producer's hook to the router with one line, for example `notice-route send --source agent-spawn --stdin-json` on a `post-escalate` hook. A permission profile of a sandboxed session must not allow `notice-route send` or any notifier, and must deny writes to every path the router runs, sources or passes with `--config`: `~/.config/notice-route/`, `~/.local/state/notice-route/`, the router and notifier executables with their interpreters and package directories, each destination's notifier config, `cron.env_file` and the crontab. `send` writes the state directory, and a process that can write it can queue any text, link and level for `flush` to deliver with the credentials; the other paths run, or are read, with the same credentials. A session's events reach the router through its producers' hooks (agent-spawn, claude-session), which run outside the session. Any other caller of `send` chooses the payload (and the text of a pass-through rule), so wire it only from producers and scripts you trust. A hook-run `send` should not be wrapped in a shell pipeline that hides its exit code. There is no per-invocation `--hook` flag.

Wiring hazards: wire either agent-spawn's or claude-session's state hooks to notifications, not both; a producer that offers an outbox redelivers at least once, which the redelivery check absorbs; pr-watch events are at-most-once and carry only metadata, so a notification can say who and which PR but not what a comment said.

## Config

Keys and defaults; schema in [schemas/config.json](../schemas/config.json). `config.example.yaml` documents every key and `examples/awkit-wiring.yaml` is the drop-in with the rules for the awkit producers; together they are a complete worked configuration, validated against the schema in CI and rendered against the producers' hook examples.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | true | When false, `send` exits 22 `disabled` before reading state and `flush` exits 22 doing nothing. |
| `destinations` | map | none | See [Destinations](#destinations). |
| `quiet_hours` | map | none | See [Quiet hours](#quiet-hours). |
| `links` | `{hosts: map host -> github or gitlab, tickets: map site -> base URL}` | empty | See [Links](#links). Hosts are `<host>[:<port>]` in the `pr` grammar (a port is 1 to 65535); ticket sites match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` and their base URLs follow the same literal URL grammar as a rule's `link` and end in `/`. |
| `rules` | list | `[]` | See [Rules](#rules). At most 256. |
| `defaults.dedup_window` | duration | `10m` | The default for a rule's `dedup_window`. |
| `lock_wait` | duration | `2s` | See [Calling the notifiers](#calling-the-notifiers). |
| `retention.history` | duration | `30d` | Age at which `prune` deletes history rows; at least 1d. |
| `retention.event_ids` | duration | `14d` | Age at which `prune` forgets event ids; at least 1d. |
| `hooks` | map | none | `post-fail` entries as in design guide section 8. |
| `cron.env_file` | absolute path | none | A mode-0600 file the cron lines source (and export, `set -a`). |
| `log` | boolean | true | Write `log.jsonl`. |

The grammar of durations is the design guide's (a number of seconds or `[0-9]+(\.[0-9]+)?(ms|s|m)?`, plus the `h` and `d` suffixes for the keys that name hours and days: `retention.*`, `dedup_window`; a message that names the key and never the value). A config error names the key and the problem. Ranges of durations and cross-references (rule names, destinations, windows, time zones) are checked by the loader, not by the schema. The config directory, `config.d`, `config.yaml` and every drop-in must be owned by the user and not group- or world-writable (`config_unsafe`, exit 2), because a drop-in can name a command. A rule naming an unknown destination is a load error.

`validate` warns, without failing, about: a destination no rule uses (`destination_unused`); a rule without `level` whose `level_min` is below `error` (the sender chooses the level; `level_from_notice`); a rule unreachable because an earlier rule's conditions are each also in it with the same value, or with a `*` value that is a prefix of its value (`labels` and `where` as subsets; `rule_unreachable`); a computed `send_worst_case_s` above 30 (`worst_case_over_hook_timeout`); no `flush` in the last 5 minutes (`flush_stale`); a last full `flush` that skipped a destination as `env_missing`, which `validate` cannot see for itself because it never reads the env file (`flush_env_missing`); a `cron.env_file` that is missing or fails the check of `validate` (`env_file_unsafe`); no rule routes a sample `post-fail` payload and no `post-fail` hook exists (`alert_unrouted`); queue rows of a destination that is not configured (`orphaned_queue`, with the count). It knows no producer: the example is checked in CI, and `test` shows `missing` and `excluded` for any real payload.

## Environment variables

None are read for configuration. The router's environment is inside its trust boundary: a caller should not let an agent session set it. The router reads `NOTICE_ROUTE_HOOK` only for the recursion guard and sets `NOTICE_ROUTE_HOOK`, `NOTICE_ROUTE_ID` and `NOTICE_ROUTE_OBJECT_TYPE` for hook commands. It reads the variables named in the destinations' `env` lists only to pass them on, and `PATH`, `LANG`, `LC_ALL`, `WSL_INTEROP` and `WSL_DISTRO_NAME` for the scrubbed child environment.

Config and state are found only from the home directory in the password database; `HOME`, `XDG_CONFIG_HOME` and `XDG_STATE_HOME` are not read (a recorded deviation from design guide section 5, for the same reason as the notifiers: the config selects commands that run with credentials in their environment). A home directory that cannot be resolved, or is relative or empty, is exit 2 `config_invalid` (`details.key` `home`).

## Log

`log.jsonl` under the state directory is public and append-only ([schemas/log.json](../schemas/log.json)): one line per destination outcome (`record` `delivery`; a `drop` rule and `no_rule` write one without a destination) and per `flush` summary (`record` `flush`, with totals), with `ts`, `tool`, `version`, `actor`, `decision_id`, `source`, `hook`, `rule`, `destination`, `level`, `outcome`, `reason`, `code`, `attempts`, the first 16 hex characters of the SHA-256 of the rendered UTF-8 title and message of the row (for a digest's rows, each row's own text), `event_id`, and the first 16 characters of the dedupe digest (`dedupe_key_sha256`); a `flush` record's `due` is the number of due rows. It never holds title text, message text, links or credentials. It rotates at 10 MiB, keeping three rotated files (`log.1` to `log.3`). It is a record of recent activity, not an audit trail: a flood rotates older lines away. A problem with the log warns on stderr and never changes a result. There is no `audit.jsonl`: a notification changes nothing in a system of record (a recorded deviation from design guide section 6, as for the notifiers).

## Files

Under the state directory (`<home>/.local/state/notice-route/`):

| File | Content |
|---|---|
| `state.db` | SQLite, WAL, private, versioned migrations. `deliveries`: `id` (never reused), `ts` (decision time, UTC epoch seconds), `source`, `hook`, `object_id` (an integer or a plain string, else null), `rule`, `destination` (null for `rule_drop` and `no_rule`), `level`, `outcome`, `reason`, `code`, `attempts`, `count`, `event_id` (its JSON text; an integer or a plain string), `dedupe`, `labels`, `surfaced`, `deliver_after`, `title` (the rendered title cut to 100 characters; null for a pass-through row), `title_sha256`, `message_sha256` (16 hex). `queue`: `id` (never reused), `delivery_id`, `kind` (`held` or `single`), the rendered `title`, `message`, `link` and `level`, `source`, `rule`, `dedupe`, `deliver_after`, `created_at`, `claimed_by`, `claim_expires`. `dedup`: destination, key digest, window start, window length, `delivery_id`. `event_ids`: destination, source, hook, event id, repeat digest, seen at, `delivery_id`. `surfaced`: destination, code, period (see [Hooks](#hooks)), `delivery_id`, processed. `rate`: destination, UTC hour, class (`error` or other), count. `meta`: `flush_last` and `flush_env_missing` (UTC epoch seconds or null). Row ids are `AUTOINCREMENT`. |
| `locks/<destination>.lock` | The per-destination send lock; `locks/_wsl.lock` is shared by every `wsl-notice` destination. |
| `locks/<destination>.flush` | The per-destination flush lock. |
| `log.jsonl`, `log.1` to `log.3`, `log.lock` | The public log. |

The router creates the state directory with mode 0700 under `umask 077` and refuses (exit 1) one that is not a directory it owns with no group or world access. Only the last component is checked: the existing parents of the state directory (the home directory and above) are followed as they are and are trusted. It opens every file relative to it with `O_NOFOLLOW` and `O_NONBLOCK` and checks `fstat` for a regular file: a bad `log.jsonl` is skipped with a warning, a bad lock file makes the call `lock_busy`, a bad `state.db` is exit 1. A database that cannot be used (corrupt, a newer schema, read-only, or busy past the timeout: 0.5 s for each of `send`'s two transactions, 5 s for every other command) is exit 1 `internal`. A read-only open (`test`, `send --dry-run`, `validate`) of a database with an older schema is also exit 1 `internal` until a command that writes migrates it, because the newer queries cannot read the older schema safely; an empty `state.db` (created but never migrated) is read as no database. Before `send`'s first call nothing was sent; after it, the calls' rows keep their claims, which lapse, and `flush` resends them (a duplicate is possible, and stderr says so). Every value reaches SQL as a bound parameter.

`deliveries` and `event_ids` hold at most 100000 rows each: the transaction that inserts a row deletes the oldest rows beyond that (at most 16 per transaction). A `deliveries` row that has a queue row or an unprocessed `surfaced` row is never deleted, and `dropped` rows go before the others; a `surfaced` row whose `deliveries` row is gone is marked processed and skipped, never an error. A `deliveries` row stores at most 16 labels, the lexically first, within 1024 characters in all; matching uses every label.

A `deliveries` row is updated in place by every later call for it. A repeat suppressed by the repeat check and a redelivery write no row; a repeat adds 1 to `count` of the row that opened its window. A digest gives each of its rows the digest's outcome and reason `digest`.

Every call claims the rows it carries, in the transaction that decides it (`send`: the decision transaction; `flush`: a transaction just before each call), with `claim_expires` now plus twice `send_worst_case_s`; a lease is never longer than that, even if the clock has moved. A row whose claim has expired is unclaimed and due, and the next `flush` sends it (one attempt). Every result is recorded only on rows still claimed by the recording process. A lapsed claim that was not taken over still accepts its claimant's result. A release sets `claim_expires` to the lesser of its value and now and keeps the claimant. A resend (a duplicate, never a loss) can also follow a clock step, a lease read before the write lock was held, or a config change that shortens `send_worst_case_s` while claims made under the longer lease are in flight. A notification is therefore delivered at least once: a process killed or stalled after its call reached the notifier and before the result was recorded causes one duplicate; a process killed before the call loses nothing. The message text of a row is deleted with the row.

Configuration: `<home>/.config/notice-route/config.yaml`, then `config.d/*.yaml` in lexical order: `rules` and `hooks.post-fail` lists append in that order; entries of `destinations`, `quiet_hours`, `links.hosts` and `links.tickets` merge by name; a rule name or entry name defined twice, or any other leaf key defined in two files, is `config_invalid` (maps such as `defaults` and `retention` merge key by key; only a leaf present in two files is refused). Each file is opened with `O_NOFOLLOW` and checked on the descriptor (owned by the user, not group- or world-writable, a regular file); a drop-in that is a symlink is refused; the 256 KiB limit applies to the bytes read, and 1 MiB to `config.yaml` and all drop-ins together (the file whose read crosses it is `config_invalid`, keyed by that file). A `config.d` with more than 4096 directory entries of any name is `config_invalid` (key `config.d`). At most 256 drop-ins (the visible names ending in `.yaml`, in code point order; a symlinked config directory or `config.d` is refused like a symlinked file; a missing `config.yaml` is `unconfigured` whatever `config.d` holds). A YAML document uses the strict loader of the other tools (no anchors, aliases or explicit tags, no duplicate keys, `yes`, `on` and dates are strings).

## Labels

notice-route stores no labeled objects. `--label` and a payload's `labels` are inputs to rule matching and to `history` only.

## TUI

None in 0.x. Every action is a command.

## Residual risks

- A process that can write the state directory bypasses every rule; keep it outside every sandbox (see [Calling from automation](#calling-from-automation)). A sender allowed `send` can forge a hook-shaped payload with any source, hook, labels, `repo`, `pr`, `site` and `key` (none of these is an authorization), and can choose the taint flags. The cap converts a flood into delayed or dropped real notifications, not only into a bound. It can send first a notification that renders like an expected real one, which is then `suppressed` for the rule's window, and resend a real event (same id and same text), which is then `redelivered`. A forged event id with the text of a real future event mutes that event for up to `retention.event_ids` (14 days by default), not only for one `dedup_window`. The plain-value rule and the opt-in keep free text out of titles; a forger can still choose a plain value of up to 256 characters, such as a bare host and path that Slack and some clients turn into a link; the link tables cannot prevent that. The flags that gate opted-in text are the payload's own claims (they protect against an honest producer, not a forger). A rule with `level_min` and no `level` lets a notice choose its own level, `error` included (`validate` warns, `level_from_notice`): set `level` on rules that match senders you do not trust. A pass-through rule sends the sender's text, which can hold a full URL that Slack turns into a live link. A forged payload that matches a rule with `level: error` bypasses quiet hours and has the `error` class's whole budget: up to `max_per_hour` pushes an hour at night, and twice that across an hour boundary.
- `sent` is not delivered: Slack and Home Assistant do not confirm display, and wsl-notice's `sent` means handed to Windows. `unknown` outcomes may have been delivered; a duplicate is possible after a `send` or `flush` killed during a call. A caller that kills `send` during a call (a hook runner's timeout) leaves the notifier running in its own session; its row's claim lapses and `flush` resends it, so the notification can arrive twice.
- Titles and messages appear on lock screens, in toast history and in the destination; the router never puts a credential in them, and free text from a payload only with the explicit opt-in or through a pass-through rule.
- The notifiers' own limits are far below the router's: ha-notice cuts a message at 1000 characters, wsl-notice a title at 100. The router passes the full text and the notifier reports `truncated`.
- Clocks: deadlines use the monotonic clock; quiet windows and `deliver_after` use the wall clock, so a large step moves dedup windows, UTC-hour cap buckets and `deliver_after` by the step, once. A forward step of 24 hours or more expires every queued row at the next `flush` or `prune`. A dedup window whose start is after now is not active and the next notification replaces it (a duplicate, never a loss); an event id seen after now is not live.
- A destination command and its `env` are trusted configuration; a user who adds a malicious drop-in owns the machine already. The ownership and mode check is the only guard.
- Ordering between destinations is not guaranteed, and a retry can arrive after a later notification.
- Repeats suppressed inside a window whose first notification later fails are not resent; they are counted on its row.

## Platform notes

Linux and WSL are first-class; macOS is checked locally. The router needs SQLite 3.35 or newer in Python's `sqlite3` module (commands that write the state database are refused otherwise; read-only ones still work), `flock`, `zoneinfo` with the system time-zone database (the `tzdata` package is not needed when the system has `/usr/share/zoneinfo`, and the router refuses a time zone name it cannot load) and `cron` for `flush`. A `wsl-notice` destination exists only on WSL. Under cron, the credentials must be in the env file the cron line sources, because the notifiers read a credential only from their own environment; a `send` without the credentials (a producer under its own cron, a shell without the exports) queues the notification for `flush`, which runs with the env file; a `flush` without them delivers nothing to that destination and surfaces `env_missing`. On WSL the cron daemon is not started by default; start it (for example from the distribution's boot command) or `flush` never runs, which `validate` reports as `flush_stale`. A stale `WSL_INTEROP` in a long-lived tmux or cron breaks `wsl-notice` (the router passes the current value and reports `interop_failed`).
