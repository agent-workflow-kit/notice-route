# notice-route

Routes notifications to channel notifiers by source, labels, level and time, with quiet hours, deferral, dedup, a rate cap, retries and a history.

## Status

Designed, not yet implemented: the contract is [docs/interface.md](docs/interface.md) (0.1.0-design) and the reasoning is in [docs/design.md](docs/design.md).

## Layer

L2 (composite). Works fully standalone; it does not need the `awkit` harness.

## What it does

A producer's hook (or a script) sends one JSON object on stdin. notice-route finds the first rule that matches it, renders a title, message and link from the rule's template (or, for a pass-through rule, sends a notice's own title and message), decides per destination whether to send now, defer (quiet hours or the hourly cap), drop or suppress a repeat, and calls the notifier for each destination to send now. Held notices (quiet hours, the cap) are delivered as one digest per destination by `flush`, which also retries failures and delivers anything queued without its credential. Everything it did is in `history`.

- Two input shapes, told apart by keys: a hook payload (has string `tool` and `hook`) or a channel notice (`title`, `message`, `level`, `link`).
- Rules are an ordered list; the first match wins. A rule names destinations, a level and templates, or drops the notification.
- Levels are ordered `info < warn < action < error`. The rule sets the level. In a destination's quiet window `info` is dropped, `warn` and `action` are deferred and `error` is delivered at once.
- Payload text never reaches a title: only word-like values (letters, digits and `._/#-`) do. Free text reaches a message only when a rule opts in and the payload says it is not third-party or tainted.
- Links are built only from your own host tables, never from a payload field.
- It never sends a message to a destination it was not configured for, and never calls a notifier's `--test-message`.

## Commands

Specified in [docs/interface.md](docs/interface.md#commands).

- `notice-route send`: route one notification
- `notice-route rules`: list the rules
- `notice-route test`: show what `send` would do for a payload, without sending
- `notice-route flush`: deliver held and single rows and run the `post-fail` hook (run from cron)
- `notice-route history --json`: what was sent, deferred, dropped or failed
- `notice-route validate [--probe]`: check the config offline, optionally ask each notifier to validate itself
- `notice-route prune`, `notice-route cron install|uninstall`

## Exit codes

0 every destination handled, 2 usage or config, 12 a destination failed or its outcome is unknown, 20 refused, 22 nothing to do. See [Exit codes](docs/interface.md#exit-codes).

## Configuration

`~/.config/notice-route/config.yaml` plus `config.d/*.yaml`, found only from your home directory in the password database (an environment variable cannot redirect it). The directory and files must be owned by you and not group- or world-writable. [config.example.yaml](config.example.yaml) is a complete worked configuration, and [examples/awkit-wiring.yaml](examples/awkit-wiring.yaml) is the matching `config.d` drop-in with the rules for the awkit producers. CI checks both: they validate as one config offline, and every rule renders against example payloads it matches (`tests/test_example_config.py`); a placeholder that renders missing or excluded fails the build. Credentials are passed only through the variables a destination names; a send that runs without them is delivered by `flush` from cron, whose env file must hold them (an empty value counts as unset).

## Wiring a producer

One line in the producer's own config, for example `notice-route send --source agent-spawn --stdin-json` on its `post-state-change` hook. A hook runner that knows only zero and non-zero should add `--exit-zero-on-noop`, so a no-op (exit 22) counts as success. Wire either agent-spawn's or claude-session's state hooks to notifications, not both. Never allow `notice-route send` or a notifier in an agent session's permission profile; a session's events reach the router through agent-spawn's and claude-session's hooks.

| Producer | Hook | Command |
|---|---|---|
| agent-spawn | `post-escalate` | `notice-route send --source agent-spawn --stdin-json` |
| agent-spawn | `post-state-change` | `notice-route send --source agent-spawn --stdin-json` |
| agent-spawn | `post-launch` | `notice-route send --source agent-spawn --stdin-json` |
| review-watch | `post-draft` | `notice-route send --source review-watch --stdin-json` |
| review-watch | `post-attention` | `notice-route send --source review-watch --stdin-json` |
| ticket-work | `post-attention` | `notice-route send --source ticket-work --stdin-json` |
| ticket-work | `post-pending` | `notice-route send --source ticket-work --stdin-json` |
| ticket-work | `post-start-failed` | `notice-route send --source ticket-work --stdin-json` |
| jobsctl | `post-held` | `notice-route send --source jobsctl --stdin-json` |
| jobsctl | `post-fail` | `notice-route send --source jobsctl --stdin-json` |
| pr-watch | `on-approved` | `notice-route send --source pr-watch --stdin-json` |
| pr-watch | `on-changes_requested` | `notice-route send --source pr-watch --stdin-json` |

## Hooks

`post-fail`, run by `flush` after a destination's failure was surfaced; see [Hooks](docs/interface.md#hooks).

## Environment variables

None are read for configuration; a destination's `env` names the credential variables passed to its notifier; `NOTICE_ROUTE_HOOK` is the recursion guard.

## Dependencies

- A notifier command per destination: [slack-notice](https://github.com/agent-workflow-kit/slack-notice), [ha-notice](https://github.com/agent-workflow-kit/ha-notice) or [wsl-notice](https://github.com/agent-workflow-kit/wsl-notice), or any command that accepts `send --stdin-json --json --actor <a> [--dedupe-key=<hex>]` and reads the notification JSON on stdin. Binding B (configurable command). Install the ones you use:

  ```sh
  uv tool install git+https://github.com/agent-workflow-kit/slack-notice
  uv tool install git+https://github.com/agent-workflow-kit/ha-notice
  uv tool install git+https://github.com/agent-workflow-kit/wsl-notice
  ```

- SQLite 3.35 or newer in Python's `sqlite3` module (older libraries lack `RETURNING`; commands that write state refuse to open the state database with them; read-only commands still work).
- `cron` for `flush` and `prune` (`notice-route cron install`).

A missing notifier ends its calls `failed`, code `exec_failed`; `validate` names the destination, and the commands above install it.

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/notice-route
```

## Documentation

- [Interface](docs/interface.md) and [design](docs/design.md)
- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
