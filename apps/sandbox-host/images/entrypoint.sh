#!/bin/sh
# Generic entrypoint for taskdeck sandbox images.
#
# sandbox-host injects:
#   TD_INSTALL_CMD   - optional, runs first if non-empty
#   TD_START_CMD     - required, what to run after install
#   TD_BASE_PATH     - e.g. "/sandbox/<task_id>/", forwarded to user app
#                        as VITE_BASE_URL / NEXT_PUBLIC_BASE_PATH / ROOT_PATH /
#                        BASE_URL so common frameworks pick it up
#
# Working directory is /workspace, which sandbox-host bind-mounts from
# the host's per-task worktree.
set -e

cd /workspace

# Forward base path under several env names that common frameworks
# expect, so user apps "just work" without needing to know about
# VITE_BASE specifically. Setting unused vars is harmless.
if [ -n "$TD_BASE_PATH" ]; then
  export VITE_BASE_URL="$TD_BASE_PATH"
  export NEXT_PUBLIC_BASE_PATH="${TD_BASE_PATH%/}"
  export ROOT_PATH="${TD_BASE_PATH%/}"
  export BASE_URL="$TD_BASE_PATH"
  export PUBLIC_URL="$TD_BASE_PATH"
fi

if [ -n "$TD_INSTALL_CMD" ]; then
  echo "[entrypoint] install: $TD_INSTALL_CMD"
  sh -c "$TD_INSTALL_CMD"
fi

echo "[entrypoint] start: $TD_START_CMD"
exec sh -c "$TD_START_CMD"
