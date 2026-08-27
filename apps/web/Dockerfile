# ── Stage 1: build ──────────────────────────────────────────────────────────
FROM node:20-alpine AS build
RUN corepack enable && corepack prepare pnpm@10.28.2 --activate

WORKDIR /web
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

# ── Stage 2: serve ──────────────────────────────────────────────────────────
FROM caddy:2-alpine
COPY --from=build /web/dist /srv/web
COPY Caddyfile /etc/caddy/Caddyfile
EXPOSE 80
