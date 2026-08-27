# Phase 3.1 migration — enabling multi-tenancy

Existing single-user deployments can flip on multi-tenancy once P3.1 code is deployed. Steps:

1. Create a GitHub OAuth App at https://github.com/settings/developers
   - Homepage URL: `https://<distribution-id>.cloudfront.net`
   - Authorization callback URL: `https://<distribution-id>.cloudfront.net/api/v1/auth/github/callback`
   - Record client ID and generate a client secret.

2. Update `.env.production` on EC2:
   ```
   TD_AUTH_MODE=github
   TD_SESSION_SECRET_KEY=<openssl rand -hex 32>
   TD_GITHUB_CLIENT_ID=<from step 1>
   TD_GITHUB_CLIENT_SECRET=<from step 1>
   TD_GITHUB_CALLBACK_URL=https://<distribution-id>.cloudfront.net/api/v1/auth/github/callback
   TD_FRONTEND_URL=https://<distribution-id>.cloudfront.net
   ```

3. Restart core:
   ```
   docker compose --env-file .env.production \
     -f docker-compose.production.yml \
     -f docker-compose.production.override.yml \
     up -d core
   ```

4. Visit https://<distribution-id>.cloudfront.net — sign in with GitHub.

5. Claim ownership of pre-existing workspaces:
   ```bash
   # While signed in (cookie set), run from your laptop:
   curl -sS -X POST https://<distribution-id>.cloudfront.net/api/v1/auth/bootstrap-ownership \
     -b "ccpt_session=<cookie value>"
   ```
   Or use the browser — open devtools, copy the cookie, POST with `fetch`.

6. Invite other users:
   - In the web UI, open a workspace → "Invite member" → copy the code → send to teammate.
   - Teammate signs in with GitHub, calls `POST /api/v1/workspaces/join {"code":"..."}`.
