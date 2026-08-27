# Tier 3 Deployment Guide

**Target:** Fresh Ubuntu 22+ / Debian 12+ server + your WeCom corp.  
**Goal:** `git clone` → phone sending messages to a live bot in ~1 hour.

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| Server | Ubuntu 22.04+ or Debian 12+, ≥ 2 vCPU, ≥ 2 GB RAM |
| Docker + Docker Compose | `docker compose version` must show v2+ |
| DNS (optional but recommended) | A record pointing at the server IP — Caddy auto-provisions TLS |
| Node.js 20+ | Only needed on the machine where you build the web bundle (can be local) |
| WeCom corp | Admin access to 企业微信管理后台 |

Install Docker on Ubuntu (if not already installed):

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # then re-login
```

---

## 2. Clone the repo and create env files

```bash
git clone <your-repo-url> /opt/taskdeck
cd /opt/taskdeck

cp .env.production.example .env.production
cp .env.runner.example     .env.runner
```

Open `.env.production` in your editor and fill every `CHANGEME` value:

- `PUBLIC_HOSTNAME` — your domain or server IP
- `POSTGRES_PASSWORD` — a strong random password
- `DATABASE_URL` — update the password to match `POSTGRES_PASSWORD`
- `TD_PUBLIC_BASE_URL` — `https://your-domain` (or `http://ip`)
- Leave `TD_WECOM_ENABLED=false` for now — you'll flip it in step 9

---

## 3. Generate the runner bearer token

Run this **once** and copy the output:

```bash
openssl rand -hex 32
```

Paste the value into **both** places:

- `.env.production` → `TD_RUNNER_BEARER_TOKEN=<token>`
- `.env.runner`     → `TD_RUNNER_TOKEN=<token>`

The same secret is used on both ends; they must match exactly.

---

## 4. Build the web bundle

The web bundle (`apps/web/dist/`) must exist before starting Caddy. Build it on any machine with Node 20+:

```bash
# On your local machine (or the server if Node is installed)
cd /path/to/taskdeck
cd apps/web && npm install -g pnpm || true
pnpm install
pnpm build          # outputs to apps/web/dist/
```

If you built locally, copy the dist to the server:

```bash
rsync -av apps/web/dist/ user@your-server:/opt/taskdeck/apps/web/dist/
```

> **Why not bake the build into Docker?**  
> The Vite build would pull Node + all npm deps into the image, adding ~400 MB and 5-10 minutes to every `docker compose build`. Caddy just needs the static files mounted as a read-only volume. This is faster and cleaner.

---

## 5. Configure and start the production stack

### 5a. Create the production override file

The override file mounts the production Caddyfile and binds postgres/core to localhost only.
An example is committed at `docker-compose.production.override.yml.example`:

```bash
cd /opt/taskdeck
cp docker-compose.production.override.yml.example docker-compose.production.override.yml
# Edit if needed (the defaults work for a standard EC2/VPS deployment)
```

> The real override file is gitignored so environment-specific edits are never committed.

### 5b. ALB / reverse-proxy port note

Caddy binds to host ports **8888** (HTTP) and **8443** (HTTPS) by default instead of 80/443.
This avoids conflicts with any host-level service already using those ports.

- If you're behind an ALB or CloudFront, set your **target group port to 8888**.
- If you have a bare server with no ALB, you can override ports back to 80/443 in your
  `docker-compose.production.override.yml`.

### 5c. Plain-HTTP (ALB/CloudFront TLS termination) setup

When TLS is terminated upstream (CloudFront → ALB → EC2), use `Caddyfile.production` which
listens on `:80` only and proxies `/api/*` to core without attempting certificate provisioning.
The override file already mounts it:

```yaml
caddy:
  volumes:
    - ./Caddyfile.production:/etc/caddy/Caddyfile:ro
```

### 5d. Start the stack

```bash
cd /opt/taskdeck
docker compose \
  -f docker-compose.production.yml \
  -f docker-compose.production.override.yml \
  --env-file .env.production \
  up -d postgres core caddy
```

Watch startup logs until core is healthy:

```bash
docker compose -f docker-compose.production.yml logs -f core
# Should print: INFO:uvicorn...:Application startup complete.
```

Verify:

```bash
curl http://localhost:8888/health            # → {"status":"ok"}
curl http://localhost:8888/api/v1/workspaces # → []  (empty list is correct)
# Or via your public domain:
curl https://your-domain/health
```

---

## 6. Install the runner as a systemd service

The service file (`deploy/taskdeck-runner.service`) defaults to `User=ec2-user` — the
standard cloud-init user on Amazon Linux 2023 and Ubuntu. This works out of the box on most
cloud VMs without creating extra system users.

**If you prefer a dedicated system user**, create it before installing the service:

```bash
sudo useradd -r -s /usr/sbin/nologin -m -d /opt/taskdeck taskdeck 2>/dev/null || true
sudo chown -R taskdeck:taskdeck /opt/taskdeck /var/taskdeck
# Then edit deploy/taskdeck-runner.service: User=taskdeck  Group=taskdeck
```

For the default `ec2-user` path:

```bash
# Create work directory
sudo mkdir -p /var/taskdeck/work /var/taskdeck/artifacts
sudo chown -R ec2-user:ec2-user /var/taskdeck

# Set up the Python venv for the runner
cd /opt/taskdeck
pip install uv   # or: curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv .venv
uv pip install --python .venv/bin/python --no-editable packages/proto packages/runner

# Copy env file for the runner
cp .env.runner /opt/taskdeck/.env.runner
chmod 600 /opt/taskdeck/.env.runner

# Install and start the systemd service
sudo cp deploy/taskdeck-runner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now taskdeck-runner

# Verify it started
sudo systemctl status taskdeck-runner
sudo journalctl -u taskdeck-runner -n 50
```

The runner will connect to core via WebSocket. You should see it appear in the web UI under **Settings → Runners** within a few seconds.

---

## 7. Verify the full stack

```bash
# Core health
curl https://your-domain/health

# API reachable
curl https://your-domain/api/v1/workspaces

# Runner connected (should show at least one runner)
curl https://your-domain/api/v1/internal/runners
```

---

## 8. WeCom (企业微信) setup

1. Log in to [https://work.weixin.qq.com/](https://work.weixin.qq.com/) as corp admin.
2. Go to **应用管理 → 自建 → 创建应用**.
   - Logo + name: anything you like.
   - Visibility: a test group or **全员** when ready.
3. After creation, note:
   - **AgentID** (数字)
   - **Secret** (点击"获取")
4. Under **接收消息** → **设置API接收**:
   - **URL**: `https://your-domain/api/v1/im/wecom/callback`
   - **Token**: generate any printable string (keep it secret)
   - **EncodingAESKey**: click **随机生成** (43 Base64 chars)
   - Click **保存** — WeCom will immediately send a GET verification request to the callback URL. Core must be running; you should see a 200 in the core logs.
5. From **我的企业** → **企业信息**, note the **企业ID** (corpid).

---

## 9. Wire WeCom into core and restart

Edit `.env.production`:

```dotenv
TD_WECOM_ENABLED=true
TD_WECOM_CORP_ID=<corpid from step 8-5>
TD_WECOM_AGENT_ID=<agentid from step 8-3>
TD_WECOM_SECRET=<secret from step 8-3>
TD_WECOM_TOKEN=<token you set in step 8-4>
TD_WECOM_AES_KEY=<aes key from step 8-4>
TD_WECOM_DEFAULT_WORKSPACE_SLUG=default   # must be an existing workspace slug
```

Restart core:

```bash
docker compose -f docker-compose.production.yml restart core
```

---

## 10. Create a workspace (if you haven't already)

Open `https://your-domain` in a browser. Create a new workspace. The slug you use here is what goes into `TD_WECOM_DEFAULT_WORKSPACE_SLUG`.

---

## 11. Phone smoke test

1. Open the WeCom app on your phone and find the self-built app you created.
2. DM the bot: `/bind`
   - The bot should reply with a bind code (e.g. `BIND-XXXX`).
   - In the web UI go to your workspace → **Settings → IM Identities** → paste the code.
   - Or use the bind panel in the web UI to generate the code, then DM `/bind <code>` to the bot.
3. DM the bot: `/status`
   - Should reply: `No active tasks.` (or a task list if any exist).
4. Send a natural-language task: e.g. `Write a brief summary of the Taskdeck project`.
   - Within a few seconds the task should appear in the web UI.
   - If a runner is online and an agent is configured, it will start running automatically.

---

## Shipping a new revision

When you land new commits on `main` locally and want to deploy them:

1. **Push first.** EC2 clones over SSH (deploy key); a fresh `git pull` only sees what's on `origin`. Forgetting this step is the most common cause of "I deployed but nothing changed":

   ```bash
   # Locally
   git push origin main
   ```

2. **Pull on the server.**

   ```bash
   ssh ec2-user@<server-ip> 'cd /opt/taskdeck && git pull --ff-only origin main'
   ```

3. **Rebuild the core image if the Dockerfile, Python source, or a migration changed.** Docker Compose will happily reuse a stale cache that predates the pull:

   ```bash
   ssh ec2-user@<server-ip> 'cd /opt/taskdeck && \
     docker compose --env-file .env.production \
     -f docker-compose.production.yml \
     -f docker-compose.production.override.yml \
     build --no-cache core'
   ```

   `--no-cache` is important when:
   - A new Alembic migration file was added (entrypoint must see it)
   - `Dockerfile` or `requirements` changed
   - `uv.lock` changed

   For web-only changes (static bundle under `apps/web/dist`), `--no-cache` is unnecessary — just rebuild the bundle and restart Caddy.

4. **Roll the core container.**

   ```bash
   ssh ec2-user@<server-ip> 'cd /opt/taskdeck && \
     docker compose --env-file .env.production \
     -f docker-compose.production.yml \
     -f docker-compose.production.override.yml \
     up -d core'
   ```

   The entrypoint runs `alembic upgrade head` on every start, so new migrations apply automatically.

5. **Rebuild the web bundle if `apps/web` changed.**

   ```bash
   ssh ec2-user@<server-ip> 'cd /opt/taskdeck/apps/web && pnpm install --frozen-lockfile && pnpm build'
   ssh ec2-user@<server-ip> 'cd /opt/taskdeck && \
     docker compose --env-file .env.production \
     -f docker-compose.production.yml \
     -f docker-compose.production.override.yml \
     restart caddy'
   ```

6. **Smoke via CloudFront.**

   ```bash
   curl -sS https://<distribution-id>.cloudfront.net/api/v1/workspaces | python3 -m json.tool
   ```

### Pitfalls

- **Silent cache reuse.** `docker compose up -d --build` without `--no-cache` can use a layer that pre-dates today's git pull. If entrypoint logs don't show a new migration running, your image is stale — rebuild with `--no-cache`.
- **`.env` overwrite on `git pull`.** The `.env.production` file is gitignored, so `pull` leaves it alone. But if you accidentally `git checkout` it, secrets are wiped. Always verify `.env.production` contents after a pull if something breaks.
- **Caddy serving old JS.** Caddy doesn't restart on file change; it watches `/srv/web`. After `pnpm build`, `restart caddy` forces a re-read of the mounted volume.

---

## 12. Troubleshooting

### WeCom callback returns 500 / signature mismatch
- Check core logs: `docker compose -f docker-compose.production.yml logs core`
- Symptom: `SignatureError` or `AES decrypt failed`
- Cause: Token or AES key copy-paste error. Re-copy from the WeCom console character by character.
- Also check that `TD_WECOM_CORP_ID` is the **corpid**, not the corp name.

### 502 Bad Gateway on `/api/*`
- Core is not running or failed to start.
- Check: `docker compose -f docker-compose.production.yml ps` — core must be `healthy`.
- Check: `docker compose -f docker-compose.production.yml logs core`
- Common cause: `DATABASE_URL` password mismatch with `POSTGRES_PASSWORD`, or postgres not yet healthy.

### Intent parser timeout / no task created from DM
- `TD_LITELLM_BASE_URL` or `TD_LITELLM_API_KEY` is wrong or the proxy is slow.
- Test directly: `curl -X POST https://your-litellm/v1/chat/completions -H "Authorization: Bearer $KEY" -d '{"model":"...","messages":[{"role":"user","content":"ping"}]}'`
- Increase `TD_INTENT_PARSER_TIMEOUT_SECONDS=15` in `.env.production` and restart core.

### Runner not appearing in the UI
- Check systemd service: `sudo journalctl -u taskdeck-runner -n 50`
- Verify `TD_CORE_WS_URL` in `.env.runner` matches the live URL (wss vs ws, correct hostname).
- Verify `TD_RUNNER_TOKEN` matches `TD_RUNNER_BEARER_TOKEN` in `.env.production`.

### TLS certificate not provisioning
- Caddy listens on host ports **8888/8443** by default. For Caddy-managed TLS (non-ALB setups),
  override the ports in your `docker-compose.production.override.yml` back to 80/443, and
  ensure those ports are open inbound with a DNS A record already resolving to the server.
- Check: `docker compose -f docker-compose.production.yml logs caddy`

---

## Updating

```bash
git pull
# Rebuild web if frontend changed:
cd apps/web && pnpm build
# Rebuild and restart core:
docker compose \
  -f docker-compose.production.yml \
  -f docker-compose.production.override.yml \
  --env-file .env.production \
  build core
docker compose \
  -f docker-compose.production.yml \
  -f docker-compose.production.override.yml \
  --env-file .env.production \
  up -d
# Migration runs automatically on core startup via entrypoint.sh
```

---

## Backups

Daily pg_dump backups are taken automatically and uploaded to S3.

### Bucket

| Key | Value |
|---|---|
| Bucket | `<backup-bucket>` |
| Region | `ap-northeast-1` |
| Prefix | `postgres/daily/` |
| Encryption | SSE-S3 (AES-256) |
| Public access | Blocked |

### Schedule

A systemd timer triggers `deploy/backup.sh` at **03:00 UTC daily** (with up to 5 minutes random jitter).  
`Persistent=true` means a missed run (host was off) is caught up on next boot.

### Retention

S3 lifecycle rule `postgres-daily-30d` expires objects under `postgres/daily/` after **30 days** automatically.  
A second rule `artifacts-weekly-180d` covers `artifacts/weekly/` with a **180-day** TTL.

### IAM

The EC2 instance role `AmazonSSMRoleForInstancesQuickSetup` has inline policy `taskdeck-s3-backup` granting:
- `s3:PutObject` / `s3:PutObjectAcl` on `arn:aws:s3:::<backup-bucket>/*`
- `s3:ListBucket` on `arn:aws:s3:::<backup-bucket>`

The policy document is committed at `deploy/taskdeck-backup-policy.json` for reference.

### Install on a new server

```bash
# Copy files to server
scp deploy/backup.sh deploy/taskdeck-backup.service deploy/taskdeck-backup.timer \
    ec2-user@<host>:/tmp/

# Install on server
ssh ec2-user@<host> '
  set -e
  sudo install -m 0755 /tmp/backup.sh /opt/taskdeck/deploy/backup.sh
  sudo install -m 0644 /tmp/taskdeck-backup.service /etc/systemd/system/
  sudo install -m 0644 /tmp/taskdeck-backup.timer /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now taskdeck-backup.timer
  sudo systemctl list-timers taskdeck-backup.timer --no-pager
'
```

### Trigger a manual backup

```bash
ssh ec2-user@<host> 'sudo systemctl start taskdeck-backup.service'
# Check result
ssh ec2-user@<host> 'sudo journalctl -u taskdeck-backup.service -n 20 --no-pager'
# Confirm object in S3
aws s3 ls s3://<backup-bucket>/postgres/daily/ --region ap-northeast-1
```

### Restore procedure

```bash
# 1. Download the dump
aws s3 cp s3://<backup-bucket>/postgres/daily/<date>.dump /tmp/restore.dump \
    --region ap-northeast-1

# 2. Restore into a target database (adjust compose opts as needed)
docker compose \
  --env-file .env.production \
  -f docker-compose.production.yml \
  -f docker-compose.production.override.yml \
  exec -T postgres \
  pg_restore -U taskdeck -d taskdeck_restore -c /tmp/restore.dump
```

Replace `<date>` with the timestamp from the S3 listing, e.g. `20260515-030042`.

## Observability (Phase 4.2)

A Prometheus + Grafana stack is part of the production compose, but **never
exposed publicly**. Operator access is via SSH local-forward, matching the
"only CloudFront prefix-list + SSH:22" security posture.

### What's in the stack

- `prometheus` (private) — scrapes `core:8000/metrics` every 15s, retains 15d.
  No host port; reachable only on the compose network.
- `grafana` (loopback) — listens on `127.0.0.1:3001` on the host (set in
  `docker-compose.production.override.yml`). Pre-provisioned with a Prometheus
  datasource, the *Taskdeck Overview* dashboard, and four alert rules.

### Required env

Add to `.env.production`:

```
GRAFANA_ADMIN_PASSWORD=<strong-password>
```

### Bringing it up

```bash
ssh ec2-user@<host> '
  cd /opt/taskdeck && \
  docker compose --env-file .env.production \
    -f docker-compose.production.yml \
    -f docker-compose.production.override.yml \
    up -d prometheus grafana
'
```

### Reaching Grafana from your laptop

```bash
# On your laptop:
ssh -L 3001:127.0.0.1:3001 ec2-user@<server-ip>
# Open http://localhost:3001 in a browser. Log in:
#   user: admin
#   pass: $GRAFANA_ADMIN_PASSWORD from .env.production
```

The first dashboard you'll see is **Taskdeck Overview** (in the default
folder). Alerts fire to a stub webhook (`http://localhost:9999/taskdeck-alerts`)
which intentionally never resolves — replace the URL in
`deploy/grafana/alerting/contact-points.yml` and re-deploy when you wire a
real alert sink.

### Verifying scrape

From inside the compose network:

```bash
ssh ec2-user@<host> 'docker exec taskdeck-prometheus-1 wget -qO- http://localhost:9090/api/v1/targets | head -c 600'
```

Look for `"health":"up"` against the `taskdeck-core` job.
