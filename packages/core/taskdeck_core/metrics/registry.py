"""Custom Prometheus metrics for Taskdeck core.

All `ccpt_*` metrics live here. The framework-level HTTP metrics come from
`prometheus-fastapi-instrumentator` and the Python runtime metrics come from
`prometheus_client` defaults — neither needs to be re-declared.

Single declaration site keeps label cardinality reviewable in one place.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

TASKS_BY_STATUS = Gauge(
    "ccpt_tasks_by_status",
    "Current task count by status (refreshed periodically from the database).",
    ["status"],
)

TASK_STATE_TRANSITIONS_TOTAL = Counter(
    "ccpt_task_state_transitions_total",
    "Task state machine transitions, including admin overrides.",
    ["from_status", "to_status", "actor"],
)

RUNNERS_CONNECTED = Gauge(
    "ccpt_runners_connected",
    "Currently connected CRP runners.",
)

LLM_CALL_DURATION_SECONDS = Histogram(
    "ccpt_llm_call_duration_seconds",
    "LLM API call latency by kind and provider.",
    ["kind", "provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)

COST_EVENTS_TOTAL = Counter(
    "ccpt_cost_events_total",
    "Cost events recorded, by provider and model.",
    ["provider", "model"],
)

INTENT_PARSE_ATTEMPTS_TOTAL = Counter(
    "ccpt_intent_parse_attempts_total",
    "Intent parse loop attempts by outcome (one increment per attempt).",
    # outcome: high_conf | low_conf | invalid_agent | timeout | json_error
    #        | schema_error | heuristic
    ["attempt", "outcome"],
)
