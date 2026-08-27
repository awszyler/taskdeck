from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_fastapi_instrumentator import Instrumentator

if TYPE_CHECKING:
    from fastapi import FastAPI


def setup_metrics(app: FastAPI) -> None:
    """Attach Prometheus HTTP metrics to the FastAPI app and expose /metrics."""
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
