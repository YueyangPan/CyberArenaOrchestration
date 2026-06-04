#!/usr/bin/env bash
# Pre-build the infrastructure images. Per-challenge images are built
# on-demand by `challenges.registry.build_images(cfg)` (invoked from
# main.py's `setup` + `start` paths) — adding a new challenge does NOT
# require touching this script.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCKER_DIR="$ROOT/config/docker"

echo "==> building cyberarena/dev-base"
docker build -t cyberarena/dev-base -f "$DOCKER_DIR/Dockerfile.dev" "$DOCKER_DIR"

echo "==> done"
docker images | grep cyberarena
