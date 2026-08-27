# Changelog

All notable changes to Taskdeck. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is [SemVer](https://semver.org/) once we ship `v1.0.0`.

## [Unreleased]

### Added — P5.1 Cognito + MFA + signup gate
- **AWS Cognito User Pool BFF auth.** GitHub OAuth removed entirely.
  New `TD_AUTH_MODE=cognito`; `disabled` mode unchanged for
  single-user / local dev.
- **TOTP MFA enforced at the user-pool level.** First-login wizard:
  Cognito returns `MFA_SETUP` → backend `AssociateSoftwareToken` →
  frontend renders QR + code input → `VerifySoftwareToken` activates
  the device. SMS MFA explicitly disabled.
- **SRP password flow.** Cleartext password is reduced to a proof in
  the browser via `amazon-cognito-identity-js`'s `AuthenticationHelper`;
  the password never crosses a network boundary or reaches the backend.
- **Server-side opaque session cookie.** `HttpOnly` + `Secure` +
  `SameSite=Strict`. Browser holds nothing reusable. Encrypted Cognito
  refresh + access tokens live in `user_sessions` (rebuilt schema;
  Fernet ciphertext via `TD_SESSION_ENCRYPTION_KEY`).
- **Signup gate.** `TD_AUTH_ALLOW_SIGNUP` (default `false`) hides the
  Sign Up link, makes `/auth/signup` return 403, and the runbook
  recommends keeping the Cognito pool's own `SelfSignUp` OFF as
  defense-in-depth.
- **WebSocket cookie auth.** `/api/v1/ws` reads the session cookie;
  closes with code 1008 on missing/invalid/expired session.
- **`GET /api/v1/auth/config`** — public, returns `{auth_mode,
  allow_signup}`. Frontend boot reads this once to decide what to render.
  Critically does **not** expose pool/client IDs (BFF — frontend doesn't
  need them).
- **New API surface:** `/auth/login/{init,respond,totp,mfa-setup,
  new-password}`, `/auth/signup{,-confirm,-resend}`,
  `/auth/password/{forgot,reset}`, `/auth/logout` (with Cognito
  `GlobalSignOut`), `/auth/me`. All rate-limited via slowapi.
- **Migration 0011:** drop `users.github_id`, add `cognito_sub` (partial
  unique index), drop the old `sessions` table, recreate
  `user_sessions` with encrypted-token columns + INET ip + user_agent.
- **Runbook §10 / §11 / §12:** Cognito User Pool setup, key rotation,
  toggling self-signup. `.env.example` + `.env.production.example`
  updated.

### Removed
- GitHub OAuth code paths: `auth/github.py`, `/auth/github/{start,
  callback}`, `users.github_id`, `TD_GITHUB_*` settings,
  `TD_SESSION_SECRET_KEY` (replaced by `TD_SESSION_ENCRYPTION_KEY`).

### Added
- **P4.1 design** for public release polish.
- **P4.0 hardening (in flight):** body-size middleware, slowapi rate limit on `/auth/*`, Prometheus `/metrics` endpoint on core + runner, daily `pg_dump` to S3 (bucket `<backup-bucket>`).
- **P3.4 cost tracking + audit (in flight):** `cost_events` + `audit_events` tables, `Pricing` module with default per-model USD rates, `CostEventSink` + `AuditEventSink` subscribed to EventBus, instrumented Intent Parser + STT calls, `/api/v1/costs/summary`, `/api/v1/costs/events`, `/api/v1/audit` admin APIs, `/admin` UI tab in the web app.

## [v1.6.0] — 2026-05-15

Async intent parsing + agent loop + idempotency (Phase 5.0).

### Added
- New `TaskStatus.PARSING` bootstrap state for raw_input async tasks. Legal transitions: `parsing → {pending, draft, cancelled}`.
- Migration 0010: `tasks.idempotency_key UUID` + partial unique index on `(workspace_id, idempotency_key) WHERE idempotency_key IS NOT NULL`. Old tasks unaffected.
- `IntentParseLoop`: two LLM attempts + heuristic terminal layer. Each attempt's prompt is **adapted to the specific failure type** (invented agent / json error / schema error / timeout / transport). Low confidence (< 0.7) does NOT trigger retry — it's a truthful signal and bypasses to draft for review.
- `POST /api/v1/tasks` accepts a new `raw_input` async path alongside the structured form. Server creates a parsing-state task and runs the loop in the background. Same workspace + idempotency_key on retry returns the same task.
- `PATCH /api/v1/tasks/{id}` for editing draft tasks (title, prompt, agent, repo).
- `task.parse_started` and `task.parsed` EventBus events so the UI can update in real time.
- `ccpt_intent_parse_attempts_total{attempt, outcome}` Prometheus counter for retry/fallback observability.
- Web UI: `NewTaskPage` collapsed to single-step submit (textarea + mic + Submit, Cmd/Ctrl+Enter shortcut). Per-mount `idempotency_key` UUID so double-clicks don't dupe.
- Web UI: BoardView implicit `parsing` column (only renders when at least one task is in flight). Skeleton card with spinner echoes the original raw_input.
- Web UI: Low-confidence draft cards get a ⚠️ icon, warning border, and "click to review" hint. Click opens `ReviewDraftDialog` for quick inline edit + submit.

### Changed
- `TaskCreateBody` made the structured-form fields optional so the same endpoint serves both modes. Validation: exactly one of `prompt` / `raw_input` must be set.
- `TaskOut` now exposes `raw_input` and `intent_confidence` so the kanban can render skeleton/review affordances.

### Fixed
- `task_events.reason` now preserves the `TaskFailed.detail` string from the runner (truncated to 500 chars), so postmortem doesn't require grepping systemd logs.

### Verified live
- High-conf coding (`refactor auth middleware`) → claude-code 0.8 → auto-pending → runner picked up → done.
- Vague input (`do something`) → claude-code 0.1 → draft (no auto-submit), title politely rendered as `Vague request: "do something"`.
- ASCII shell command (`echo idempotency-test`) → shell 0.98 → done.
- Explicit kiro (`用 kiro 帮我修...`) → kiro-cli 0.9 → auto-pending → runner running.
- Idempotency: same `(workspace_id, idempotency_key)` second POST returns the same task id.
- Mutual exclusion: POST with both `raw_input` and `prompt` → 400 with descriptive detail.

## [v1.5.0] — 2026-05-15

Runtime-aware intent parsing (Phase 4.3).

### Added
- `Hello.capability_descriptions: dict[str, str]` (optional). Bumps `PROTO_VERSION` to `2.3`. Old runners still validate.
- `RunnerHub.available_capabilities()` returns `[{capability, description}]`, deduplicated across runners.
- `IntentParser.parse(input, *, available_capabilities=...)` builds the tool schema enum and the system prompt fresh per request, listing each runtime with its description. Empty caps short-circuit with `confidence=0` and "no runners connected" reason; LLM not invoked.
- Defensive: if the LLM hallucinates an agent value outside the enum, parser downgrades to `shell` with `confidence=0` and surfaces the violation.
- Tasks API setting `TD_REJECT_OFFLINE_AGENTS` (default off): when enabled, returns 400 if `body.agent` has no connected runner.
- `kiro-cli` runtime: new `agents/kiro_cli.py` invoking `kiro-cli chat --no-interactive --trust-all-tools <prompt>`, ANSI stripping, runs in the same git worktree flow as claude-code. New `TD_KIRO_CLI_BIN` runner setting.
- `TD_AGENTCORE_AGENT_DESCRIPTIONS` (JSON: `{"agent_id": "human description"}`) so AgentCore agents can be routed by the parser based on task fit.
- WeCom callback now reads `RunnerHub` and threads capabilities into `handle_free_text` the same way the REST handler does.

### Changed
- Removed the hard-coded `["shell", "claude-code"]` agent enum and the misleading "short prompt → shell, otherwise → claude-code" heuristic. Routing is now driven by which runtimes are connected and their descriptions.

### Verified live
- `git fetch origin main` → `agent=shell, conf=0.95`
- 用 claude code 写贪吃蛇+部署 → `agent=claude-code, conf=0.9`
- 用 kiro 修 apps/web 失败的测试 → `agent=kiro-cli, conf=0.9`
- refactor auth middleware → `agent=claude-code, conf=0.85` (默认)

## [v1.4.1] — 2026-05-15

### Changed
- Intent parser switched from LiteLLM/qwen3-coder to Bedrock Claude Opus 4.7 (cross-region inference profile, us-east-1). Tightened tool schema (closed enum) and system prompt; default timeout 5s → 30s.

## [v1.4.0] — 2026-05-15

Observability stack (Phase 4.2).

### Added
- `ccpt_*` business metrics on `/metrics`: `ccpt_tasks_by_status`, `ccpt_task_state_transitions_total`, `ccpt_runners_connected`, `ccpt_llm_call_duration_seconds`, `ccpt_cost_events_total`. Single `taskdeck_core.metrics.registry` module so cardinality stays auditable.
- 30s background refresher in `lifespan` populating `ccpt_tasks_by_status`.
- Prometheus + Grafana services in `docker-compose.production.yml`. Both stay on the private compose network; Grafana exposed only on `127.0.0.1:3001` via the override file. Operator access via SSH local-forward (matches the "no public ports beyond CloudFront + SSH:22" rule).
- Provisioned datasource (`taskdeck-prom`), 9-panel "Taskdeck Overview" dashboard, and 4 alert rules (`core_down`, `http_5xx_burst`, `task_failure_rate_high`, `embed_p95_slow`).
- `tier3-deployment.md` "Observability" section covering env, bring-up, SSH-tunnel access, scrape verification.

## [v1.3.1] — 2026-05-15

### Fixed
- Cohere embed v3 retrieval ranking: pass `input_type=search_query` for queries (vectors are not symmetric with `search_document`). Without this, similarity search returned essentially random ordering.

## [v0.7.0-p3.1] — 2026-05-15

Multi-tenancy.

### Added
- GitHub OAuth login flow (`/api/v1/auth/github/start`, `/callback`, `/me`, `/logout`).
- Sessions, `workspace_members`, `workspace_invites` tables (migration 0005).
- Auth middleware with `current_principal` dependency; bearer token still works for runner traffic.
- Query scoping: tasks/workspaces/intent/stt routes filter by workspace membership.
- Bootstrap-ownership endpoint for first-run setup.
- Web UI: `LoginPage`, top-nav user menu.
- `docs/phase3-1-migration.md` describing how to flip `TD_AUTH_MODE` from `disabled` to `github`.

### Behaviour
- `TD_AUTH_MODE=disabled` (default): no gating, single-user mode unchanged.
- `TD_AUTH_MODE=github`: full multi-tenant. UI redirects to GitHub on first hit.

## [v0.6.0-p2.2] — 2026-05-14

Dependency input injection.

### Added
- `dependency_outputs` field on `TaskAssignPayload`.
- Core-side injector that pulls parent artifacts (`git-diff`, `git-branch`, `decision`) and ships them with the dispatch; truncated at 10 KB per artifact / 50 KB total.
- Runner renders the injected outputs as `<dependency-outputs>…</dependency-outputs>` in the task prompt for non-shell agents.

## [v0.5.0-p2.1] — 2026-05-14

Task dependencies (cross-agent orchestration).

### Added
- New `blocked` task state.
- `task_dependencies` table (migration 0004) + `TaskDependency` ORM.
- `POST /api/v1/tasks` accepts `depends_on`.
- `DependencyResolver` subscribed to EventBus; advances blocked children when all parents reach `done`, cascades cancel when a parent fails.
- Web UI: dependency multi-select on new-task page; ⛓ indicator on kanban cards.

## [v0.4.0-m4] — 2026-05-14

WeCom IM integration + project context injection.

### Added
- WeCom callback (`/api/v1/im/wecom/callback`): AES-256-CBC decrypt, signature verification, bind / status / cancel commands.
- Outbound `WecomClient` for replies + completion notifications via the WeCom Server API.
- `/bind <code>` flow: web UI mints a 6-char code, user DMs the bot, identity link created.
- Free-text DMs route through Intent Parser, create `origin=im` tasks.
- `EventBus.subscribe_callback` so `WecomNotifier` can fan out terminal-state notifications.
- Runner injects `.taskdeck/CONTEXT.md` / `AGENTS.md` / `CLAUDE.md` (first-wins) into claude-code prompts.

### Fixed
- `5143037` — initialise IntentParser eagerly in lifespan so first WeCom free-text call doesn't no-op.
- `b83025f` — skip context injection for shell agent (XML wrapper would break a shell command).
- `a0b60e6` — `git fetch --force` on bare-clone reuse.
- `3c1dafc` — write `intent_parse_log` row from WeCom free-text path.
- `ff2356d` — Intent Parser system prompt strips leading `run`/`execute` verbs from shell prompts.

## [v0.3.0-m3] — 2026-05-14

Voice + text + Intent Parser.

### Added
- `IntentParser` module + `OpenAIClient` over LiteLLM.
- `POST /api/v1/intent/parse` and `POST /api/v1/stt` endpoints.
- `/tasks/new` page with push-to-talk mic, Web Speech API primary, Whisper fallback, parse-then-edit-then-submit flow.
- `intent_parse_log` table records every parse for later eval.
- Live LLM path verified against the user's qwen3-coder-30b LiteLLM endpoint.

### Fixed
- `479cb90` — Qwen3 XML tool-call format support + `confidence_reasons` string-to-list coercion.
- `f683781` — Intent Parser default timeout bumped from 3s to 5s after live testing showed 2 s+ latencies.

## [v0.2.0-m2] — 2026-05-14

claude-code + git worktree + workspaces API.

### Added
- `Workspace` runner abstraction: per-task git worktree, bare-clone management, artifact collection (`git-diff`, `git-branch`).
- `ClaudeCodeExecutor` runner agent.
- `POST /api/v1/internal/artifacts` + `LocalFSArtifactStore` for persisting per-task artifacts.
- SQLAlchemy engine moved into FastAPI lifespan; orphan-task recovery on startup.
- `POST /api/v1/workspaces` / `GET /api/v1/workspaces` + workspace picker UI.
- AgentRuntime protocol (`shell` and `claude-code` implementations).

## [v0.1.0-m1] — 2026-05-14

Skeleton.

### Added
- FastAPI core + Postgres via `docker-compose.yml`.
- Runner daemon connecting over WebSocket (CRP, "Taskdeck Runner Protocol").
- Task state machine: `draft → pending → running → done/failed/cancelled`.
- React kanban table with WS live updates.
- Initial Alembic migration (workspaces / users / runners / tasks / events / logs / artifacts).
- Five-tier release strategy.
