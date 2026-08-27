# Taskdeck Operational Runbook

**Stack:** EC2 `<server-ip>` · CloudFront → ALB → Caddy (`:8888`) → core (`:8000`) + PostgreSQL  
**Auth mode:** `TD_AUTH_MODE=disabled` by default (single-user). Switch to
`cognito` when adding multi-user via AWS Cognito (see §10–§11).

---

### 1. Emergency access

**When:** SSH broken, key lost, or instance unreachable over TCP 22.

```bash
# Normal SSH
ssh ec2-user@<server-ip>

# SSM Session Manager fallback (no open port needed — requires AWS CLI + ssm plugin)
aws ssm start-session \
  --target $(aws ec2 describe-instances --filters "Name=ip-address,Values=<server-ip>" \
    --query "Reservations[0].Instances[0].InstanceId" --output text) \
  --region ap-northeast-1

# Roll back to the previous release tag (e.g. v0.7.9)
cd /opt/taskdeck
git fetch --tags origin
git reset --hard v0.7.9
docker compose --env-file .env.production \
  -f docker-compose.production.yml \
  -f docker-compose.production.override.yml \
  build --no-cache core
docker compose --env-file .env.production \
  -f docker-compose.production.yml \
  -f docker-compose.production.override.yml \
  up -d core
```

Verify rollback: `curl -sS http://localhost:8888/health` → `{"status":"ok"}`.

---

### 2. Core crashloop

**When:** `docker compose ps` shows core restarting, or `/health` returns 502/503.

```bash
# Tail recent logs
docker compose \
  -f docker-compose.production.yml \
  --env-file .env.production \
  logs --tail=200 core

# If DB migration failed (look for "alembic" errors):
#   Usually a Python syntax error or a missing column.
#   Fix the migration, rebuild, redeploy.

# If LiteLLM creds are wrong (look for "404" or "AuthenticationError" in intent parser):
#   Edit .env.production → fix TD_LITELLM_BASE_URL / TD_LITELLM_API_KEY
#   Then:
docker compose --env-file .env.production \
  -f docker-compose.production.yml \
  -f docker-compose.production.override.yml \
  restart core

# If TD_SESSION_SECRET_KEY is missing (only relevant when AUTH_MODE=github):
#   Add the key to .env.production and restart core.

# Confirm recovery
curl -sS http://localhost:8888/health
```

---

### 3. Runner disconnected

**When:** No runner appears in Settings → Runners, or tasks queue but never start.

```bash
# Check service state
sudo systemctl status taskdeck-runner

# Check recent logs
sudo journalctl -u taskdeck-runner -n 100 --no-pager

# Restart
sudo systemctl restart taskdeck-runner

# After restart, check core logs for orphan recovery (from M2.1):
docker compose -f docker-compose.production.yml --env-file .env.production \
  logs --tail=50 core | grep orphan
# Expect: "marked N orphan task(s) failed"

# Verify runner registered
curl -sS http://localhost:8888/api/v1/internal/runners | python3 -m json.tool
```

If the runner keeps crashing, check `TD_CORE_WS_URL` and `TD_RUNNER_TOKEN` in `.env.runner`
match `TD_RUNNER_BEARER_TOKEN` in `.env.production`.

---

### 4. Postgres full disk

**When:** `docker compose logs core` shows `FATAL: could not write to file` or disk-full errors.

```bash
# Check disk usage
df -h /var/lib/docker

# Vacuum the database (recovers dead tuple space without shrinking files)
docker exec taskdeck-postgres-1 \
  psql -U taskdeck -c "VACUUM FULL;"

# Trim old task logs (safe to delete — they are append-only diagnostic data)
docker exec taskdeck-postgres-1 \
  psql -U taskdeck -c \
  "DELETE FROM task_logs WHERE created_at < now() - interval '14 days';"

# If disk is still critically low — expand the EBS volume online:
#   1. In AWS Console → EC2 → Volumes → select the root volume
#   2. Actions → Modify Volume → increase size → Modify
#   3. On the host (no reboot needed for ext4/xfs):
sudo growpart /dev/nvme0n1 1
sudo resize2fs /dev/nvme0n1p1      # ext4
# or: sudo xfs_growfs /             # xfs (Amazon Linux 2023 default)

df -h /var/lib/docker   # confirm new size
```

---

### 5. CloudFront 5xx

**When:** End users see 502/503/504 via `https://<distribution-id>.cloudfront.net`.

```bash
# Check ALB target health
aws elbv2 describe-target-health \
  --target-group-arn arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:targetgroup/taskdeck-tg/86fbb671d5e38a29 \
  --region ap-northeast-1

# If target is unhealthy — SSH to host and check Caddy → core chain
ssh ec2-user@<server-ip> 'curl -sI http://localhost:8888/health'
# Expect: HTTP/1.1 200 OK

# If Caddy is healthy but core is not:
ssh ec2-user@<server-ip> \
  'docker compose -f /opt/taskdeck/docker-compose.production.yml \
   --env-file /opt/taskdeck/.env.production logs --tail=50 core'

# If it's a transient CloudFront cache issue, invalidate:
aws cloudfront create-invalidation \
  --distribution-id <your-cf-distribution-id> \
  --paths "/*" \
  --region ap-northeast-1
```

---

### 6. WeCom token rotation

**When:** WeCom sends messages but core returns signature errors, or you need to rotate credentials after a suspected leak.

```bash
# 1. In the WeCom admin console (work.weixin.qq.com):
#    Application → Settings → Receive Messages → API Settings
#    Generate a new Token (printable string) and a new EncodingAESKey (click 随机生成).
#    DO NOT click Save yet.

# 2. Update .env.production on the server:
ssh ec2-user@<server-ip> 'nano /opt/taskdeck/.env.production'
# Edit:  TD_WECOM_TOKEN=<new_token>
#        TD_WECOM_AES_KEY=<new_aes_key>

# 3. Restart core so it picks up the new values:
ssh ec2-user@<server-ip> \
  'cd /opt/taskdeck && docker compose --env-file .env.production \
   -f docker-compose.production.yml \
   -f docker-compose.production.override.yml restart core'

# 4. Back in the WeCom console, click Save.
#    WeCom immediately sends a GET verification request; core must return 200.
#    Check: docker compose logs --tail=20 core  (look for "wecom callback verified")
```

---

### 7. LiteLLM quota / budget blown

**When:** Intent parsing fails; tasks are not created from WeCom messages; core logs show 429 or budget errors from LiteLLM.

```bash
# Option A — fail fast while you fix the quota (prevents long hangs):
ssh ec2-user@<server-ip> 'nano /opt/taskdeck/.env.production'
# Set:  TD_INTENT_PARSER_TIMEOUT_SECONDS=1

# Option B — disable LiteLLM entirely and fall back to the rule-based parser:
# Set:  TD_LITELLM_BASE_URL=
# (empty value forces the fallback path; the system stays usable without LLM)

# Restart core after editing:
ssh ec2-user@<server-ip> \
  'cd /opt/taskdeck && docker compose --env-file .env.production \
   -f docker-compose.production.yml \
   -f docker-compose.production.override.yml restart core'

# Restore normal settings once quota is refilled:
# TD_INTENT_PARSER_TIMEOUT_SECONDS=10   (or remove the override)
# TD_LITELLM_BASE_URL=<original url>
```

The intent parser degrades gracefully — rule-based fallback still creates tasks from
structured slash-commands (`/run`, `/status`, etc.); only free-form NL parsing is affected.

---

### 8. Restore from backup

**When:** Data loss, accidental deletion, or disaster recovery.

```bash
# 1. List available backups
aws s3 ls s3://<backup-bucket>/postgres/daily/ \
  --region ap-northeast-1

# 2. Download the desired dump (replace <date> with e.g. 20260515-030042)
ssh ec2-user@<server-ip> \
  'aws s3 cp s3://<backup-bucket>/postgres/daily/<date>.dump \
   /tmp/restore.dump --region ap-northeast-1'

# 3. Stop core to avoid writes during restore
ssh ec2-user@<server-ip> \
  'cd /opt/taskdeck && docker compose --env-file .env.production \
   -f docker-compose.production.yml \
   -f docker-compose.production.override.yml stop core'

# 4. Restore (drops and recreates all objects in the taskdeck DB)
ssh ec2-user@<server-ip> \
  'cd /opt/taskdeck && docker compose --env-file .env.production \
   -f docker-compose.production.yml \
   -f docker-compose.production.override.yml \
   exec -T postgres \
   pg_restore -U taskdeck -d taskdeck -c /tmp/restore.dump'

# 5. Restart core
ssh ec2-user@<server-ip> \
  'cd /opt/taskdeck && docker compose --env-file .env.production \
   -f docker-compose.production.yml \
   -f docker-compose.production.override.yml up -d core'

curl -sS https://<distribution-id>.cloudfront.net/health
# Expect: {"status":"ok"}
```

Note: if the dump is a `.sql.gz` (older backup format), decompress first:
`gunzip /tmp/restore.dump.sql.gz`, then use `psql` instead of `pg_restore`.

---

### 9. Rotate runner bearer token

**When:** Token leaked, runner replaced, or routine credential rotation.

The core and runner must receive the new token **simultaneously** — a mismatch causes the
runner to be rejected with 401 and immediately disconnect.

```bash
# 1. Generate a new token
NEW_TOKEN=$(openssl rand -hex 32)
echo "New token: $NEW_TOKEN"

# 2. Update both env files on the server atomically
ssh ec2-user@<server-ip> "
  sed -i 's/^TD_RUNNER_BEARER_TOKEN=.*/TD_RUNNER_BEARER_TOKEN=$NEW_TOKEN/' \
    /opt/taskdeck/.env.production
  sed -i 's/^TD_RUNNER_TOKEN=.*/TD_RUNNER_TOKEN=$NEW_TOKEN/' \
    /opt/taskdeck/.env.runner
"

# 3. Restart core (picks up TD_RUNNER_BEARER_TOKEN)
ssh ec2-user@<server-ip> \
  'cd /opt/taskdeck && docker compose --env-file .env.production \
   -f docker-compose.production.yml \
   -f docker-compose.production.override.yml restart core'

# 4. Restart the runner (picks up TD_RUNNER_TOKEN from .env.runner)
ssh ec2-user@<server-ip> 'sudo systemctl restart taskdeck-runner'

# 5. Verify runner reconnected
ssh ec2-user@<server-ip> 'sudo journalctl -u taskdeck-runner -n 20 --no-pager'
# Look for: "welcome:" log line from the WS handshake
```

Existing in-flight tasks finish before the runner process exits (60s grace via SIGTERM handler).

---

### 10. Cognito User Pool setup (one-time)

**Applicable only when switching to `TD_AUTH_MODE=cognito`.** Skip if you're
running the default single-user `disabled` mode.

The pool is created manually in the AWS Console — taskdeck has no IaC story
yet and one resource doesn't justify adding one. Re-runs are idempotent on the
console; capture the pool/client IDs in `.env.production` exactly once.

```text
1. AWS Console → Cognito → "Create user pool"
   - Region: ap-northeast-1 (matches the rest of the stack)
   - Pool name: taskdeck-prod-users

2. Sign-in options
   - Cognito user pool sign-in only (no federation in v1)
   - Sign-in method: Email

3. Password policy
   - Minimum length: 8
   - Require: numbers + symbols + upper + lower
   - Temporary password expires: 7 days

4. MFA
   - MFA enforcement: REQUIRED
   - MFA methods: Authenticator apps  (TOTP only — DO NOT enable SMS)

5. User account recovery: email only

6. Required attributes: email
   No custom attributes.

7. Email
   - Send email with Cognito (SES integration is a follow-up)

8. App client
   - Client name: taskdeck-web
   - Client type: Public client (no client secret) ← important
   - Auth flows allowed: ALLOW_USER_SRP_AUTH, ALLOW_REFRESH_TOKEN_AUTH
   - Auth flows DISALLOWED: ALLOW_USER_PASSWORD_AUTH (cleartext path)
   - Token expiry: Access 1h, ID 1h, Refresh 30d

9. Self-service sign-up
   - OFF (defense in depth — TD_AUTH_ALLOW_SIGNUP is the ergonomic
     switch; pool-level OFF prevents an env-var bug from accidentally
     opening signup).

10. Capture (Console → User pools → taskdeck-prod-users → ...)
    - User Pool ID:           ap-northeast-1_xxxxxxxxx
    - App client → Client ID: xxxxxxxxxxxxxxxxxxxxxxxxx
```

Generate the session-cookie encryption key (Fernet, 32-byte url-safe b64):

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Append to `/opt/taskdeck/.env.production` on EC2:

```
TD_AUTH_MODE=cognito
TD_AUTH_ALLOW_SIGNUP=false                       # flip to true to enable signup UI
TD_COGNITO_USER_POOL_ID=ap-northeast-1_xxxxxxxxx
TD_COGNITO_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxx
TD_COGNITO_REGION=ap-northeast-1
TD_SESSION_ENCRYPTION_KEY=<paste fernet output here>
# TD_SESSION_COOKIE_NAME=ccpt_session   # default ok
# TD_SESSION_COOKIE_DOMAIN=             # leave empty for host-only cookie
```

⚠️ The Fernet key is part of the **backup-critical** set alongside Postgres.
Lose it and every active user must re-login. Compromise it together with the
DB and an attacker has every active refresh token (30-day windows).

Restart core and verify:

```bash
ssh ec2-user@<server-ip> \
  'cd /opt/taskdeck && docker compose --env-file .env.production \
   -f docker-compose.production.yml \
   -f docker-compose.production.override.yml up -d core'

curl -s https://<distribution-id>.cloudfront.net/api/v1/auth/config | jq
# → {"auth_mode":"cognito","allow_signup":false}
```

Create the first admin (bypasses self-signup-OFF):

```bash
aws cognito-idp admin-create-user \
  --region ap-northeast-1 \
  --user-pool-id ap-northeast-1_xxxxxxxxx \
  --username your-email@example.com \
  --user-attributes Name=email,Value=your-email@example.com Name=email_verified,Value=true \
  --temporary-password 'TempPass!1' \
  --message-action SUPPRESS
```

Then:
1. Open https://<distribution-id>.cloudfront.net → Sign in with email + temp password.
2. NEW_PASSWORD_REQUIRED prompt → set a real password.
3. MFA setup wizard → scan the QR code with an Authenticator app → enter the first 6-digit code.
4. You land in the kanban.
5. POST `/api/v1/auth/bootstrap-ownership` if there are legacy workspaces to claim.

### 11. Rotate Cognito session encryption key

**Applicable only when `TD_AUTH_MODE=cognito`.**

Rotation invalidates every active session — every user has to sign in again.
Plan accordingly (after-hours, send a heads-up).

```bash
# 1. Generate a new Fernet key on your laptop.
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. Update .env.production on EC2 with the new value.
ssh ec2-user@<server-ip> 'nano /opt/taskdeck/.env.production'
#   Edit TD_SESSION_ENCRYPTION_KEY=<new>

# 3. Restart core.
ssh ec2-user@<server-ip> \
  'cd /opt/taskdeck && docker compose --env-file .env.production \
   -f docker-compose.production.yml \
   -f docker-compose.production.override.yml restart core'

# 4. (Optional) Mass-revoke all active sessions in case any old ciphertext
#    is still served from a hot DB snapshot:
ssh ec2-user@<server-ip> 'docker exec taskdeck-postgres-1 \
  psql -U taskdeck -c "TRUNCATE user_sessions"'
```

After rotation: every user lands on the LoginPage on their next request.

### 12. Toggle self-signup

```bash
ssh ec2-user@<server-ip> 'nano /opt/taskdeck/.env.production'
# Set TD_AUTH_ALLOW_SIGNUP=true   (or false to disable)

ssh ec2-user@<server-ip> \
  'cd /opt/taskdeck && docker compose --env-file .env.production \
   -f docker-compose.production.yml \
   -f docker-compose.production.override.yml restart core'
```

The frontend reloads `/auth/config` on next page load and will show / hide
the Sign Up link accordingly.

### 13. Clean up orphaned worktrees from pre-workspace-slug deploys

Before 2026-05-21 (commit P5.3, `0b5f9f9`), all task worktrees lived under
`${TD_WORK_DIR}/workspaces/<task_id>/` and bare clones under
`${TD_WORK_DIR}/repos/<repo-slug>/`. After the deploy they moved to
`${TD_WORK_DIR}/<workspace_slug>/{tasks,repos}/...`. The old top-level
`workspaces/` and `repos/` directories are no longer touched by the runner.

Once you've confirmed the kanban is idle (no `running` tasks) and you've
seen the new layout populated for at least one task:

```bash
ssh ec2-user@<server-ip> 'sudo rm -rf /var/taskdeck/work/workspaces /var/taskdeck/work/repos'
```

Adjust the path if `TD_WORK_DIR` is set to something other than
`/var/taskdeck/work`. The runner recreates everything it needs on
the next dispatched task.

### 14. Why is `summary` not "ok" anymore?

Up until P5.3, `Task.summary` was hardcoded to `"ok"` whenever the runner
finished a task with `exit_code == 0` — regardless of what the task
actually produced. After P5.3, summary is:

- `AgentCoreExecutor.summary()` → the agent's final answer (last 500 chars)
- For shell / Claude Code / Kiro CLI executors → last 500 chars of stdout
  (whitespace-stripped). If stdout was empty, summary is NULL.

Failed tasks (exit_code != 0) leave summary NULL — better than lying.

### 15. ccpt:ask interactive task protocol

After P5.4 (commit `e3e0cb2` onwards), agents that opt in (claude-code,
agentcore-*) receive a system header in their prompt instructing them
to emit `<ccpt:ask>question</ccpt:ask>` and exit when they need user
input. The runner detects this in stdout, transitions the task to
`awaiting_input`, and the user responds via the kanban drawer.

**Stuck task in awaiting_input?** The user just hasn't responded.
There's no auto-timeout for awaiting_input itself; the overall task
`timeout_seconds` (default 7200s = 2h) does eventually fire. To force
unblock, either:
- POST `/api/v1/tasks/{id}/cancel` (kanban supports this in the drawer)
- POST `/api/v1/tasks/{id}/respond {"content":"..."}` to give the
  agent something to work with

**Truncated ccpt:ask:** The runner buffers up to 64KB of stdout for
ask detection. If the agent emits >64KB of output and the ask tag is
in the early-truncated portion, it's lost — the task finishes
normally and the user sees a `done` card. This is rare in practice
(claude --print outputs are usually <10KB). If it becomes a problem,
raise `STDOUT_FULL_CAP` in `packages/runner/taskdeck_runner/crp_client.py`.

**Prior turns prompt size:** Dispatcher caps total `prior_turns`
content at 32KB when re-dispatching. Older turns are dropped with a
placeholder. The full conversation is always retained in the
`task_turns` table; only the prompt is compressed.

**Inspecting a task's conversation directly:**

```sql
SELECT seq, role, content, created_at
FROM task_turns
WHERE task_id = '<uuid>'
ORDER BY seq;
```

**ccpt:ask is for clarification, not tool permission.** The dispatcher
header (`ASK_PROTOCOL_HEADER` in `dispatcher/service.py`) and the resume
prompt (`build_resumed_prompt` in `runner/resume.py`) both tell the
agent it has full tool access in a sandboxed workspace and that
`<ccpt:ask>` should ONLY be used for missing information (ambiguous
scope, missing credentials, choice between approaches). If you see
agents stuck in awaiting_input loops asking for "write permission",
that is a regression — the prompts have drifted back to the pre-P5.5
wording.

### 16. Agent CLI permission model + security follow-ups

**Current model (P5.5+):** All agent CLIs run with their tool-permission
checks disabled. The trust boundary is the **container + per-task
worktree**, not per-tool approval.

| Agent | Flag |
|---|---|
| `claude-code` | `--permission-mode bypassPermissions` |
| `kiro-cli` | `--trust-all-tools` |
| `agentcore-*` | tool list controlled at agent definition time |
| `shell` | unrestricted (the workspace is the sandbox) |

**Why:** Headless runners have no IDE to surface CLI permission
prompts, so leaving permission mode on `default` makes file/Bash tool
calls block forever, which manifests as agents looping in
`awaiting_input` asking the user to "click approve in the IDE." Trust
is delegated downward to the container/worktree level (L2) instead of
per tool call (L1).

**Threat model assumed:**
- Single-tenant EC2 instance (production) or local dev box
- Per-task ephemeral git worktree under `/var/taskdeck/work/<slug>/tasks/<task_id>/`
- Worktrees on failure are kept for postmortem; on success removed
- Agent process inherits runner's environment (incl. AWS creds, API keys)

**Known gaps + follow-ups:**

1. **No egress filtering.** Agent can `curl` arbitrary URLs, exfiltrate
   files, or pull malicious dependencies. Consider per-task network
   namespace + allow-list (npm/pypi/github + the user's declared repos)
   before opening to multi-tenant.
2. **No filesystem boundary inside the container.** Agent can read
   `/opt/taskdeck/.env.production` and other tasks' worktrees.
   Consider running the agent under a dedicated UID with bind mounts
   instead of full container access.
3. **Credentials inheritance is too broad.** The runner has AWS creds
   for `agentcore`; a `claude-code` task can also see them via
   `env`. Split the runner into per-agent subprocesses with scoped env.
4. **No per-tool audit log.** We only persist the agent's stdout
   summary, not which Bash commands ran or which files were written.
   Consider piping `claude --output-format stream-json` and recording
   tool_use events to a `task_tool_events` table.
5. **No rate limit / resource cap.** A runaway agent could fork-bomb
   or fill disk. Add `ulimit` / cgroup memory + CPU caps in the
   container override.
6. **`ccpt:ask` content is user-rendered without sanitization.**
   Markdown/HTML in agent questions reaches the kanban drawer; if we
   ever render it as HTML (currently plain text), XSS is a risk.

These are **deferred**, not blockers — flag any of them as a separate
P5.x or P6.x issue when you pick one up.

---

### 17. Sandbox iptables (block IMDS + taskdeck_default)

**What it does.** A host-level systemd one-shot installs DOCKER-USER
rules that drop egress *from sandbox bridges only* (`td-sbx-*`) to:

- `169.254.169.254/32` — EC2 IMDSv2 metadata (closes the actual gap:
  hop limit=2 lets a single-hop container fetch a token + read the
  IAM role's instance-profile credentials).
- `172.18.0.0/16` — `taskdeck_default` subnet. Defense in depth;
  docker `icc` already blocks cross-bridge L2 traffic.

Core/postgres/caddy on the default `br-*` bridge are untouched, so
boto3 → Cognito / Bedrock keeps working.

**Install on a fresh host:**

```bash
sudo cp deploy/taskdeck-sandbox-iptables.service \
        /etc/systemd/system/
sudo chmod +x deploy/sandbox-iptables.sh
sudo systemctl daemon-reload
sudo systemctl enable --now taskdeck-sandbox-iptables.service
sudo systemctl status taskdeck-sandbox-iptables.service --no-pager
sudo /opt/taskdeck/deploy/sandbox-iptables.sh status   # show rules
```

**EC2 user-data fragment** (re-installs on instance rebuild):

```bash
#!/usr/bin/env bash
# taskdeck deploy dir is /opt/taskdeck (cloned at provision time).
ln -sf /opt/taskdeck/deploy/taskdeck-sandbox-iptables.service \
       /etc/systemd/system/taskdeck-sandbox-iptables.service
chmod +x /opt/taskdeck/deploy/sandbox-iptables.sh
systemctl daemon-reload
systemctl enable --now taskdeck-sandbox-iptables.service
```

**Uninstall / debug:**

```bash
sudo systemctl stop taskdeck-sandbox-iptables.service   # ExecStop = uninstall
# Or one-off:
sudo /opt/taskdeck/deploy/sandbox-iptables.sh uninstall
```

**Verify (must run all three after install):**

```bash
# 1. Core IAM still works (the regression to watch for):
docker exec taskdeck-core-1 python3 -c \
  "import urllib.request as u; r=u.Request('http://169.254.169.254/latest/api/token', method='PUT', headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'}); print(u.urlopen(r,timeout=3).read()[:8])"
# → expect: a token prefix (not a TimeoutError)

# 2. Sandbox container CANNOT reach metadata:
#    spawn a sandbox via the kanban "Open sandbox" button, then:
docker exec td-sandbox-<task_id> sh -c \
  "wget -qO- --timeout=3 http://169.254.169.254/latest/meta-data/ || echo BLOCKED"
# → expect: BLOCKED

# 3. Sandbox can still reach the public internet (don't strangle it):
docker exec td-sandbox-<task_id> sh -c "wget -qO- --timeout=3 https://example.com >/dev/null && echo OK"
# → expect: OK
```
