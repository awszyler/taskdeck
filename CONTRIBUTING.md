# Contributing to Taskdeck

Thanks for considering a contribution. This is a one-person project at the moment, kept tidy in case it grows.

## Dev setup

See the README "Quickstart" section. The TL;DR for hacking on backend:

```bash
docker compose up -d postgres
make install
cd packages/core && uv run alembic upgrade head && cd ../..
DATABASE_URL=postgresql+asyncpg://taskdeck:taskdeck@localhost:5432/taskdeck \
TD_RUNNER_BEARER_TOKEN=dev \
uv run --package taskdeck-core uvicorn taskdeck_core.main:app --reload --port 8000
```

For frontend:

```bash
cd apps/web && pnpm install && pnpm dev
```

## Where things live

- `packages/proto/` — shared Pydantic types for the runner protocol (CRP).
- `packages/core/` — FastAPI service: REST + WebSockets + Postgres ORM + dispatcher + Intent Parser + memory + auth + cost tracking.
- `packages/runner/` — long-running daemon that connects to core over CRP and runs tasks (shell, claude-code, AgentCore).
- `apps/web/` — Vite + React + shadcn/ui dashboard.
- `docs/runbook.md` — operations: backups, restores, common failures.
- `docs/tier3-deployment.md` — production deploy guide (CloudFront + ALB + EC2).
- `deploy/` — systemd units + backup scripts.

## Pre-PR checklist

```bash
# All four must pass cleanly:
uv run ruff check .
uv run pyright    # set DATABASE_URL + TD_RUNNER_BEARER_TOKEN env if needed
uv run pytest     # set the same env vars
cd apps/web && pnpm build
```

The repo has GitHub Actions that runs the same checks.

## Designs

Significant changes warrant a design doc under `docs/`. Pattern:

1. **§1 Why this milestone exists** — what pain it solves.
2. **§2 Scope (In / Out)** — explicit non-goals matter.
3. **§3 Contract changes** — DB schema, API shape, settings.
4. **§4–5 Implementation sketches** — the parts where the design has actual choices.
5. **§7 Sub-milestones** — bite-sized work units.
6. **§N Known holes deferred** — what we explicitly punt.

Copy structure from any existing design doc.

## Style

- Python: enforced by `ruff` (config in root `pyproject.toml`).
- TypeScript: strict mode + `noUncheckedIndexedAccess`; the build is the source of truth.
- Comments: keep them sparse, prefer explanatory variable names. When you do comment, explain *why* not *what*.
- Commits: conventional-ish. `feat(scope): summary`. Body in the imperative. Reference design / sub-milestone (P2.2.3 etc.) when relevant.

## License

MIT. By contributing you agree your code is MIT-licensed.
