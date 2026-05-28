#!/usr/bin/env bash
# Pin-clones nautilus-institute/finals-2025 into vendor/ at a known SHA.
#
# The DEFCON framework reads challenge sources from vendor/nautilus-finals-2025/
# at build time (see challenges/nautilus.py). Upgrading the pin requires:
#   1. Update PINNED_SHA below.
#   2. Re-run this script.
#   3. Re-run the full DEFCON test suite (incl. the live Claude smoke).
#   4. Single commit with the new SHA + any per-challenge Dockerfile / spec
#      tweaks needed for the upgrade.
#
# Pinning (not tracking main) so re-runs are reproducible.

set -euo pipefail

REPO_URL="https://github.com/nautilus-institute/finals-2025.git"
PINNED_SHA="366520c0cc43c08c0eaf0a9aadd3d813df01ce88"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR_DIR="${ROOT}/vendor/nautilus-finals-2025"

mkdir -p "${ROOT}/vendor"

if [ ! -d "${VENDOR_DIR}/.git" ]; then
    echo ">>> cloning ${REPO_URL} → ${VENDOR_DIR}"
    git clone "${REPO_URL}" "${VENDOR_DIR}"
fi

cd "${VENDOR_DIR}"

# Make sure the pinned commit is available (in case the local clone is stale).
git fetch --quiet origin

echo ">>> checking out pinned SHA: ${PINNED_SHA}"
git checkout --quiet "${PINNED_SHA}"

echo ">>> nautilus-finals-2025 synced at ${PINNED_SHA}"
echo "    challenges available: $(ls -d */ 2>/dev/null | tr -d '/' | tr '\n' ' ')"
