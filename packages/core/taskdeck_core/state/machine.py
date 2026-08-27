from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    PARSING = "parsing"
    PARSE_FAILED = "parse_failed"
    PENDING = "pending"
    BLOCKED = "blocked"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    IN_REVIEW = "in_review"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL: frozenset[TaskStatus] = frozenset(
    {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

_LEGAL: dict[TaskStatus, frozenset[TaskStatus]] = {
    # PARSING is the bootstrap state for raw_input async tasks. The intent
    # parse loop transitions it to PENDING on any successful parse (confidence
    # is recorded but no longer gates — see docs/parsing-ux-rework.md). When
    # the parser itself fails (LLM down, invalid schema, illegal agent), the
    # task goes to PARSE_FAILED so the user can edit & resubmit or cancel —
    # it surfaces in the "Needs you" column instead of being silently retried.
    # CANCELLED supports orphan recovery on core restart and user cancel.
    TaskStatus.PARSING: frozenset(
        {TaskStatus.PENDING, TaskStatus.PARSE_FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.PARSE_FAILED: frozenset({TaskStatus.PENDING, TaskStatus.CANCELLED}),
    TaskStatus.BLOCKED: frozenset({TaskStatus.PENDING, TaskStatus.CANCELLED}),
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    # Agent-level failures route through IN_REVIEW so the user can decide
    # to retry or abandon. System-level failures (orphan recovery on core
    # restart) still go straight to FAILED — they're operator concerns,
    # not product decisions.
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.DONE,
            TaskStatus.IN_REVIEW,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.AWAITING_INPUT,
        }
    ),
    TaskStatus.AWAITING_INPUT: frozenset(
        {TaskStatus.PENDING, TaskStatus.CANCELLED}
    ),
    # IN_REVIEW: user can approve (→ DONE), retry (→ PENDING), or cancel.
    TaskStatus.IN_REVIEW: frozenset(
        {TaskStatus.DONE, TaskStatus.PENDING, TaskStatus.CANCELLED}
    ),
    # DONE → PENDING supports the rerun action on completed cards. The
    # original done state is preserved in task_events.
    TaskStatus.DONE: frozenset({TaskStatus.PENDING}),
    TaskStatus.FAILED: frozenset({TaskStatus.PENDING}),
    TaskStatus.CANCELLED: frozenset(),
}


class IllegalTransition(ValueError):
    pass


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in _LEGAL[current]


def next_status(current: TaskStatus, target: TaskStatus) -> TaskStatus:
    if not can_transition(current, target):
        raise IllegalTransition(f"{current} -> {target} is illegal")
    return target
