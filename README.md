# Taskdeck

> A kanban for your AI agents. Self-hosted; bring your own runtimes.

Your AI agents (claude-code, hermes, kiro, AgentCore, …) are scattered across IM bots, terminals, and untracked notebook tabs. **Taskdeck gives them a kanban**: file a task by voice or text, watch it run, see the diff, retry it.

> **Disclaimer** — a personal project, **not an AWS product or solution**, not affiliated with or endorsed by any employer, and provided without official support. MIT-licensed, as-is; review it yourself before running it anywhere that matters.

## What it is

- A **web kanban** for tasks across multiple agent runtimes (shell, [Claude Code](https://docs.anthropic.com/claude/docs/claude-code), [Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/)).
- A **bot you can DM** in WeCom (Lark / Telegram on the roadmap) to file tasks from your phone.
- A **scheduler with cross-agent dependencies** — task B waits for A, gets A's diff/output as part of its prompt.
- **Self-hosted**: runs on your own server, talks to your existing LLM endpoint via [LiteLLM](https://github.com/BerriAI/litellm).

## What it isn't

- **Another agent.** Bring your own — Taskdeck just orchestrates.
- **Hosted SaaS.** (Maybe one day.)
- **A team-only tool.** Single-user works fine (auth off by default). Multi-user via AWS Cognito is opt-in.

## Quickstart (5 minutes)

```bash
git clone https://github.com/awszyler/taskdeck.git
cd taskdeck
docker compose -f docker-compose.demo.yml up
```

First run takes ~3 minutes because the web image does a Node build. Subsequent ups reuse the cache and start in ~10 seconds.

Open <http://localhost:5173>. Type a request, watch it run.

(Optional) seed sample tasks:

```bash
docker compose -f docker-compose.demo.yml exec core python /app/scripts/seed-demo.py
```

For real deployment (production server, WeCom, multi-user, etc.) see [docs/tier3-deployment.md](docs/tier3-deployment.md).

For hacking on the code (live reload), see [CONTRIBUTING.md](CONTRIBUTING.md).

## What's inside

| Layer | Stack | Why |
| --- | --- | --- |
| Web (kanban + voice + intent parser + admin) | Vite, React 19, TypeScript strict, shadcn/ui, Tailwind, Recharts | Modern, fast, dark by default |
| Core | FastAPI + SQLAlchemy 2 async + Postgres + pgvector | Boring & reliable |
| Runner | Python daemon, plug-in agents (`shell`, `claude-code`, `agentcore-<id>`) | Decoupled from core; runs on your hardware |
| Wire protocol | Custom WS protocol, "CRP" | Reverse-WS so the runner connects out, not in (NAT-friendly) |
| Memory | `.taskdeck/CONTEXT.md` injection (RAG planned) | Project-level cold-start kill |
| LLM access | Anything OpenAI-compatible (via LiteLLM) | Bring your own model |
| Auth (optional) | AWS Cognito (hosted UI) + server-side sessions | `TD_AUTH_MODE=disabled` by default; flip to `cognito` for multi-user |
| Cost tracking | Per-call `cost_events`, admin dashboard with daily breakdown | See where your tokens go |

## Production deployment

`docs/tier3-deployment.md` walks through the AWS deployment we use:

- **EC2** runs core + Postgres + Caddy + runner.
- **CloudFront → ALB → Caddy → core** (no public ports on EC2 except SSH).
- **WeCom** for IM access (with full encrypted callback support).
- **S3** for daily Postgres dumps via systemd timer.

See also `docs/runbook.md` for operational procedures (restore from backup, rotate secrets, etc).

## Status

Shipped: kanban + multi-runtime dispatch, claude-code with git worktrees, voice input + intent parser,
WeCom bot, cross-agent task dependencies with output injection, AgentCore runner, multi-tenancy,
cost tracking + audit log, and partial hardening (rate limiting, metrics, backups).

Planned: RAG memory layer, remaining hardening work, CI.

## Known issues

- **CI is red on lint and type-check.** `pytest` (469 passed) and the web build both pass, but
  `ruff check .` reports 81 findings and `pyright` reports 16 — mostly unused imports, `TYPE_CHECKING`
  moves, and simplification hints accumulated over time. Known and not yet cleaned up; they don't
  affect runtime behaviour.
- `docker-compose.production.yml` is a template — it expects `TD_RUNNER_BEARER_TOKEN` and friends from
  `.env.production` (copy `.env.production.example`). `docker compose config` warns until you provide them.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Designs first, code second.

## License

MIT — see [LICENSE](LICENSE).
