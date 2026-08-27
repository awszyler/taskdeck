#!/usr/bin/env bash
# Host-level iptables rules to isolate sandbox containers from
# AWS metadata + the taskdeck-default docker subnet.
#
# Why this script exists:
#   sandbox-host (apps/sandbox-host/sandbox_host/provisioning.py) creates
#   one docker bridge per task and tags it with bridge.name=td-sbx-*.
#   We match on `-i td-sbx-+` in DOCKER-USER so these rules apply ONLY
#   to traffic *originating* on a sandbox bridge — core/postgres/caddy
#   on the default `br-*` bridge are untouched. That preserves core's
#   IAM-role boto3 access (Cognito, Bedrock).
#
# What it blocks:
#   - 169.254.169.254 — EC2 IMDS. host metadata reachable via hop=2 +
#     IMDSv2 token from inside any container. The actual security gap.
#   - 172.18.0.0/16   — taskdeck_default subnet. Defense in depth;
#     docker icc isolation already blocks cross-bridge traffic, but
#     explicit drop survives accidental network-attaches.
#
# Usage:
#   sudo deploy/sandbox-iptables.sh install    # idempotent
#   sudo deploy/sandbox-iptables.sh uninstall  # idempotent
#   sudo deploy/sandbox-iptables.sh status     # show current rules
#
# Wired into:
#   - deploy/taskdeck-sandbox-iptables.service (systemd one-shot)
#   - docs/runbook.md "EC2 user-data" section (re-install on new instance)
set -euo pipefail

BRIDGE_PREFIX="td-sbx-+"           # iptables wildcard glob (matches `+`)
META_IP="169.254.169.254/32"
DEFAULT_NET="172.18.0.0/16"          # taskdeck_default subnet

cmd="${1:-status}"

ensure_chain() {
    # DOCKER-USER is created by docker daemon at startup. Be defensive:
    # if docker isn't running yet we want to fail loud, not silently no-op.
    iptables -L DOCKER-USER -n >/dev/null 2>&1 || {
        echo "DOCKER-USER chain not present — is docker running?" >&2
        exit 1
    }
}

rule_present() {
    iptables -C DOCKER-USER "$@" 2>/dev/null
}

install_rule() {
    if rule_present "$@"; then
        echo "  already present: $*"
    else
        # -I 1 inserts at the top so RETURN/ACCEPT defaults below can't
        # win first. DOCKER-USER's default last rule is RETURN, so order
        # matters.
        iptables -I DOCKER-USER 1 "$@"
        echo "  installed:       $*"
    fi
}

remove_rule() {
    while rule_present "$@"; do
        iptables -D DOCKER-USER "$@"
        echo "  removed:         $*"
    done
}

case "$cmd" in
install)
    ensure_chain
    echo "Installing sandbox iptables rules…"
    install_rule -i "$BRIDGE_PREFIX" -d "$META_IP" -j DROP \
        -m comment --comment "ccpt: block sandbox→IMDS"
    install_rule -i "$BRIDGE_PREFIX" -d "$DEFAULT_NET" -j DROP \
        -m comment --comment "ccpt: block sandbox→taskdeck_default"
    echo "Done."
    ;;
uninstall)
    ensure_chain
    echo "Removing sandbox iptables rules…"
    remove_rule -i "$BRIDGE_PREFIX" -d "$META_IP" -j DROP \
        -m comment --comment "ccpt: block sandbox→IMDS"
    remove_rule -i "$BRIDGE_PREFIX" -d "$DEFAULT_NET" -j DROP \
        -m comment --comment "ccpt: block sandbox→taskdeck_default"
    echo "Done."
    ;;
status)
    ensure_chain
    iptables -L DOCKER-USER -n -v --line-numbers
    ;;
*)
    echo "Usage: $0 {install|uninstall|status}" >&2
    exit 1
    ;;
esac
