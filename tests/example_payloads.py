"""The example payloads the shipped rules are rendered against (#30).

Each ``Example`` is one hook payload or channel notice and the rule that must decide it (``None``:
no rule, exit 22 ``no_rule``). Every shipped rule needs at least one example that reaches it.

agent-spawn payloads are derived from the pinned examples under ``tests/sibling-schemas/`` (a
recorded version, validated against the pinned hook schemas): the pinned file is copied and only
the fields a rule keys on are overridden. The other producers' schemas are not pinned here (and
this repo's docs/interface.md does not define their payloads), so their payloads are authored
here in the shape of each producer's own hook schema and are covered by the render check alone
(``schema`` is ``None``). The sibling file each shape was taken from:

- review-watch: ``docs/interface.md`` hooks section and ``schemas/hooks/post-draft.json``,
  ``schemas/hooks/post-attention.json`` (``tier`` is an integer 0..3, ``reason`` one of the
  attention reasons, round ids ``rw-<8 base32>#<n>``, draft ids 26-character ULIDs, a string
  ``event_id`` of the form ``<object id>-<hook>-<n>``).
- ticket-work: ``docs/interface.md`` hooks section and ``schemas/hooks/post-attention.json``,
  ``post-pending.json``, ``post-start-failed.json`` (work ids ``<site>:<KEY>``, ``phase`` and
  ``reason`` from the real enums, a string ``event_id``).
- jobsctl: ``docs/interface.md`` hooks section and ``schemas/hooks/post-held.json``,
  ``post-fail.json`` (job ids ``jb-<8 base32>``, the full ``hook_job`` object and the attempt
  object, a string ``event_id``).
- pr-watch: ``docs/interface.md`` hooks section and ``schemas/hooks/on-approved.json``,
  ``on-changes_requested.json`` (``ev-<12 base32>`` event ids, ``id`` equal to ``event_id``,
  every required field of the event).
- agent-spawn ``post-launch``: ``schemas/hooks/post-launch.json`` (not pinned: only the
  post-escalate and post-state-change schemas are), built from the pinned post-escalate example
  with the fields that schema requires and none of the ones it forbids.

All values are placeholders (example.com names, invented ids). Payload field names avoid the
words the clear-text analysis flags.
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


def _launch() -> dict[str, Any]:
    """A post-launch payload in the shape of agent-spawn's schemas/hooks/post-launch.json."""
    payload = _spawn(
        "post-escalate",
        hook="post-launch",
        status="live",
        needs_attention=None,
        run=1,
        resumed=False,
        fork=False,
        promoted=False,
        auto_resumed=False,
    )
    del payload["outcome"], payload["chain_failure"]
    payload["agent"]["status"] = "live"
    payload["agent"]["needs_attention"] = None
    return payload


def _parked(needs_attention: str) -> dict[str, Any]:
    """A post-park payload (a hook no shipped rule routes) with the same attention value."""
    return {**_escalate(needs_attention), "hook": "post-park"}


_HOST = "github.example.com"
_REPO = f"{_HOST}/example-org/example-repo"
_PR = f"{_REPO}#12"
_SHA = "0123456789abcdef0123456789abcdef01234567"
_SPAWN = ("agent-spawn", "hooks/post-escalate.json")
_STATE = ("agent-spawn", "hooks/post-state-change.json")


def _review(hook: str, event: int, **fields: Any) -> dict[str, Any]:
    """A review-watch payload for round rw-abcdefgh#1 (the real labels, ids and event id)."""
    item, round_id = "rw-abcdefgh", "rw-abcdefgh#1"
    return {
        "tool": "review-watch",
        "hook": hook,
        "object_type": "round",
        "object_id": round_id,
        "repo": _REPO,
        "labels": [f"rw-item={item}", "rw-round=1", f"pr={_PR}"],
        "event_id": f"{round_id}-{hook}-{event}",
        "pr": _PR,
        "item": item,
        "n": 1,
        "head_sha": _SHA,
        **fields,
    }


def _details(**fields: Any) -> dict[str, Any]:
    base = {
        "kind": None,
        "failure_reason": None,
        "retryable": None,
        "stale_reason": None,
        "host": None,
        "consecutive_failures": None,
        "code": None,
        "waiting": None,
    }
    return {**base, **fields}


def _review_attention(reason: str) -> dict[str, Any]:
    details = {
        "escalated": _details(kind="session"),
        "failed": _details(failure_reason="session_failed", retryable=False),
    }
    return _review("post-attention", 1, reason=reason, details=details.get(reason, _details()))


def _review_item_attention(reason: str) -> dict[str, Any]:
    """round_limit and daily_limit are about an item, not a round."""
    item = "rw-abcdefgh"
    payload = _review("post-attention", 1, reason=reason, details=_details(waiting=1))
    payload.update(object_type="item", object_id=item, n=None, event_id=f"{item}-post-attention-1")
    payload["labels"] = [f"rw-item={item}", f"pr={_PR}"]
    return payload


def _review_poll_failed() -> dict[str, Any]:
    """A host-level attention: no repo, PR, item or labels."""
    return {
        "tool": "review-watch",
        "hook": "post-attention",
        "object_type": "host",
        "object_id": _HOST,
        "repo": None,
        "labels": [],
        "event_id": f"{_HOST}-post-attention-1",
        "pr": None,
        "item": None,
        "n": None,
        "head_sha": None,
        "reason": "poll_failed",
        "details": _details(host=_HOST, consecutive_failures=3),
    }


def _ticket(hook: str, event_id: str, **fields: Any) -> dict[str, Any]:
    """A ticket-work payload for ticket EX-123 on site example-site (the common fields)."""
    return {
        "tool": "ticket-work",
        "hook": hook,
        "event_id": event_id,
        "object_type": "work",
        "object_id": "example-site:EX-123",
        "repo": _REPO,
        "labels": ["ticket=EX-123", "owner=ticket-work", f"repo={_REPO}", "board=main"],
        "board": "main",
        "site": "example-site",
        "key": "EX-123",
        "phase": "working",
        "actor": "auto:ticket-work",
        "decision_ids": [],
        **fields,
    }


_JOB_ID = "jb-abcdefgh"


def _job(status: str, **fields: Any) -> dict[str, Any]:
    """The jobsctl hook_job object: every required field, consistent with its status."""
    job: dict[str, Any] = {
        "id": _JOB_ID,
        "queue": "default",
        "status": status,
        "reason": None,
        "hold_reason": None,
        "argv": ["/usr/bin/example-job", "--once"],
        "cwd": "/home/example/work",
        "pass_env": [],
        "labels": ["ticket=EX-123"],
        "unique_key": None,
        "priority": 0,
        "pinned": False,
        "rank": None,
        "attempt": 1,
        "retried_from_attempt": None,
        "overrides": {},
        "effective": {"timeout": None, "max_retries": 2, "kill_grace": 10},
        "not_before": None,
        "blocked_by": None,
        "force_requested_by": None,
        "override_hooks": [],
        "shim_lost": False,
        "created_at": "2026-11-20T10:15:30.000Z",
        "enqueued_at": "2026-11-20T10:15:30.000Z",
        "status_changed_at": "2026-11-20T10:16:30.000Z",
        "claimed_at": "2026-11-20T10:15:31.000Z",
        "ended_at": None,
        "actor": "human:example",
        "decision_ids": [],
        "exit": None,
        "leftovers": None,
        "resolved_path": None,
        "resolved_by": None,
        "note": None,
        "env_names": [],
    }
    job.update(fields)
    return job


def _attempt(status: str, **fields: Any) -> dict[str, Any]:
    attempt: dict[str, Any] = {
        "attempt": 1,
        "status": status,
        "reason": None,
        "hold_reason": None,
        "claimed_at": "2026-11-20T10:15:31.000Z",
        "running_at": "2026-11-20T10:15:32.000Z",
        "ended_at": "2026-11-20T10:16:30.000Z",
        "exit": None,
        "leftovers": None,
        "resolved_path": "/usr/bin/example-job",
        "effective": {"timeout": None, "max_retries": 2, "kill_grace": 10},
        "escalated_hooks": [],
        "vetoed_by": None,
        "hook_messages": [],
        "decision_ids": [],
        "force_requested_by": None,
        "resolved_by": None,
        "note": None,
        "jobsctl_version": "0.36.0",
    }
    attempt.update(fields)
    return attempt


def _job_event(hook: str, event_id: str, previous: str, job: dict[str, Any], attempt: Any) -> Any:
    return {
        "tool": "jobsctl",
        "hook": hook,
        "event_id": event_id,
        "object_type": "job",
        "object_id": _JOB_ID,
        "repo": None,
        "labels": ["ticket=EX-123"],
        "previous_status": previous,
        "job": job,
        "attempt": attempt,
        "output_available": False,
        "output_tail": None,
    }


_EXIT_1 = {"kind": "exited", "code": 1, "signal": None, "core": False}
_EXIT_13 = {"kind": "exited", "code": 13, "signal": None, "core": False}


def _job_held() -> dict[str, Any]:
    job = _job("held", hold_reason="exit_13", exit=_EXIT_13, leftovers=0)
    job["resolved_path"] = "/usr/bin/example-job"
    attempt = _attempt("held", hold_reason="exit_13", exit=_EXIT_13, leftovers=0)
    return _job_event("post-held", "ev-job-0001", "running", job, attempt)


def _job_failed(final: bool) -> dict[str, Any]:
    ended = "2026-11-20T10:16:30.000Z"
    exit_fields = {
        "reason": "exit",
        "exit": _EXIT_1,
        "leftovers": 0,
        "resolved_path": "/usr/bin/example-job",
    }
    attempt = _attempt("failed", ended_at=ended, **exit_fields)
    if final:
        job = _job("failed", ended_at=ended, **exit_fields)
        return _job_event("post-fail", "ev-job-0002", "running", job, attempt)
    # A retry is scheduled: the job is queued again and the attempt that failed is reported.
    job = _job(
        "queued",
        rank=1,
        claimed_at=None,
        not_before="2026-11-20T10:21:30.000Z",
        blocked_by="not_before",
    )
    return _job_event("post-fail", "ev-job-0003", "running", job, attempt)


def _pr_event(kind: str, seq: int, **fields: Any) -> dict[str, Any]:
    """A pr-watch event payload (hook on-<type>) with every field the schema requires."""
    event = f"ev-abcdefghijk{'ab'[seq % 2]}"
    return {
        "tool": "pr-watch",
        "hook": f"on-{kind}",
        "object_type": "pr",
        "object_id": "example-pr-12",
        "event_id": event,
        "id": event,
        "seq": seq,
        "type": kind,
        "pr": _PR,
        "pr_id": "example-pr-12",
        "repo": _REPO,
        "detected_at": "2026-11-20T10:15:30.123456Z",
        "occurred_at": "2026-11-20T10:15:00.000000Z",
        "labels": [],
        "head_sha": _SHA,
        "watches": ["w-abcdefghij"],
        "reviewer": {"id": "example-account-1", "login": "example-reviewer", "is_bot": False},
        "commit_sha": _SHA,
        "at": "2026-11-20T10:15:00.000000Z",
        "can_push": True,
        **fields,
    }


def examples() -> list[Example]:
    pinned = hook_examples("agent-spawn")
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
        # post-launch.json is not pinned: built from its schema, covered by the render check.
        Example("post-launch", _launch(), "launch-drop", None),
        Example("session reported", _session_reported(), "agent-done", _STATE),
        Example("escalate failed", _escalate("failed"), "agent-failed", _SPAWN),
        Example("escalate lost", _escalate("lost"), "agent-lost", _SPAWN),
        Example(
            "escalate launch_failed", _escalate("launch_failed"), "agent-launch-failed", _SPAWN
        ),
        Example("escalate escalated", _escalate("escalated"), "agent-escalated", _SPAWN),
        # Right source, other hook: post-park is routed by no rule.
        *(
            Example(f"agent-spawn park {value}", _parked(value), None)
            for value in ("failed", "lost", "launch_failed", "escalated")
        ),
        Example(
            "agent-spawn reported (other hook)", {**_session_reported(), "hook": "post-park"}, None
        ),
        # Right hook, other source: a rule that lost its source condition would match these.
        Example("other source post-launch", {**_launch(), "tool": "ticket-work"}, None),
        Example(
            "other source post-state-change",
            {**_session_reported(), "tool": "claude-session"},
            None,
        ),
        *(
            Example(
                f"other source post-escalate {value}", {**_escalate(value), "tool": "jobsctl"}, None
            )
            for value in ("failed", "lost", "launch_failed", "escalated")
        ),
        # review-watch
        Example(
            "review draft",
            _review(
                "post-draft",
                1,
                draft_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
                tier=2,
                verdict="comment",
                severity_counts={
                    "blocker": 0,
                    "major": 1,
                    "minor": 2,
                    "nit": 0,
                    "info": 0,
                    "unspecified": 0,
                },
                coverage_status="full",
            ),
            "review-draft-ready",
        ),
        Example(
            "review draft (other source)",
            {
                **_review("post-draft", 1, draft_id="01ARZ3NDEKTSV4RRFFQ69G5FAV", tier=2),
                "tool": "x",
            },
            None,
        ),
        Example("review post (other hook)", _review("post-post", 1), None),
        *(
            Example(f"review attention {reason}", _review_attention(reason), rule)
            for reason, rule in (
                ("escalated", "review-escalated"),
                ("failed", "review-failed"),
                ("stale", "review-attention"),
            )
        ),
        Example(
            "review attention round_limit",
            _review_item_attention("round_limit"),
            "review-attention",
        ),
        Example("review attention poll_failed", _review_poll_failed(), "review-poll-failed"),
        # The reasons the rules key on, with another source and with another hook.
        *(
            Example(f"review attention {reason} ({kind})", {**payload, **change}, None)
            for reason, payload in (
                ("escalated", _review_attention("escalated")),
                ("failed", _review_attention("failed")),
                ("poll_failed", _review_poll_failed()),
            )
            for kind, change in (
                ("other source", {"tool": "jobsctl"}),
                ("other hook", {"hook": "post-post"}),
            )
        ),
        # ticket-work
        Example(
            "ticket attention",
            _ticket(
                "post-attention",
                "ev-tw-0001",
                reason="agent_blocked",
                dedupe_key="example-site:EX-123:agent_blocked:1",
            ),
            "ticket-attention",
        ),
        Example(
            "ticket pending",
            _ticket(
                "post-pending",
                "ev-tw-0002",
                pending_id="pd-abcdefgh",
                action="transition",
                event="pr_opened",
                summary="Move EX-123 to In Review.",
                expires="2026-11-21T10:15:30Z",
                dedupe_key="pd-abcdefgh",
            ),
            "ticket-pending",
        ),
        Example(
            "ticket start failed",
            _ticket(
                "post-start-failed",
                "ev-tw-0003",
                reason="launch_failed",
                restore_state="none",
                dedupe_key="example-site:EX-123:start_failed:1",
            ),
            "ticket-start-failed",
        ),
        Example(
            "ticket status (other hook)",
            _ticket("post-status", "ev-tw-0004", event="pr_opened"),
            None,
        ),
        Example(
            "ticket attention (other source)",
            {**_ticket("post-attention", "ev-tw-0005", reason="agent_blocked"), "tool": "jobsctl"},
            None,
        ),
        Example(
            "ticket pending (other source)",
            {**_ticket("post-pending", "ev-tw-0006", action="start"), "tool": "pr-watch"},
            None,
        ),
        Example(
            "ticket start failed (other source)",
            {**_ticket("post-start-failed", "ev-tw-0007"), "tool": "pr-watch"},
            None,
        ),
        # jobsctl
        Example("job held", _job_held(), "job-held"),
        Example("job failed (final)", _job_failed(True), "job-failed"),
        Example("job failed (retry scheduled)", _job_failed(False), None),
        Example("job held (other source)", {**_job_held(), "tool": "pr-watch"}, None),
        Example("job finished (other hook)", {**_job_failed(True), "hook": "post-finish"}, None),
        Example("job failed (other source)", {**_job_failed(True), "tool": "pr-watch"}, None),
        # pr-watch
        Example("pr approved", _pr_event("approved", 1, approvals_at_head=1), "pr-approved"),
        Example("pr changes requested", _pr_event("changes_requested", 2), "pr-changes-requested"),
        Example("pr merged (other hook)", _pr_event("merged", 3), None),
        Example(
            "pr approved (other source)",
            {**_pr_event("approved", 4, approvals_at_head=1), "tool": "jobsctl"},
            None,
        ),
        Example(
            "pr changes requested (other source)",
            {**_pr_event("changes_requested", 5), "tool": "jobsctl"},
            None,
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
