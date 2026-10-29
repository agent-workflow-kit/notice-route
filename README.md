# notice-route

Routes notifications to channel tools by level, source, labels and time.

## Status

Pre-design: interface not yet specified; see [docs/design.md](docs/design.md).

## Layer

L2 (composite). Works fully standalone; it does not need the `awkit` harness.

## What it does

- `send --stdin-json` reads either a hook payload (mapped to title and message by rule templates) or the channel-tool JSON shape; takes `--source <tool>` and repeatable `--label`. Plain `--title`/`--message` remain for trusted human use.
- Always calls channel tools with `send --stdin-json`.
- Rules match on level, source, labels and time and fan out to channels.
- Levels are ordered `info < warn < action < error`; handles quiet hours and dedup; deferred notifications are delivered by `flush`.

## Indicative commands

Indicative, finalized in [docs/interface.md](docs/interface.md).

- `notice-route send`
- `notice-route rules`
- `notice-route test`
- `notice-route flush`
- `notice-route history --json`

## Dependencies

- configured notifier commands — binding C (adapters, per architecture §3.3/§5.2); adapter details decided in the design session

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/notice-route
```

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
