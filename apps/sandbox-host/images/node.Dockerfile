# taskdeck-sandbox-node
#
# Node 20 LTS with common build tools preinstalled. Sandbox-host runs
# the user app via /usr/local/bin/entrypoint.sh which honors
# $TD_INSTALL_CMD then execs $TD_START_CMD.
#
# Common frameworks supported out of the box:
#   - vite                  (npm run dev → :5173)
#   - express / fastify     (npm start    → :3000)
#   - next                  (npm run dev → :3000)
#   - react/cra             (npm start    → :3000)
#
# The image is intentionally generic — user-app deps are installed
# via $TD_INSTALL_CMD against the bind-mounted /workspace.
FROM node:20-alpine

# Tools agents commonly invoke during build.
RUN apk add --no-cache git python3 make g++ \
    && npm config set fund false \
    && npm config set audit false

# Pre-install the static-fallback "serve" so node-build-only projects
# (npm run build && serve dist) don't re-download it every sandbox start.
RUN npm install -g serve@14

WORKDIR /workspace

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
