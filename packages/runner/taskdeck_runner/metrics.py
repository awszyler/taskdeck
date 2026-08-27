from __future__ import annotations

from prometheus_client import Counter, Histogram, start_http_server

TASKS_TOTAL = Counter(
    "ccpt_runner_tasks_total",
    "Total tasks processed by this runner",
    ["agent", "exit_status"],
)
TASK_DURATION_SECONDS = Histogram(
    "ccpt_runner_task_duration_seconds",
    "Task end-to-end runtime seconds",
    ["agent"],
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 300, 900, 3600, 7200),
)


def start_metrics_server(port: int = 9100) -> None:
    start_http_server(port, addr="127.0.0.1")
