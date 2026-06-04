"""Image build for the notes challenge.

Simple single-stage ubuntu + python3 + sshd + the notes daemon baked
in at build time. No external pin (vs. ico which clones an upstream
vendor commit); the tag's `v<n>` suffix is bumped manually when the
Dockerfile or daemon source changes meaningfully — the cache-by-content
docker layer system handles the actual rebuild detection.

Shells out to `docker build` (not the python SDK) for the same reason
ico does: the SDK's legacy builder mishandles `--platform=linux/amd64`
on arm64 hosts (mac dev), and the CLI uses buildx which gets the cross-
arch path right.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import docker

from .challenge import NotesChallenge

log = logging.getLogger(__name__)

_CONTEXT_DIR = Path(__file__).resolve().parent / "image"


def build_image(client: docker.DockerClient | None = None) -> str:
    """Build (or cache-hit) the notes image. Returns the resolved tag."""
    challenge = NotesChallenge()
    tag = challenge.vulbox.reference

    client = client or docker.from_env()
    try:
        client.images.get(tag)
        log.info("notes: image %s already present; skipping build", tag)
        return tag
    except docker.errors.ImageNotFound:
        pass

    if not _CONTEXT_DIR.is_dir():
        raise FileNotFoundError(f"missing build context: {_CONTEXT_DIR}")

    log.info("notes: building %s (context=%s)", tag, _CONTEXT_DIR)
    try:
        subprocess.run(
            ["docker", "build", "--platform=linux/amd64", "-t", tag, str(_CONTEXT_DIR)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"docker build failed for {tag}\n"
            f"stdout:\n{e.stdout}\nstderr:\n{e.stderr}"
        ) from e
    log.info("notes: built %s", tag)
    return tag
