"""The example payloads the shipped rules are rendered against (#30).

Each ``Example`` is one hook payload or channel notice and the rule that must decide it (``None``:
no rule, exit 22 ``no_rule``). Every shipped rule needs at least one example that reaches it.

agent-spawn payloads are derived from the pinned examples under ``tests/sibling-schemas/`` (a
recorded version, validated against the pinned hook schemas): the pinned file is copied and only
the fields a rule keys on are overridden. The other producers' schemas are not pinned (their
tools are not pinned here), so their payloads are written here, small, with the fields their rules
read and the shapes of docs/interface.md; they are checked by the render check alone. All values
are placeholders (example.com names, invented ids). Payload field names avoid the words the
clear-text analysis flags.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from tests.sibling_schemas import hook_examples


@dataclass(frozen=True)
class Example:
    name: str
    payload: dict[str, Any]
    rule: str | None
    # (tool, schema name) of the pinned schema the payload must satisfy, None when not pinned.
    schema: tuple[str, str] | None = None


def _spawn(base: str, **overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(hook_examples("agent-spawn")[base])
    payload.update(overrides)
    return payload


def _escalate(needs_attention: str) -> dict[str, Any]:
    payload = _spawn("post-escalate", needs_attention=needs_attention, outcome=None)
    payload["agent"]["needs_attention"] = needs_attention
    return payload


def _session_reported() -> dict[str, Any]:
    """A live session that reported: the state ``agent-done`` keys on (not a final state)."""
    return _spawn(
        "post-state-change-session",
        state="reported",
        previous_state="working",
        final=False,
        outcome=None,
        end_reason=None,
        exit_code=None,
    )


_PR = "github.example.com/example-org/example-repo#12"
_SPAWN = ("agent-spawn", "hooks/post-escalate.json")
_STATE = ("agent-spawn", "hooks/post-state-change.json")


def examples() -> list[Example]:
    pinned = hook_examples("agent-spawn")
    launch = {**copy.deepcopy(pinned["post-escalate"]), "hook": "post-launch"}
    return [
        # agent-spawn: the pinned examples as shipped, and the ones derived for each rule.
        Example("pinned post-escalate", pinned["post-escalate"], "agent-escalated", _SPAWN),
        Example("pinned post-state-change agent", pinned["post-state-change-agent"], None, _STATE),
        Example(
            "pinned post-state-change session (final)",
            pinned["post-state-change-session"],
            None,
            _STATE,
        ),
        Example("post-launch", launch, "launch-drop"),
        Example("session reported", _session_reported(), "agent-done", _STATE),
        Example("escalate failed", _escalate("failed"), "agent-failed", _SPAWN),
        Example("escalate lost", _escalate("lost"), "agent-lost", _SPAWN),
        Example(
            "escalate launch_failed", _escalate("launch_failed"), "agent-launch-failed", _SPAWN
        ),
        Example("escalate escalated", _escalate("escalated"), "agent-escalated", _SPAWN),
        # review-watch
        Example(
            "review draft",
            {
                "tool": "review-watch",
                "hook": "post-draft",
                "event_id": 31,
                "object_id": "rv-example1",
                "draft_id": "dr-example1",
                "pr": _PR,
                "tier": "standard",
            },
            "review-draft-ready",
        ),
        *(
            Example(
                f"review attention {reason}",
                {
                    "tool": "review-watch",
                    "hook": "post-attention",
                    "event_id": 32,
                    "object_id": "rv-example1",
                    "pr": _PR,
                    "reason": reason,
                },
                rule,
            )
            for reason, rule in (
                ("escalated", "review-escalated"),
                ("failed", "review-failed"),
                ("stalled", "review-attention"),
            )
        ),
        # ticket-work
        Example(
            "ticket attention",
            {
                "tool": "ticket-work",
                "hook": "post-attention",
                "event_id": 33,
                "object_id": "tw-example1",
                "site": "example-site",
                "key": "EX-123",
                "reason": "blocked",
                "phase": "build",
            },
            "ticket-attention",
        ),
        Example(
            "ticket pending",
            {
                "tool": "ticket-work",
                "hook": "post-pending",
                "event_id": 34,
                "object_id": "tw-example1",
                "site": "example-site",
                "key": "EX-123",
                "action": "transition",
            },
            "ticket-pending",
        ),
        Example(
            "ticket start failed",
            {
                "tool": "ticket-work",
                "hook": "post-start-failed",
                "event_id": 35,
                "object_id": "tw-example1",
                "site": "example-site",
                "key": "EX-123",
            },
            "ticket-start-failed",
        ),
        # jobsctl
        Example(
            "job held",
            {"tool": "jobsctl", "hook": "post-held", "event_id": 36, "object_id": "job-example1"},
            "job-held",
        ),
        Example(
            "job failed (final)",
            {
                "tool": "jobsctl",
                "hook": "post-fail",
                "event_id": 37,
                "object_id": "job-example1",
                "job": {"status": "failed"},
            },
            "job-failed",
        ),
        Example(
            "job failed (retry scheduled)",
            {
                "tool": "jobsctl",
                "hook": "post-fail",
                "event_id": 38,
                "object_id": "job-example1",
                "job": {"status": "queued"},
            },
            None,
        ),
        # pr-watch
        Example(
            "pr approved",
            {"tool": "pr-watch", "hook": "on-approved", "event_id": 39, "pr": _PR},
            "pr-approved",
        ),
        Example(
            "pr changes requested",
            {"tool": "pr-watch", "hook": "on-changes_requested", "event_id": 40, "pr": _PR},
            "pr-changes-requested",
        ),
        # channel notices (no producer): the pass-through rule
        Example(
            "notice error",
            {
                "title": "Disk almost full",
                "message": "The example volume is at 95%.",
                "level": "error",
            },
            "direct-errors",
        ),
        Example(
            "notice info (below the pass-through level)",
            {"message": "Backup finished.", "level": "info"},
            None,
        ),
    ]
