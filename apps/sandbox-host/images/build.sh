#!/bin/bash
# Build all sandbox base images. Idempotent — re-run anytime.
#
# Usage:
#   ./build.sh           # build all 3
#   ./build.sh static    # build a specific one
set -euo pipefail

cd "$(dirname "$0")"

build_image() {
    local name="$1"
    local dockerfile="${name}.Dockerfile"
    local tag="td-sandbox-${name}:latest"

    echo "==> building $tag from $dockerfile"
    docker build -f "$dockerfile" -t "$tag" .
    echo "==> built $tag"
}

if [ $# -eq 0 ]; then
    targets=(static node python)
else
    targets=("$@")
fi

for t in "${targets[@]}"; do
    build_image "$t"
done

echo
echo "==> done. tags:"
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' | grep -E '^(REPOSITORY|td-sandbox-)'
