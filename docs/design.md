# notice-route: design

Status: designed 2026-11-17 against architecture v1.24, revised after review pass 1. The normative rules are in [interface.md](interface.md); this document records why, and how the tool is built. It links to interface headings and does not restate their tables.

## Role and layer

Layer 2 (composite). Routes notifications to channel notifiers by source, labels, level and time, with quiet hours, deferral, dedup, a per-destination rate cap, retries and a history. It works fully standalone: it needs only a command that accepts `send --stdin-json`, and the three notifiers we ship are the natural targets. It has no upward awareness: a producer reaches it through a hook line in the producer's own config, and nothing in the router knows who that producer is beyond the `tool` name in the payload.

## Uses

| Dependency | Binding | Notes |
|---|---|---|
| slack-notice, ha-notice, wsl-notice | B (configurable command) | Each destination names a command; the call shape is fixed (`send --stdin-json --json --actor auto:notice-route [--dedupe-key=<hex>]`). A `kind` selects the retry rules. See [Destinations](interface.md#destinations) and [Calling the notifiers](interface.md#calling-the-notifiers). |
| any other notifier | B, `generic` kind | Works when it reads the notification JSON from stdin; otherwise a wrapper script. No adapter code is shipped. |
| a time-zone database | system | `zoneinfo` from the standard library with the system database. |

The architecture entry for this tool said "binding C (adapters)" and also fixed the call shape, which is binding B. The call shape wins (D-6), and the architecture entry is corrected in v1.24. No other tool is a dependency: pr-watch, review-watch, ticket-work, agent-spawn and the rest reach the router only through hooks wired in their own configs.

## Decisions

| ID | Decision | Rationale | Interface |
|---|---|---|---|
| D-1 | Two input shapes told apart by keys, no mode flag; `payload_unrecognized` and `source_mismatch` refusals. | One documented hook line; a script cannot rename another tool's events. A rule without `message` passes a notice's own text through. | [Input shapes](interface.md#input-shapes) |
| D-2 | First matching rule wins; a rule can fan out to up to 8 destinations or `drop`. | One rule explains a routing; adding a rule cannot silently duplicate sends. | [Rules](interface.md#rules) |
| D-3 | The rule decides the level; the notice's own level is only a fallback; no `--level` flag. | A script cannot claim `error` to bypass quiet hours on a rule that fixes the level. | [Levels](interface.md#levels) |
| D-4 | Templates are a single-pass `{{field}}` substitution. A payload value goes into a title only when it is plain (an integer, a boolean, or a short word-like string); other text reaches only a message, through `include_text`, for payloads with `reads_third_party: false` and `tainted: false`. | The value is judged, not its field name, so a new free-text field fails closed without a list that tracks every producer; "what the agent asked" stays possible. | [Untrusted text](interface.md#untrusted-text), [Templates](interface.md#templates) |
| D-5 | Links come only from `links` tables and fixed payload ids. | A producer cannot aim the tap target, and a rule's link is one derived token or a literal URL. | [Links](interface.md#links) |
| D-6 | A destination is a command (binding B) with `kind`, `floor`, `quiet`, `max_per_hour`, `env`. | The call shape is fixed; adapters would track each notifier's changes. | [Destinations](interface.md#destinations) |
| D-7 | Named quiet windows referenced per destination; `timezone` required; wall-clock minute-of-day rule; only `error` bypasses. | A phone should be quiet while a desktop toast is harmless; spike A. | [Quiet hours](interface.md#quiet-hours) |
| D-8 | A deferred batch is one digest per destination, titled `N held notifications`; at most 100 queue rows (deferred and retry together) per destination; rows 24 hours old or more expire. | Fifteen buzzes at 07:00 defeat quiet hours. | [Deferral and digest](interface.md#deferral-and-digest) |
| D-9 | Two dedup checks per destination: redelivery by source, hook and `event_id`, and a fixed-window repeat check keyed on the notification as it would be sent (level included); its digest is passed as `--dedupe-key=<hex>`. | Two notifications that look the same are one; any field that shows makes them two, without a list of producer fields. A sliding window suppresses a steady repeater forever. Event ids are compared per source and hook, because producers number them independently. Records are released when a delivery fails, so a failed first send does not mute the next one. | [Dedup](interface.md#dedup) |
| D-10 | Every send-now decision is queued before its call; retries go through the queue and `flush` with a fixed schedule; a possibly-sent outcome is never retried (ha-notice's both-true excepted). | A hook-run `send` must end quickly; retrying a possibly-sent Slack message duplicates it. A crash during a call is the one case that resends (at least once). | [Retries and outcomes](interface.md#retries-and-outcomes) |
| D-11 | Exit 0 means the router took responsibility for every destination; 12 means a definite failure or unknown outcome; 22 means nothing to do. | Scripts can tell a failure from quiet hours or dedup. | [Exit codes](interface.md#exit-codes) |
| D-12 | History keeps the rendered title of a templated rule (literal text and plain values only) and hashes; message text is deleted with its queue row; no `audit.jsonl`. | Opted-in untrusted text never rests on disk after delivery. | [Files](interface.md#files), [Log](interface.md#log) |
| D-13 | Config and state are found only from the password-database home; owned-by-user and no group or world write; no per-repo or environment layer; `--config` only on commands that run nothing. | The config selects commands that run with credentials in their environment. `--config` is accepted only by commands that run nothing, so allowing `send` cannot select a command. | [Environment variables](interface.md#environment-variables), [Config](interface.md#config) |
| D-14 | One hook, `post-fail`, with a recursion guard on `NOTICE_ROUTE_HOOK`; surfaced failures are also routed through the rules as source `notice-route`. | A way to learn a channel is down without a loop, with quiet hours, floors and credentials applied like any other notification. | [Hooks](interface.md#hooks) |
| D-15 | Eight commands; no `queue` or `destination` management in 0.x; `test` is explicit, never sends and takes `send`'s input flags. | Each command has one job; management is state reachable by config and `history`. | [Commands](interface.md#commands) |
| D-16 | Process model: parallel across destinations, serialized per destination by `flock`, own process group, bounded output, monotonic deadline, group kill. | Spike B. | [Calling the notifiers](interface.md#calling-the-notifiers) |
| D-17 | A per-destination counter of calls per UTC hour, kept separately for `error` and for the other levels; overflow is folded into the next digest. | Dedup stops repeats, not a flood of distinct events. A flood of non-errors cannot delay an `error`, and eviction drops the lowest level first. | [Rate limit](interface.md#rate-limit) |
| D-18 | No built-in rules or destinations; `config.example.yaml` is a complete worked config. | A built-in rule would silently change who is pinged when a producer's enum list changes. `validate` knows no producer; the example is checked in CI. | [Config](interface.md#config) |

## Calls made by the author

Decisions the user did not see as choices, made while writing and open to correction:

- C-1: the whole-call budget of the approved model bounds only the lock wait (a started call runs to its deadline); `lock_wait` defaults to 2s, a `send` makes at most one call per destination, and the worst case is 29 s with defaults, counted from the end of input, under the hooks' 30 s default. `validate` prints the figure.
- C-2: a destination's `timeout` is at least the notifier's own; the router cannot read the notifier's value.
- C-3: input limits for the strict reader (1 MiB, depth 16) and label grammar (producer-compatible, up to 64 payload labels read and 32 `--label`).
- C-4: the retry schedule (1 m, 5 m, 30 m, 2 h counted from the end of the previous call; 5 attempts), digest size (10 lines), the 24 hour expiry; `lock_busy` follows the retry schedule and counts as an attempt; one 100-row cap covers deferred and retry rows; immediate retries go through the queue.
- C-5: the cap counts calls started (failures too) in the current UTC hour.
- C-7: `post-fail` is best-effort, at most once, with no outbox in 0.x; expiry and overflow are surfaced too.
- C-8: a label on a payload that does not fit the grammar is ignored and counted, not refused.
- C-9: exit 20 for payload faults only; `config_unsafe` and bad config are exit 2.
- C-10: pass-through rules require `level_min`, and messages are cut at 4000 characters.
- C-11: a `send` that lacks a destination's credential defers to `flush` (`env_missing`).
- C-12: the forbidden `env` names list is removed; the name grammar and a fixed set remain.
- C-13: queue rows of `send` are claimed and resent after a crash.
- C-14: drop-ins append `rules` and `hooks.post-fail` lists and refuse any other key defined twice.
- C-15: the digest title is `N held notifications`.
- C-16: `prune` keeps at most 100000 rows in `deliveries` and `event_ids` per destination, and retention settings are at least 1 day.
- C-17: input must be complete within 5 seconds, and a lone surrogate is replaced, not refused.
- C-18: a top-level `enabled` key disables the router.

## Resolved open topics

The seeded open topics were rule syntax, dedup window and quiet-hours config. Rule syntax is [Rules](interface.md#rules) with the closed conditions of D-2. The dedup window is a fixed window, 10 minutes by default and 1 second to 24 hours per rule (D-9). Quiet hours are named windows referenced per destination (D-7). The questions the architecture left implicit (input discrimination, level precedence, untrusted text, links, retries, history, state, hooks, config location, exit codes, rate limiting) are D-1 to D-18.

## Live verification

Two free local spikes ran before the questions (private records in the umbrella; no network and no notification).

| Spike | Finding | Where it lands |
|---|---|---|
| A, window membership | A wall-clock minute-of-day rule matched an independent reference for every minute of 2026 in New York, London and Kolkata across 7 window configs, including a start in a gap, an end in a gap and an end in a fold; `window_end` was always later than now. | [Quiet hours](interface.md#quiet-hours); a property test. |
| A, time zone lookup | `PYTHONTZPATH` is honored even under `python3 -I`; bad keys raise `ZoneInfoNotFoundError` or `ValueError`. | Zones load only from the system database, `localtime`, `posixrules` and `Factory` are refused, and any lookup error is `config_invalid` ([Quiet hours](interface.md#quiet-hours)). |
| A, clock | UTC epoch integers are stable under a backwards step; local ISO strings misorder across fall-back. | `deliver_after` is epoch seconds; the lease is capped at `now` plus its length. |
| A, queue | 8 concurrent senders and 3 flushers claimed 800 of 800 rows once with no lock error; a killed claim expires and is re-claimed; a kill between the call and the mark duplicates once. Only `send` rows carry claims now, and the duplicate is the at-least-once rule. | [Files](interface.md#files). |
| A, dedup | One `INSERT ... ON CONFLICT DO UPDATE ... RETURNING count` lets exactly one of 8 writers through; with a 40 s repeater and a 60 s window a fixed window passes 46 of 91 and a sliding window passes 1. | [Dedup](interface.md#dedup). |
| A, matching and templates | `fnmatch` surprises (`[` classes, `*` crossing `/`) make exact plus a trailing `*` the right grammar; a single-pass substitution does not re-expand a value that holds `{{title}}`. | [Rules](interface.md#rules), [Templates](interface.md#templates). |
| A, JSON | The default parser accepts duplicate keys, `NaN`, `1e999`, lone surrogates (now replaced) and any size; depth 100000 raises `RecursionError`. | The strict reader of [Input shapes](interface.md#input-shapes). |
| B, spawning | A grandchild holding stdout hangs `communicate()` after killing only the child; killing the process group fixes it; a bounded final read keeps a descendant that left the group from holding the router. | [Calling the notifiers](interface.md#calling-the-notifiers). |
| B, output | `communicate()` on 200 MiB used 413 MB; a bounded reader stays at 12 MB. | The 64 KiB and 16 KiB caps. |
| B, locks | `flock` kept one in the critical section, released on SIGKILL, with fair FIFO order for blocking `flock`; the router polls without blocking, so order is not guaranteed; `O_EXCL` lock files were unfair and went stale. | The per-destination `flock`. |
| B, deadline | A monotonic deadline is unaffected by a wall-clock jump; three serial channels cost 42 s worst case. | Parallel across destinations. |
| B, environment | Python, uv-tool shebangs and `pwd.getpwuid` work with an empty environment; ten silent PowerShell launches (three in parallel) exited 0. | The scrubbed child environment. |

Still to verify (live tests in the plan, with conservative defaults until then): the real three notifiers once implemented (a marked test to the user's Slack DM only, a Home Assistant message, a toast); wsl-notice's display gate; macOS cron and `flock`.

## Modules

| Module | Responsibility |
|---|---|
| `cli.py` | argparse, the commands, exit codes, output |
| `config.py` | strict YAML load, drop-ins, ownership check, validation (pydantic) |
| `payload.py` | strict JSON reader, shape discrimination, labels |
| `rules.py` | matching and the first-match decision |
| `render.py` | templates, derived link fields, untrusted-text handling, sanitizing |
| `window.py` | quiet-hour membership and window ends (pure functions) |
| `decide.py` | the per-destination decision order and the dedup and rate statements |
| `store.py` | SQLite access, migrations, bounded tables, counters |
| `queue.py` | queue claims and recovery: `send` claims, lease expiry, the flush lock, the 100-row cap and lowest-level eviction |
| `notifier.py` | building the call, classifying a result, the retry schedule |
| `gateway.py` | the only place that spawns processes, locks files, reads clocks and touches the filesystem outside the store |
| `flush.py` | due rows, digests, delivery |
| `history.py`, `hooks.py`, `cron.py`, `log.py` | `history`, `post-fail`, the cron block, `log.jsonl` |

Dependencies: `pyyaml` (YAML cannot be read from the standard library) and `pydantic` (the design guide's config validation); everything else is the standard library (`argparse`, `sqlite3`, `zoneinfo`, `subprocess`, `selectors`, `fcntl`, `hashlib`, `json`). No HTTP client is needed: the router never speaks to a network.

## Data model

See [Files](interface.md#files). Migrations are versioned and additive. Rows are updated in place; repeats are counted, not stored. Every table is bounded: queue 100 rows per destination, `deliveries` and `event_ids` 100000 per destination after `prune`, dedup rows until their window ends. The queue row carries rendered text only until its delivery, expiry or drop. A destination's identity in every table is its name; renaming a destination in config starts a fresh history for it.

## Concurrency

Any number of `send` and `flush` processes may run at once. The decision step is one `BEGIN IMMEDIATE` transaction with a 5 second busy timeout; it also writes the queue rows of the destinations to be sent now. The send step is serialized per destination by `flock`. A `send` claims the queue rows it is about to send; `flush` resends a row whose claim expired. A `flush` holds a per-destination flush lock for its whole run and the send lock per call; only `send` rows carry a lease. Exact PIDs the router started are the only processes it signals, as process groups it created.

## Error handling

Failures are values: every call ends in one of the outcomes of [Retries and outcomes](interface.md#retries-and-outcomes), recorded in `history` and `log.jsonl`. State problems fail closed as [Files](interface.md#files) says: nothing is sent when the database cannot be used. Config errors fail fast with the key named and never the value.

## Test strategy

- Every subprocess, lock and clock use goes through `gateway.py`; tests substitute fakes. Fake notifiers live in `tests/fakebin/` and can exit with any code, print any envelope, sleep, ignore SIGTERM, spawn a grandchild, close stdin early and write oversized output.
- `--json` outputs have golden files and are validated against `schemas/`; the sibling notifiers' error envelope and `send` schemas are copied into `tests/` at a pinned version.
- Process tests run in CI: group kill with a grandchild, the bounded reader, `flock` serialization across 8 processes including a SIGKILLed holder, concurrent `send` and `flush` claiming each row once, SIGKILL after the decision transaction and before the call (the row is sent by the next `flush` and a redelivery of the same `event_id` is `redelivered`), SIGTERM before the call (claims cleared, exit 128+N), two `flush` on one destination with 40 due rows (each row sent once), the dedup statement under 8 writers.
- Spike regressions: the window brute force as a property test; the strict reader cases; the template non-re-expansion case; bad time zone keys; the fixture "New York, 2026-11-01, window 22:00 to 01:30: inside until 01:30 EDT, inside again 01:00 to 01:30 EST".
- Behavior cases: a `failed` first send, then the next identical notification inside the window is sent; agent-spawn and claude-session both send `event_id: 41` to one destination, and both are sent.
- Fake notifiers also include a double fork with `setsid` holding stdout.
- The example config and the drop-in are rendered in CI against pinned copies of the producers' hook schemas' examples; any `missing` or `excluded` token fails.
- `live` tests (deselected, never in CI): the real notifier trio, a marked test to the user's own Slack direct messages only.
- `make verify` runs ruff, pyright, pytest and markdown lint. CI is Linux only; macOS is checked locally.

## Architecture changes (v1.24)

Section 5.2 entry (binding B, commands, state, hook), section 15 and the section 20 entry (decided constraints replace the seed text; the dedup key, keyed on the notification as it would be sent), section 10.4 (`--source`), section 7.7 (the cron block, `flush` every minute), design guide sections 5, 6 and 12 (deviation notes and the stale "channel tools escape markup" sentence), the interface registry row and the roadmap hook candidate `post-fail`. Recorded in the private cross-cutting log.
