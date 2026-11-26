# notice-route: implementation plan

This plan implements [interface.md](interface.md) and [design.md](design.md). It names the stages, their goals and their tests; each GitHub issue is expanded into bite-sized test-first steps when it is picked up. Nothing here restates a rule: the rules are in the interface, and a stage's tests cite the interface heading they check.

## Conventions for every stage

- **Gateway boundary.** Every subprocess, lock, clock read, directory and file access outside the state database goes through `src/notice_route/gateway.py`. Tests replace it with a fake. A module that imports `subprocess`, `fcntl`, `time` or `os` for those purposes directly is a defect.
- **Fakes and pinned sibling schemas.** Fake notifiers live in `tests/fakebin/` and are copied per repo, never shared. A fake can exit with any code, print any envelope or none, sleep, ignore SIGTERM, spawn a grandchild (also one that leaves the process group with `setsid` and holds stdout), close stdin early, write oversized output and be killed by a signal. The notifiers' error envelope and `send` result schemas are copied into `tests/` at a pinned version, with the version recorded; the producers' hook schemas are pinned the same way (with `defs.json` and `notification.json` where they are referenced), and the hook payload examples are written here (producers publish none) and validated against those pinned schemas for the example-config render check.
- **Process tests and live tests.** Process tests (real processes, real `flock`, real SQLite, a fake notifier) run in `make verify` and in CI. `live` tests run the real notifiers and the user's real channels, are deselected by default and never run in CI. A live test sends only a marked test message to the user's own Slack direct messages, a Home Assistant message and a toast. The runbook, variables and manual checklists are in [live-testing.md](live-testing.md); `NOTICE_ROUTE_LIVE=1 make live` runs it, and `tests/test_live_harness.py` proves offline that nothing else does.
- **Golden files.** Every `--json` output has a golden file and is validated against its schema in `schemas/`; the example config and the drop-in are validated against `schemas/config.json` and rendered against example payloads (agent-spawn's derived from its pinned hook examples, the other producers' authored here from their hook schemas; a token that renders missing or excluded fails the build). What ships: `config.example.yaml` (every key, the destinations and the router's own rules) and `examples/awkit-wiring.yaml` (the `config.d` drop-in with the producers' rules). `tests/test_example_config.py` (support: `tests/example_check.py`, `tests/example_payloads.py`) is the check, part of `make verify` and so of CI's `verify` job: it validates both as one config with offline `validate` (only the `flush_stale` warning of a fresh state directory is allowed; never `--probe`), renders every rule with offline `test` against the payloads that rule must match, and fails on a missing or excluded token, a rule no example reaches, an example that matches an unexpected rule, a payload that breaks its pinned schema, and a golden file that breaks its schema (`tests/test_golden_schemas.py`).
- **Spike regressions.** Each live-verified behavior in design.md's table becomes a test: the window brute force as a property test (whole weeks plus every offset-change neighborhood in three zones against the reference rule), the strict reader cases, the template non-re-expansion case, bad time-zone keys and `PYTHONTZPATH`, the process-group kill with a grandchild, the bounded reader, `flock` fairness and release on SIGKILL, the monotonic deadline under a wall-clock jump, and the single-statement dedup under 8 writers.
- **`make verify`** (ruff, pyright, pytest without `live`, markdown lint) passes before every commit and in CI. CI is Linux only; macOS is checked locally.
- **Time and randomness** are injected through the gateway so every test is deterministic (a fake clock for decisions, leases and windows; a fixed token source for claims).
- **No upward awareness.** No code branches on the identity of a producer beyond the payload's `tool` name used as a source.

## Module map

| Module | Stage | Responsibility |
|---|---|---|
| `gateway.py` | 0 | the only place that spawns processes, locks files, reads clocks and touches the filesystem |
| `cli.py` | 0, grows | argparse, commands, exit codes, output |
| `config.py` | 1 | strict YAML load, drop-ins, ownership check |
| `validate.py` | 1 | validation of the merged config into typed objects |
| `payload.py` | 1 | strict JSON reader, shape discrimination, labels |
| `match.py` | 2 | matching and the first-match decision |
| `render.py` | 2 | templates, derived link fields, plain and text values, sanitizing |
| `window.py` | 3 | quiet-hour membership and window ends (pure functions) |
| `store.py` | 3 | SQLite access, migrations, bound parameters |
| `queue.py` | 3 | queue rows, claims, leases, the cap and eviction |
| `dedup.py` | 3 | the repeat and redelivery checks, the lifetime of their records |
| `decide.py` | 3 | the per-destination decision order; calls `dedup.py` for dedup, holds the rate statements |
| `notifier.py` | 4 | building the call, the child process, classification |
| `retry.py` | 4 | the retry schedule and attempt caps |
| `log.py` | 4 | the public `log.jsonl` |
| `flush.py` | 5 | due rows, digests, delivery, heartbeat |
| `history.py` | 5 | the `history` command and `prune` |
| `hooks.py` | 6 | `post-fail` and the alert routing |
| `cron.py` | 6 | the managed crontab block |

## Stage 0: scaffold and gateway

**Goal**: A package that installs, a `--version`, the gateway with its fakes, and the fake notifiers.
**Success Criteria**: `make verify` and CI pass; `notice-route --version` prints; the gateway can spawn a fake notifier in its own process group, read it with a bounded reader and signal its group; `tests/fakebin/` holds the fakes and the pinned sibling schemas.
**Tests**: gateway unit tests against the fakes (group kill with a grandchild, the 64 KiB and 16 KiB caps, EPIPE ignored, the monotonic deadline); the fake clock; import-boundary test that only `gateway.py` imports the process and lock modules.
**Status**: Complete

## Stage 1: configuration and input

**Goal**: Load and validate a config, read and discriminate an input, and run `rules` and an offline `validate`.
**Success Criteria**: [Config](interface.md#config), [Destinations](interface.md#destinations), [Quiet hours](interface.md#quiet-hours), [Input shapes](interface.md#input-shapes) and [Environment variables](interface.md#environment-variables) hold: config and state only from the password-database home; ownership and mode checks; drop-ins append `rules`; strict JSON reader; two shapes; labels; `rules --json` and `validate --json` match their schemas.
**Tests**: golden `rules` and `validate` outputs; every input refusal reason; reader cases (duplicate keys, `NaN`, `1e999`, depth, lone surrogate, size, deadline); config negatives for every `config_invalid` and `config_unsafe` case; the example config and the drop-in validate.
**Status**: Complete

## Stage 2: rules and rendering

**Goal**: Match a rule and render a title, message and link, with `test` showing the result.
**Success Criteria**: [Rules](interface.md#rules), [Levels](interface.md#levels), [Templates](interface.md#templates), [Untrusted text](interface.md#untrusted-text) and [Links](interface.md#links) hold, including pass-through rules, the plain and text value rule, `include_text` with the taint flags, single-pass substitution and the link tables; `test --json` matches its schema.
**Tests**: matching table tests (types, `*`, labels, `where`, `level_min`, unreachable-rule warning); render tests against realistic producer payloads built from the pinned examples; a value containing `{{title}}` is not re-expanded; text never reaches a title or link; the example config renders with no missing or excluded token.
**Status**: Complete

## Stage 3: state and the decision

**Goal**: The database, the quiet windows and the whole decision order of `send`, up to but not including the notifier call.
**Success Criteria**: [Quiet hours](interface.md#quiet-hours), [Deferral and digest](interface.md#deferral-and-digest), [Dedup](interface.md#dedup), [Rate limit](interface.md#rate-limit) and the decision steps of [`send`](interface.md#send) hold; rows and claims follow [Files](interface.md#files); `send --dry-run` and `test` read the database read-only.
**Tests**: the window property test; DST fixtures; the decision order table (one test per decision); the dedup statement under 8 writers; redelivery on id plus repeat digest; record release by `delivery_id`; per-class counters at the UTC hour boundary; the 100-row cap and lowest-level eviction; migrations; a database that cannot be opened is exit 1 with nothing sent.
**Status**: Complete

## Stage 4: calling the notifiers

**Goal**: A full `send`: spawn, serialize, bound, classify and record.
**Success Criteria**: [Calling the notifiers](interface.md#calling-the-notifiers), [Retries and outcomes](interface.md#retries-and-outcomes) and [Exit codes](interface.md#exit-codes) hold: no shell, scrubbed environment, per-destination `flock`, one call per destination, monotonic deadline, group TERM then KILL, bounded output, the outcome table row by row, one exit code per outcome combination, the log lines.
**Tests**: one test per row of the outcome table with a fake notifier; a killed `send` (SIGKILL after the decision) leaves a claimed row that `flush` resends; a SIGTERM before the call releases its claims; lock contention across 8 processes; a descendant that leaves the group cannot hold the router; the worst-case time bound with defaults; `send --json` golden files for every outcome combination; `hook_recursion`.
**Status**: Complete

## Stage 5: flush, retries and history

**Goal**: `flush`, the retry schedule, digests, `prune` and `history`.
**Success Criteria**: [`flush`](interface.md#flush), [`history`](interface.md#history), [`prune`](interface.md#prune) and the digest and retry rules hold: the flush lock, claims per call, the heartbeat, the `env_missing` rows and their wait, expiry, the cap at `flush`, digest lines and order, the attempt caps (3 for `wsl-notice`, 5 otherwise), `lock_busy` counting no attempt.
**Tests**: two `flush` on one destination with 40 due rows send each once; a `send` stalled past its lease (SIGSTOP) and the resend keep `sent`; `prune` and eviction during a call leave claimed rows alone; the digest for 1, 10 and 25 rows; a destination with an unset credential is left untouched and surfaced; the heartbeat age; golden `history` outputs and every filter.
**Status**: Complete

## Stage 6: alerts, cron, probe, integration

**Goal**: Failure alerts and the hook, the cron block, `validate --probe`, and the live and platform checks.
**Success Criteria**: [Hooks](interface.md#hooks), [`cron install` and `cron uninstall`](interface.md#cron-install-and-cron-uninstall) and `validate --probe` hold: surfacing writes a row, the next `flush` routes the alert and runs the hooks, an alert about a failed alert routes nothing further, the managed block is installed and removed idempotently with exact block detection, the probe runs the notifiers' own `validate` without sending.
**Tests**: surfacing slots per code and UTC day; alert routing through a rule for source `notice-route`; `send` refuses that source; cron block golden files and a crafted existing crontab; the probe against fake notifiers; the example config and the drop-in render every rule with no missing or excluded token (`tests/test_example_config.py`); `live` tests against the real trio (marked messages to the user's own channels only); the macOS checklist (cron, `flock`, `zoneinfo`).
**Status**: Complete for the code; the live run (#31) and the macOS checks (#32) are open, see Open issues below

## Version roadmap

- **0.x:** stages 0 to 6, releases cut by the release job from the PR titles. Interface changes before 1.0 are `feat` commits that name the break, with the registry updated.
- **1.0 qualifies when:** the `live` suite passes against the three real notifiers on the author's machine; the macOS checklist is done; the review and ticket slices have run through the router for a period without an interface change; the router's interface is declared stable by a deliberate breaking-change commit.
- **1.x (additive):** `queue` and `destination` management commands (#34, implemented: `queue list|show|drop`, `destination list|enable|disable`, an override table in `state.db` at schema version 2, design C-61); a fallback chain between destinations; a link to the awkit dashboard in notifications; an outbox for `post-fail` (#37, implemented: a `hook_outbox` table at schema version 3 retries synchronous hooks, design C-62); a sliding-window cap if the hourly bucket proves too coarse; more rule conditions only if real use needs them; retry-after hints from notifiers once they report them.
- **2.0:** nothing planned.

## Open issues

Stages 0 to 6 are implemented and released in the 0.x series (see [CHANGELOG.md](../CHANGELOG.md)). The issues still open:

- **#31 (human):** the live suite against the real notifiers. A person reads [live-testing.md](live-testing.md) and runs `NOTICE_ROUTE_LIVE=1 make live`. It sends real test messages to the user's own destinations, so no agent runs it.
- **#32 (human):** the macOS checks (cron, `flock`, `zoneinfo`), listed in [live-testing.md](live-testing.md).
- **#33 (tracking):** the sibling issues and facts this tool relies on. It has no code of its own.
- **#34 to #41 (1.x):** the additive items in the version roadmap. #34 (the queue and destination management commands) is implemented; the others are not started.
