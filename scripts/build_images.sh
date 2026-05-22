#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCKER_DIR="$ROOT/config/docker"

echo "==> building cyberarena/dev-base"
docker build -t cyberarena/dev-base -f "$DOCKER_DIR/Dockerfile.dev" "$DOCKER_DIR"

echo "==> building cyberarena/prod-base"
docker build -t cyberarena/prod-base -f "$DOCKER_DIR/Dockerfile.prod" "$DOCKER_DIR"

echo "==> done"
docker images | grep cyberarena
