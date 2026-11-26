# Live testing

The live suite in `tests/live/` runs the real `slack-notice`, `ha-notice` and (on WSL) `wsl-notice`, a real shell and real signals, through the router. It settles the items of issue #31 and of [`tests/fakebin/FIDELITY.md`](../tests/fakebin/FIDELITY.md). It is **opt-in, side-effecting and never part of CI or `make verify`**: the `live` marker is deselected by default (`addopts = "-m 'not live'"`), every test under `tests/live/` gets the marker from `tests/live/conftest.py`, and `tests/test_live_harness.py` proves offline that a normal run collects none of them.

Issue #31 stays open until a person has run this and the [macOS checklist](#order-of-the-human-work) (#32). The PR that adds the suite refers to it with `Refs #31`, not `Closes`.

## Safety

- Every message is a test message: the title is `notice-route live test <run id>` and the text starts with `TEST MESSAGE from the notice-route live suite`.
- Messages go only where your own notifier configs send them. The suite never names a channel, host or address; use configs whose destination is your own Slack direct messages, your own phone and your own desktop. Nothing here may reach a shared channel.
- A full run sends 8 planned messages and at most 10 in the worst case. Planned: one `send` per notifier (up to 3), one deferred then flushed message per notifier (up to 3) and two for the signal checks (one notifier). The two extra worst-case messages: the stale-interop test (`wsl-notice` ignores a `WSL_INTEROP` outside `/run/WSL/<digits>_interop` and falls back to the newest valid socket, so on a working WSL host a labelled toast may be delivered, after which the test skips), and the SIGTERM check (the notifier child may deliver after the router is gone, and the follow-up `flush` then sends the row again). The bad-credential test cannot deliver anything; the `ha-notice` one makes one failed call to your own instance (with `ip_ban_enabled` on a very strict instance, run it from an address you can afford to have banned, or skip it with `-k "not bad_credential"`).
- Credentials come only from the environment, in the variable **you name**; the suite never prints one, writes one to a file or the report, or puts one in argv. It hands a credential only to the notifier under the name your notifier config uses. Assertion messages carry names, exit codes and fixed words only, never an output of a notifier or of the router (a leak check computes its result first and asserts it with a fixed text), and every string written to the report passes a redaction step (before it is encoded or cut). The router's own output, log and the raw bytes of `state.db` (with its `-wal` and `-shm`) are checked for the value after a delivery; whether the message text remains in the database is recorded, not asserted. A credential variable cannot be a reserved one (`HOME`, `PATH`, `LANG`, `TMPDIR`, `LC_*`, `WSL_*`, `NOTICE_ROUTE_*`, `XDG_*`). The report directory is created with mode 0700 and an existing one is fixed to 0700 (a symlink or another user's directory is refused).
- The router under test runs with a **private passwd home** in a scratch directory (its config, state and log are never yours). The notifiers still read their own config and keep their own state under your real home, as they do in production; use `NOTICE_ROUTE_LIVE_SLACK_CONFIG` and its siblings to point them at a config made for this run.
- **The suite never runs `crontab`, in any form.** The autouse guard of `tests/conftest.py` applies to live tests unchanged and there is no bypass. The real crontab is a [manual checklist](#manual-crontab-checklist).
- Hooks in the suite are scripts written to the scratch directory. A real hook is never run.
- Never export `NOTICE_ROUTE_LIVE` in your shell profile. Set it inline for the run.

## Prerequisites and variables

A test whose prerequisite is missing is skipped with a reason (`-rs` lists them); nothing fails for a missing prerequisite. `make live` prints which notifiers it found before it runs.

| Variable | Needed for | Meaning |
|---|---|---|
| `NOTICE_ROUTE_LIVE` | everything | Must be exactly `1`. Without it `make live` refuses and every live test skips. |
| `NOTICE_ROUTE_LIVE_REPORT_DIR` | optional | An absolute directory for the observation report. Default: a new private directory under the temp directory; the path is printed at the end of the run. |
| `NOTICE_ROUTE_LIVE_SLACK_BIN` | optional | Absolute path of `slack-notice`; default: found on `PATH`. |
| `NOTICE_ROUTE_LIVE_SLACK_CONFIG` | optional | Absolute path of the notifier's own config (passed as `--config` in the destination's `cmd`); default: its own default config. |
| `NOTICE_ROUTE_LIVE_SLACK_CRED_ENV` | sends | The **name** of the variable that holds the credential your slack-notice config reads (for example the variable its config's credential setting names). The variable itself must be set and non-empty. |
| `NOTICE_ROUTE_LIVE_HA_BIN` | optional | As above for `ha-notice`. |
| `NOTICE_ROUTE_LIVE_HA_CONFIG` | optional | As above for `ha-notice`. |
| `NOTICE_ROUTE_LIVE_HA_CRED_ENV` | sends | The name of the variable that holds the Home Assistant credential your config reads. |
| `NOTICE_ROUTE_LIVE_WSL_BIN` | optional | As above for `wsl-notice` (WSL only). |
| `NOTICE_ROUTE_LIVE_WSL_CONFIG` | optional | As above for `wsl-notice`. |

`wsl-notice` tests also need a WSL session with interop (`WSL_DISTRO_NAME` and `WSL_INTEROP` set); there is no credential for it. Install and configure each notifier first (each has its own `validate`), and use a config made for the test whose destination is your own.

## Run

```sh
read -rs SLACK_LIVE_CRED && export SLACK_LIVE_CRED      # the credential, never typed into a command line
export NOTICE_ROUTE_LIVE_SLACK_CRED_ENV=SLACK_LIVE_CRED
# the same for HA (NOTICE_ROUTE_LIVE_HA_CRED_ENV) and, on WSL, nothing else is needed
NOTICE_ROUTE_LIVE=1 make live
```

`make live` refuses (exit 2) unless `NOTICE_ROUTE_LIVE=1`, prints what it will run (names and yes/no, never values), then runs `pytest -m live tests/live -s -rs -v`. To run one item, run pytest the same way with `-k`, for example `NOTICE_ROUTE_LIVE=1 uv run pytest -m live tests/live -s -rs -k quiet_deferral`. The tests that need no notifier (`live_local`) run with `-m live_local`.

Assertions never depend on elapsed time. The quiet-hours test moves only the **router's own wall clock** (a child-process offset); the notifier sees real time.

## What each test settles

Observation tests record what the real tool did into the report (`live-report-<run id>.json`) and assert only what the router relies on; compare the report with the fake's behavior in `tests/fakebin/FIDELITY.md`.

| Test | Needs | What it observes and asserts |
|---|---|---|
| `test_observe_dry_run_stdout_shape` | each notifier with an enabled, loadable config (a disabled one, exit 22, is a recorded skip) | `send --dry-run` stdout is strict JSON; the line count and trailing newline are recorded; the object against the pinned schema (`ha-notice` must match; `slack-notice` drift is recorded, its pin is stale). |
| `test_observe_usage_errors` | same | Exit code and texts of usage errors (unknown flag, missing subcommand, `--` with empty stdin so nothing can be sent, `--json=1`); asserts exit 2 and an envelope of code `usage`; the line count is recorded. |
| `test_observe_flag_positioning` | same | `--config` and `--actor` before and after the subcommand, on both sides, abbreviations; asserts the router's shape works and abbreviations are refused (exit 2). |
| `test_observe_sanitizer` | same | Messages that sanitize to nothing (zero-width, controls, whitespace, combining marks, variation selectors) and kept ones (NUL, bidi, ZWJ, 5000 characters, astral); `bytes` and `truncated` recorded. Every case is recorded first, then asserted: exit 20 for the empty ones; for `ha-notice` exit 20 `command_keyword` for the command words. |
| `test_observe_config_checked_before_input` | same | A missing config path with a broken payload: asserts `config_invalid` (exit 2) comes first. |
| `test_observe_closed_stdin_and_oversize` | same | Empty stdin; a valid short message padded with JSON whitespace past 1048576 bytes (asserts exit 20); the unpadded oversize message is recorded separately. |
| `test_validate_json_real_output` | each notifier (credential for the two that use one) | The exact stdout and exit code of `validate --json [--probe]`: exit 22 is `{"outcome":"disabled"}`; otherwise strict JSON with a boolean `enabled` and a `checks` list (line count and trailing newline recorded, not asserted). |
| `test_validate_rejects_probe_grammar` | slack, ha | Reads stdout, not the exit code alone: `slack-notice validate --probe` is exit 2 with an envelope of code `usage`; `ha-notice` accepts it (exit 0, 12 or 22, never a usage envelope). An unloadable config (`config_invalid`) is a skip. |
| `test_observe_whitespace_only_credential` | slack, ha, credential | Plain `validate --json` (no `--probe`, so no network call) with a whitespace-only value under the real credential variable name: exit code and output recorded, nothing asserted. `wsl-notice` has no credential. |
| `test_router_validate_probe` | any designated notifier | `notice-route validate --probe --json` against the real destinations: `cmd_ok` (false is a skip, see [A refused notifier path](#a-refused-notifier-path)), and a probe of status `ok`, `failed` or `disabled` with a boolean or null `enabled`. The probes run one after the other, each up to `lock_wait` + `timeout` + 16 s; the test gives the router call 45 s per destination plus 30 s (the default limit of `router.run` is 150 s). |
| `test_send_reaches_the_real_notifier` | each notifier, credential | One marked message through `send`: outcome `sent`, one attempt, code null, history `sent`; every `history --json` row matches `schemas/cli/history.json`; the router's output, log and the raw bytes of `state.db`, `-wal` and `-shm` hold no credential, and the output and log hold no message text (the database may keep it: recorded); then `prune --dry-run --json` exits 0, matches `schemas/cli/prune.json`, changes no history row count and creates no file. |
| `test_quiet_deferral_then_flush_delivers` | each notifier, credential | A `warn` in a quiet window containing now is `deferred` (reason `quiet`), nothing is sent; `flush` with the router clock moved past the window end delivers one message (`sent`). |
| `test_bad_credential_fails_surfaces_and_alerts` | slack or ha, credential | A deliberately invalid value under the real variable name: the call fails (no delivery), the failure is surfaced, the next `flush` routes an alert to a local capture script (never a real channel) and runs a scratch hook; asserts the alert holds only the rule's title, message and level, and the hook's environment names are recorded. |
| `test_wsl_interop_failed_is_retried_three_times` | wsl-notice, WSL | A `WSL_INTEROP` outside `/run/WSL/<digits>_interop` is ignored by the real tool, which falls back to the newest valid socket; a labelled toast may therefore be delivered (counted above). When the tool answers `interop_failed`: asserts `queued_retry` first, then `failed` with `retries_exhausted` after the third call; otherwise skips with a reason that says a message may have been delivered. |
| `test_flusher_signals_with_a_real_notifier` | slack or ha, credential | A stopped flusher keeps the flush lock and a second `flush` reports `already_running`; a terminated flusher either finishes its call or makes none, and the row ends `sent` exactly once. Skips when the lock window is not observed. Sends one message per signal; the follow-up waits (bounded) until the router and its process group are reaped, and a late delivery of the notifier child (its own process group) is the duplicate counted above. |
| `test_sync_hook_environment_and_payload` | nothing (local) | The `post-fail` hook runs through the real `sh -c`: its environment is the documented set plus what `sh` adds itself (recorded; compare on macOS); the payload has no title or message. |
| `test_async_hook_outlives_flush_and_hits_its_timeout` | nothing (local) | An `async` hook's supervisor survives the end of `flush` and ends the hook at its timeout: the hook traps SIGTERM and writes a `termed` file, which must appear after `flush` returned; the order against the return time is recorded. A few seconds. |
| `test_hook_recursion_guard_with_the_real_environment` | nothing (local) | A `send` started from a hook is refused with `hook_recursion`. |

### Not live-testable (and why)

- A real notifier answering with a router-reserved word as `error.code` (`expired`, `overflow`, `env_missing`, `rate_capped`): no real tool does; the offline tests with the fake cover it.
- `retries_exhausted` without a call (a row that escaped repeatedly): needs killed notifiers; covered offline.
- The `SIGSTOP` of a flusher that keeps the **send lock** (as opposed to the flush lock the suite checks), `SIGTERM` to a router that is **waiting for the send lock**, and the `async` hook supervisor on macOS (#32): signal tests that need two real sends in flight; run by hand.
- Whether `wsl-notice` needs `HOME` or `WSLENV` beyond `WSL_INTEROP` and `WSL_DISTRO_NAME`: run `wsl-notice send` through the router on WSL; `test_send_reaches_the_real_notifier` fails if it needs more.

### A refused notifier path

The router refuses a notifier whose resolved path has a directory (or the file) writable by anyone but the owner or root: `validate` reports `cmd_ok` false and a call ends `failed` with code `exec_failed`. The tests that depend on a call (`test_router_validate_probe`, the send, flush, bad-credential and interop tests) then skip with a pointer to this paragraph. Install the notifier where every directory of its path is private to you or root (for example a virtualenv under your home, not a world-writable directory), or point `NOTICE_ROUTE_LIVE_<TOOL>_BIN` at such a copy, and run again.

## Manual crontab checklist

Human only. No test touches a crontab. Back it up first: `crontab -l > ~/crontab.backup` (a message `no crontab for` means there is nothing to back up). Run on the real machine, then again on macOS (#32).

1. `notice-route cron install --dry-run`: check the block text (first line, `PATH=` line, `flush --quiet` every minute, `prune --quiet` at 03:17, end line).
2. `notice-route cron install`, then `crontab -l`: the block is present; repeat the install: `unchanged`.
3. Put a user entry **after** the block and another `SHELL=` line **above** it; run `cron install` again. Observe that the block's `PATH=` applies to every later entry (an update keeps the block in place), and that the `SHELL=` line changes how the block lines run (they need POSIX `sh`).
4. After one minute, `notice-route history --json` and `notice-route validate --json` show `flush_age_s` small (the flush ran from cron, as `auto:unknown`).
5. With the block as the **only** content of the crontab, run `notice-route cron uninstall`. Observe what `crontab -` does with empty stdin (whether the table is removed or left empty), and the exact text of a `crontab -l` on an absent table (`no crontab for <user>`; on macOS too, with `LC_ALL=C`).
6. A crontab larger than 64 KiB: observe the router's read cap (`cron install` must refuse, exit 1).
7. Restore: `crontab ~/crontab.backup` (or `crontab -r` when there was none).

## Other manual items

- The authored agent-spawn hook payload examples (`tests/sibling-schemas/agent-spawn/0.38.0/examples`) against a real producer's output: run a real agent-spawn hook through `notice-route test --stdin-json` (it sends nothing) and compare the shape.
- A notifier's exit code **128+N** when a signal arrives before its send starts (the router relies on it; the fake scripts it), its behavior when **stdout is closed early** (a `send` whose reader goes away), and the exact **SIGTERM envelope after the send started**: run a real notifier by hand with a closed stdout and with `kill -TERM` at different moments, and record the exit codes and outputs on #31. The live suite does not signal a notifier directly because that could send.
- The send-lock and `_wsl.lock` spacing of the notifiers against the router's own `flock`, SIGTERM at the deadline, stdin EPIPE near 1 MiB, and `LANG`/`LC_ALL` `C.UTF-8` against unset: record anything unexpected from the runs above.
- Re-pin the `slack-notice` schemas (`tests/sibling-schemas/slack-notice`, still `0.1.0-design`) from the real 0.25.1 and give the fake a `wsl-notice` shape, using the report as the reference (todo(#31) in FIDELITY.md).

## Recording results

1. The run prints the report path at the end. Keep the report and attach the relevant parts (never anything it redacted) as a comment on issue #31, one line per test: pass, skip with reason, or the difference found.
2. In `tests/fakebin/FIDELITY.md`, change `verified live: pending` to `verified live: <date>, <tool> <version>` for each row that matched, and open an issue for each difference (the fake or the router changes in a follow-up, not in the run).
3. For the manual items, comment the observation on #31 (crontab, macOS) or #32.

## Order of the human work

1. Back up your crontab; configure the notifiers for a test destination of your own.
2. `NOTICE_ROUTE_LIVE=1 make live` on Linux or WSL; record the results (above).
3. The [manual crontab checklist](#manual-crontab-checklist) on the same machine.
4. The macOS checklist of #32 (cron, `flock`, `zoneinfo`, the `sh` environment of the hook and the detached `async` supervisor); `make live` also runs on macOS for the `live_local` and notifier tests there.
5. When all of it is recorded, #31 and #32 can close, and the live suite passing on the author's machine counts toward the 1.0 qualification in [plan.md](plan.md#version-roadmap).
