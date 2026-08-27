import pytest
from taskdeck_core.state.machine import (
    TERMINAL,
    IllegalTransition,
    TaskStatus,
    can_transition,
    next_status,
)


def test_parse_failed_can_resubmit_to_pending():
    assert can_transition(TaskStatus.PARSE_FAILED, TaskStatus.PENDING)


def test_parse_failed_can_cancel():
    assert can_transition(TaskStatus.PARSE_FAILED, TaskStatus.CANCELLED)


def test_parse_failed_to_running_is_illegal():
    assert not can_transition(TaskStatus.PARSE_FAILED, TaskStatus.RUNNING)


def test_pending_can_start_to_running():
    assert can_transition(TaskStatus.PENDING, TaskStatus.RUNNING)


def test_running_to_done_allowed():
    assert can_transition(TaskStatus.RUNNING, TaskStatus.DONE)


def test_running_to_failed_allowed():
    assert can_transition(TaskStatus.RUNNING, TaskStatus.FAILED)


def test_running_to_cancelled_allowed():
    assert can_transition(TaskStatus.RUNNING, TaskStatus.CANCELLED)


def test_pending_to_cancelled_allowed():
    assert can_transition(TaskStatus.PENDING, TaskStatus.CANCELLED)


def test_done_can_rerun_to_pending():
    """Rerun action on a completed card transitions DONE → PENDING."""
    assert can_transition(TaskStatus.DONE, TaskStatus.PENDING)


def test_done_cannot_go_to_other_states():
    """DONE only escapes via the rerun path (→ PENDING). Everything else
    is illegal; cancelling a done task is not a workflow we support."""
    for target in TaskStatus:
        if target is TaskStatus.PENDING:
            continue
        assert not can_transition(TaskStatus.DONE, target)


def test_cancelled_is_terminal():
    for target in TaskStatus:
        assert not can_transition(TaskStatus.CANCELLED, target)


def test_failed_can_rerun_to_pending():
    """System-level FAILED (orphan recovery, runner crash) can be retried
    via the same rerun path. User-facing failures route through IN_REVIEW
    instead — see crp/handler.py."""
    assert can_transition(TaskStatus.FAILED, TaskStatus.PENDING)


def test_failed_cannot_go_to_other_states():
    for target in TaskStatus:
        if target is TaskStatus.PENDING:
            continue
        assert not can_transition(TaskStatus.FAILED, target)


def test_next_status_raises_on_illegal():
    with pytest.raises(IllegalTransition):
        next_status(TaskStatus.PARSE_FAILED, TaskStatus.RUNNING)


def test_next_status_returns_target_on_legal():
    assert next_status(TaskStatus.PENDING, TaskStatus.RUNNING) is TaskStatus.RUNNING


def test_blocked_to_pending_allowed():
    assert can_transition(TaskStatus.BLOCKED, TaskStatus.PENDING)


def test_blocked_to_cancelled_allowed():
    assert can_transition(TaskStatus.BLOCKED, TaskStatus.CANCELLED)


def test_parse_failed_cannot_go_to_blocked():
    # parse_failed re-routes only to pending (resubmit) or cancelled.
    assert not can_transition(TaskStatus.PARSE_FAILED, TaskStatus.BLOCKED)


def test_blocked_is_not_terminal():
    assert TaskStatus.BLOCKED not in TERMINAL


def test_running_cannot_go_to_blocked():
    assert not can_transition(TaskStatus.RUNNING, TaskStatus.BLOCKED)


def test_blocked_cannot_go_to_running_directly():
    assert not can_transition(TaskStatus.BLOCKED, TaskStatus.RUNNING)


def test_terminal_states_cannot_enter_blocked():
    for t in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        assert not can_transition(t, TaskStatus.BLOCKED)


# --- Phase 5.6: IN_REVIEW state ---


def test_running_to_in_review_legal():
    """Agent-level failures (TaskFailed message) route through IN_REVIEW
    so the user can decide to retry or abandon."""
    assert can_transition(TaskStatus.RUNNING, TaskStatus.IN_REVIEW)


def test_in_review_to_done_legal():
    """User approves the result via /tasks/{id}/approve."""
    assert can_transition(TaskStatus.IN_REVIEW, TaskStatus.DONE)


def test_in_review_to_pending_legal():
    """User reruns from review via /tasks/{id}/rerun."""
    assert can_transition(TaskStatus.IN_REVIEW, TaskStatus.PENDING)


def test_in_review_to_cancelled_legal():
    """User abandons the failed task via /cancel."""
    assert can_transition(TaskStatus.IN_REVIEW, TaskStatus.CANCELLED)


def test_in_review_to_running_illegal():
    """Re-execution always goes back through PENDING so the dispatcher
    re-claims the task fresh; never skip the queue."""
    assert not can_transition(TaskStatus.IN_REVIEW, TaskStatus.RUNNING)


def test_in_review_is_not_terminal():
    assert TaskStatus.IN_REVIEW not in TERMINAL


def test_running_to_done_still_legal_after_in_review():
    """Successful completion still bypasses IN_REVIEW (trust agent)."""
    assert can_transition(TaskStatus.RUNNING, TaskStatus.DONE)


# --- Phase 5.0: PARSING state ---


def test_parsing_to_pending_allowed():
    """High-confidence parse auto-submits."""
    assert can_transition(TaskStatus.PARSING, TaskStatus.PENDING)


def test_parsing_to_parse_failed_allowed():
    """Parser error / crash lands in parse_failed for edit & resubmit."""
    assert can_transition(TaskStatus.PARSING, TaskStatus.PARSE_FAILED)


def test_parsing_to_cancelled_allowed():
    """User can cancel a stuck parse; orphan recovery uses this too."""
    assert can_transition(TaskStatus.PARSING, TaskStatus.CANCELLED)


def test_parsing_cannot_go_directly_to_running():
    """Parse loop must finalize via PENDING (auto-submit dispatch path), never skip."""
    assert not can_transition(TaskStatus.PARSING, TaskStatus.RUNNING)


def test_parsing_cannot_go_to_done_or_failed():
    """No agent has executed yet — terminal completion states are illegal."""
    assert not can_transition(TaskStatus.PARSING, TaskStatus.DONE)
    assert not can_transition(TaskStatus.PARSING, TaskStatus.FAILED)


def test_parsing_is_not_terminal():
    assert TaskStatus.PARSING not in TERMINAL


def test_no_state_can_re_enter_parsing():
    """PARSING is bootstrap-only — once parsed, you can't go back to it."""
    for t in TaskStatus:
        if t is TaskStatus.PARSING:
            continue
        assert not can_transition(t, TaskStatus.PARSING)


# --- Phase 5.4: AWAITING_INPUT state ---


def test_running_to_awaiting_input_legal():
    assert can_transition(TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT)


def test_awaiting_input_to_pending_legal():
    assert can_transition(TaskStatus.AWAITING_INPUT, TaskStatus.PENDING)


def test_awaiting_input_to_cancelled_legal():
    assert can_transition(TaskStatus.AWAITING_INPUT, TaskStatus.CANCELLED)


def test_awaiting_input_to_done_illegal():
    assert not can_transition(TaskStatus.AWAITING_INPUT, TaskStatus.DONE)


def test_running_to_done_still_legal():
    """Existing transition unchanged by adding awaiting_input."""
    assert can_transition(TaskStatus.RUNNING, TaskStatus.DONE)
