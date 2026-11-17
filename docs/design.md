# notice-route: design

Status: designed 2026-11-17 against architecture v1.24. The normative rules are in [interface.md](interface.md); this document records why, and how the tool is built. It links to interface headings and does not restate their tables.

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
| D-1 | Two input shapes told apart by keys, no mode flag; `payload_unrecognized` and `source_mismatch` refusals. | One documented hook line; a script cannot rename another tool's events. | [Input shapes](interface.md#input-shapes) |
| D-2 | First matching rule wins; a rule can fan out to up to 8 destinations or `drop`. | One rule explains a routing; adding a rule cannot silently duplicate sends. | [Rules](interface.md#rules) |
| D-3 | The rule decides the level; the notice's own level is only a fallback; no `--level` flag. | A script cannot claim `error` to bypass quiet hours on a rule that fixes the level. | [Levels](interface.md#levels) |
| D-4 | Templates are a single-pass `{{field}}` substitution; a fixed deny-list of free-text fields is opt-in and only for payloads with `reads_third_party: false` and `tainted: false`; titles never carry untrusted text. | A third-party-reading or tainted session never leaks text into a notification, while "what the agent asked" stays possible. | [Untrusted text](interface.md#untrusted-text), [Templates](interface.md#templates) |
| D-5 | Links come only from `links` tables and fixed payload ids. | A producer cannot aim the tap target. | [Links](interface.md#links) |
| D-6 | A destination is a command (binding B) with `kind`, `floor`, `quiet`, `max_per_hour`, `env`. | The call shape is fixed; adapters would track each notifier's changes. | [Destinations](interface.md#destinations) |
| D-7 | Named quiet windows referenced per destination; `timezone` required; wall-clock minute-of-day rule; only `error` bypasses. | A phone should be quiet while a desktop toast is harmless; spike A. | [Quiet hours](interface.md#quiet-hours) |
| D-8 | A deferred batch is one digest per destination; at most 100 rows kept; rows over 24 hours old expire. | Fifteen buzzes at 07:00 defeat quiet hours. | [Deferral and digest](interface.md#deferral-and-digest) |
| D-9 | Two dedup checks per destination: redelivery by `event_id`, and a fixed-window repeat check on a cause-aware key that includes the level; the digest of the key is passed as `--dedupe-key=<hex>`. | The architecture's key would swallow distinct events on one object; a sliding window suppresses a steady repeater forever. | [Dedup](interface.md#dedup) |
| D-10 | Retries go through the queue and `flush` with a fixed schedule; a possibly-sent outcome is never retried (ha-notice with a tag excepted). | A hook-run `send` must end quickly; retrying a possibly-sent Slack message duplicates it. | [Retries and outcomes](interface.md#retries-and-outcomes) |
| D-11 | Exit 0 means the router took responsibility for every destination; 12 means a definite failure or unknown outcome; 22 means nothing to do. | Scripts can tell a failure from quiet hours or dedup. | [Exit codes](interface.md#exit-codes) |
| D-12 | History keeps the rendered title (never untrusted text) and hashes; message text is deleted with its queue row; no `audit.jsonl`. | Opted-in untrusted text never rests on disk after delivery. | [Files](interface.md#files), [Log](interface.md#log) |
| D-13 | Config and state are found only from the password-database home; owned-by-user and no group or world write; no per-repo or environment layer. | The config selects commands that run with credentials in their environment. | [Environment variables](interface.md#environment-variables), [Config](interface.md#config) |
| D-14 | One hook, `post-fail`, with a recursion guard on `NOTICE_ROUTE_HOOK`. | A way to learn a channel is down without a loop. | [Hooks](interface.md#hooks) |
| D-15 | Eight commands; no `queue` or `destination` management in 0.x; `test` is explicit and never sends. | Each command has one job; management is state reachable by config and `history`. | [Commands](interface.md#commands) |
| D-16 | Process model: parallel across destinations, serialized per destination by `flock`, own process group, bounded output, monotonic deadline, group kill. | Spike B. | [Calling the notifiers](interface.md#calling-the-notifiers) |
| D-17 | A per-destination hourly cap folds overflow into the next digest. | Dedup stops repeats, not a flood of distinct events. | [Rate limit](interface.md#rate-limit) |
| D-18 | No built-in rules or destinations; `config.example.yaml` is a complete worked config. | A built-in rule would silently change who is pinged when a producer's enum list changes. | [Config](interface.md#config) |

## Calls made by the author

Decisions the user did not see as choices, made while writing and open to correction:

- C-1: the whole-call budget of the approved model became `lock_wait` (default 5s). A started call runs to its own deadline, so a hook-run `send` ends in at most `lock_wait` + `timeout` + 11 s, 26 s with defaults, under the hooks' 30 s default. `validate` prints the figure.
- C-2: a destination's `timeout` is declared in the router config and must equal the notifier's own, because the router does not pass it; `validate --probe` warns when it is shorter.
- C-3: input limits for the strict reader (1 MiB, depth 16) and label grammar (producer-compatible, at most 32 labels).
- C-4: the retry schedule (1 m, 5 m, 30 m, 2 h; 5 attempts), digest size (10 lines), the 24 hour expiry, the 100-row cap, `lock_busy` retries that do not count as attempts.
- C-5: the rate cap counts `sent` and `unknown` rows and applies to digests as one notification.
- C-6: deny-listed names `report`, `message`, `summary`, `details`, `output_tail`, `error`; ticket-work's `post-pending` `summary` is ticket text with no flags, so it is never inserted.
- C-7: `post-fail` is best-effort, at most once, with no outbox in 0.x.
- C-8: a label on a payload that does not fit the grammar is ignored and counted, not refused.
- C-9: exit 20 for payload faults only; `config_unsafe` and bad config are exit 2.

## Resolved open topics

The seeded open topics were rule syntax, dedup window and quiet-hours config. Rule syntax is [Rules](interface.md#rules) with the closed conditions of D-2. The dedup window is a fixed window, 10 minutes by default and 1 second to 24 hours per rule (D-9). Quiet hours are named windows referenced per destination (D-7). The questions the architecture left implicit (input discrimination, level precedence, untrusted text, links, retries, history, state, hooks, config location, exit codes, rate limiting) are D-1 to D-18.

## Live verification

Two free local spikes ran before the questions (private records in the umbrella; no network and no notification).

| Spike | Finding | Where it lands |
|---|---|---|
| A, window membership | A wall-clock minute-of-day rule matched an independent reference for every minute of 2026 in New York, London and Kolkata across 7 window configs, including a start in a gap, an end in a gap and an end in a fold; `window_end` was always later than now. | [Quiet hours](interface.md#quiet-hours); a property test. |
| A, time zone lookup | `PYTHONTZPATH` is honored even under `python3 -I`; bad keys raise `ZoneInfoNotFoundError` or `ValueError`. | The key is checked against a pattern and all three exceptions are caught. |
| A, clock | UTC epoch integers are stable under a backwards step; local ISO strings misorder across fall-back. | `deliver_after` is epoch seconds; the lease is capped at `now` plus its length. |
| A, queue | 8 concurrent senders and 3 flushers claimed 800 of 800 rows once with no lock error; a killed flush's claim expires and is re-claimed; a kill between the call and the mark duplicates once. | [Files](interface.md#files). |
| A, dedup | One `INSERT ... ON CONFLICT DO UPDATE ... RETURNING count` lets exactly one of 8 writers through; with a 40 s repeater and a 60 s window a fixed window passes 46 of 91 and a sliding window passes 1. | [Dedup](interface.md#dedup). |
| A, matching and templates | `fnmatch` surprises (`[` classes, `*` crossing `/`) make exact plus a trailing `*` the right grammar; a single-pass substitution does not re-expand a value that holds `{{title}}`. | [Rules](interface.md#rules), [Templates](interface.md#templates). |
| A, JSON | The default parser accepts duplicate keys, `NaN`, `1e999`, lone surrogates and any size; depth 100000 raises `RecursionError`. | The strict reader of [Input shapes](interface.md#input-shapes). |
| B, spawning | A grandchild holding stdout hangs `communicate()` after killing only the child; killing the process group fixes it. | [Calling the notifiers](interface.md#calling-the-notifiers). |
| B, output | `communicate()` on 200 MiB used 413 MB; a bounded reader stays at 12 MB. | The 64 KiB and 16 KiB caps. |
| B, locks | `flock` kept one in the critical section, FIFO order, released on SIGKILL; `O_EXCL` lock files were unfair and went stale. | The per-destination `flock`. |
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
| `store.py` | SQLite access, migrations, claims and leases |
| `notifier.py` | building the call, classifying a result, the retry schedule |
| `gateway.py` | the only place that spawns processes, locks files, reads clocks and touches the filesystem outside the store |
| `flush.py` | due rows, digests, delivery |
| `history.py`, `hooks.py`, `cron.py`, `log.py` | `history`, `post-fail`, the cron block, `log.jsonl` |

Dependencies: `pyyaml` (YAML cannot be read from the standard library) and `pydantic` (the design guide's config validation); everything else is the standard library (`argparse`, `sqlite3`, `zoneinfo`, `subprocess`, `selectors`, `fcntl`, `hashlib`, `json`). No HTTP client is needed: the router never speaks to a network.

## Data model

See [Files](interface.md#files). Migrations are versioned and additive. The queue row carries rendered text only until its delivery, expiry or drop. A destination's identity in every table is its name; renaming a destination in config starts a fresh history for it.

## Concurrency

Any number of `send` and `flush` processes may run at once. The decision step is one `BEGIN IMMEDIATE` transaction with a 5 second busy timeout. The send step is serialized per destination by `flock`. A `flush` claims rows with a lease; a second `flush` for the same destination sees its claims and reports `already_running`. Exact PIDs the router started are the only processes it signals, as process groups it created.

## Error handling

Failures are values: every call ends in one of the outcomes of [Retries and outcomes](interface.md#retries-and-outcomes), recorded in `history` and `log.jsonl`. A state problem warns and never changes the result of a send, except that a database that cannot be opened is exit 1 `internal` with nothing sent. Config errors fail fast with the key named and never the value.

## Test strategy

- Every subprocess, lock and clock use goes through `gateway.py`; tests substitute fakes. Fake notifiers live in `tests/fakebin/` and can exit with any code, print any envelope, sleep, ignore SIGTERM, spawn a grandchild, close stdin early and write oversized output.
- `--json` outputs have golden files and are validated against `schemas/`; the sibling notifiers' error envelope and `send` schemas are copied into `tests/` at a pinned version.
- Process tests run in CI: group kill with a grandchild, the bounded reader, `flock` serialization across 8 processes including a SIGKILLed holder, concurrent `send` and `flush` claiming each row once, a killed `flush` re-claimed after its lease, the dedup statement under 8 writers.
- Spike regressions: the window brute force as a property test; the strict reader cases; the template non-re-expansion case; bad time zone keys.
- `live` tests (deselected, never in CI): the real notifier trio, a marked test to the user's own Slack direct messages only.
- `make verify` runs ruff, pyright, pytest and markdown lint. CI is Linux only; macOS is checked locally.

## Architecture changes (v1.24)

Section 5.2 entry (binding B, commands, state, hook), section 15 and the section 20 entry (decided constraints replace the seed text; the dedup key), section 10.4 (`--source`), section 7.7 (the cron block), design guide sections 5, 6 and 12 (deviation notes and the stale "channel tools escape markup" sentence), the interface registry row and the roadmap hook candidate `post-fail`. Recorded in the private cross-cutting log.
