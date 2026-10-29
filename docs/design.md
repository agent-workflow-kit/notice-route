# notice-route: design

Status: Seeded from awkit architecture v1.4; not yet designed.

## Role and layer

Layer 2 (composite). Routes notifications to channel tools by level, source, labels and time. Works fully standalone (architecture C2).

## Responsibilities (architecture §5.2)

Routes notifications to channels.

- Commands: `send`, `rules`, `test`, `flush`, `history --json`.
- Uses: any configured notifier command (ours or third-party, via adapters).
- Notes: `send --stdin-json` reads either a hook payload (mapped to title and message by rule templates) or the channel-tool JSON shape; it takes `--source <tool>` and repeatable `--label`. Plain `--title`/`--message` remain for trusted human use. It always calls channel tools with `send --stdin-json`. Rules match on level, source, labels and time and fan out to channels. Level order, quiet hours and dedup are in section 15 and section 20; deferred notifications are delivered by `flush`.

Section numbers above refer to `../awkit-docs/architecture.md`.

## Uses

- configured notifier commands — binding C (adapters, per architecture §3.3/§5.2); adapter details decided in the design session

## Cross-cutting sections to read

In `../awkit-docs/architecture.md` (plus `../awkit-docs/design-guide.md` throughout):

- §3.2: coordinating tools bind arbitrary lower tools, including tools we do not own
- §10.4: flow D: attention and notifications
- §13.2: transport rule 2 (`send --stdin-json`); untrusted text never in argv or env
- §13.3: credentials and secrets never in payloads or output
- §15: notifications: producers, direct wiring, levels, templates excluding third-party-reading summaries by default

## Constraints (decided)

From architecture §20: Levels are ordered `info < warn < action < error`; quiet hours drop `info`, defer `warn` and `action` until `notice-route flush`, and deliver `error` immediately; hook-payload sends dedup on source + hook + object id within a window, other sends on source + title; the level comes from the rule template, default `info`; channel tools are always called with `send --stdin-json`; `history --json` lists sent notifications.

## Open topics

From architecture §20:

- rule syntax
- dedup window
- quiet-hours config

## Decisions

| Decision | Rationale | Date |
|---|---|---|

## Questions for the design session

1. What is the rule syntax (matching on level, source, labels, time; templates mapping hook payloads to title/message/level/link; channel fan-out)?
2. What is the default dedup window, and is it configurable per rule?
3. How are quiet hours configured (time zones, days, per-channel overrides)?
