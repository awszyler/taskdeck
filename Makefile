.PHONY: install dev test fmt lint typecheck compose-up compose-down check

install:
	uv sync --all-packages --dev

dev:
	docker compose up -d postgres
	uv run --package taskdeck-core alembic upgrade head
	uv run --package taskdeck-core uvicorn taskdeck_core.main:app --reload --port 8000

test:
	uv run pytest

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff check .

typecheck:
	uv run pyright

compose-up:
	docker compose up -d

compose-down:
	docker compose down

# make check runs ruff + pyright + pytest.
# Note: the pytest step may show 2 DB-related failures when tests run together
# (test_tasks_api + test_ws_fanout + test_crp_handshake interact via module-
# level asyncpg engine; this is a known M1 limitation). Run affected tests
# individually to verify they pass in isolation. CI handles this by running
# DB-heavy tests in separate invocations with truncate between them.
check:
	uv run ruff check .
	uv run pyright
	uv run pytest
