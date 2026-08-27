# taskdeck-sandbox-host

Per-host sandbox provisioning + reverse proxy for taskdeck.

## Role

When a user clicks "Open sandbox" on a completed task, core's
`POST /api/v1/sandbox/{task_id}/start` calls into this service via
the docker-compose internal network. This service:

1. Detects what to run (auto-detect + `sandbox.yml`).
2. Spawns a gVisor-isolated docker container with the agent's workspace
   mounted at `/workspace`.
3. Waits for the user app to listen on its declared port.
4. Forwards public traffic from `/sandbox/{task_id}/*` to the container.
5. Reaps idle containers after 5 min of no traffic.

Auth is **not** done here — Caddy must `forward_auth` to core before
proxying us. We trust the docker-compose network boundary.

## Running locally

```bash
uv sync --package taskdeck-sandbox-host
uv run taskdeck-sandbox-host
# listens on :9101
```

For local dev without gVisor:

```bash
TD_SBH_CONTAINER_RUNTIME=runc uv run taskdeck-sandbox-host
```

## Status

- [x] P6.3.1.1: gVisor verified on Tokyo
- [x] P6.3.1.2: Scaffolding (this scaffold)
- [ ] P6.3.1.3: Detection logic (sandbox.yml + auto-detect)
- [ ] P6.3.1.4: Docker provisioning
- [ ] P6.3.1.5: Reverse proxy
- [ ] P6.3.1.6: Idle GC
- [ ] P6.3.1.7: Sandbox base images
- [ ] P6.3.1.8: Tests + manual smoke test

## Settings (env vars)

All keyed `TD_SBH_*`. See `sandbox_host/settings.py` for the full list.

| Var | Default | Notes |
|---|---|---|
| `TD_SBH_PORT` | 9101 | HTTP listen port |
| `TD_SBH_WORK_DIR` | /var/taskdeck/work | Mount root for sandbox /workspace |
| `TD_SBH_CONTAINER_RUNTIME` | runsc | "runc" for local dev |
| `TD_SBH_CPU_LIMIT` | 1.0 | Per-sandbox vCPU |
| `TD_SBH_MEMORY_LIMIT_MB` | 1024 | Per-sandbox RAM |
| `TD_SBH_IDLE_SECONDS` | 300 | Idle reclaim threshold |
| `TD_SBH_MAX_CONCURRENT` | 4 | Soft cap on concurrent sandboxes |
