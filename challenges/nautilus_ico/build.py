"""Build the `cyberarena/chal-nautilus-ico` image.

Combines two sources into one build context:
  challenges/nautilus_ico/image/    — Dockerfile + framework scripts (this repo)
  vendor/nautilus-finals-2025/ico/  — Pascal sources (pin-cloned upstream)

Run `scripts/sync_nautilus.sh` once to materialize the vendor checkout.

Shells out to `docker build` (not the python SDK) because the SDK's
legacy builder misroutes `--platform=linux/amd64` on arm hosts and
fpc/ppcx64 is amd64-only on Ubuntu.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import docker

from .challenge import IcoChallenge

log = logging.getLogger(__name__)

_FRAMEWORK_DIR = Path(__file__).resolve().parent / "image"
_VENDOR_DIR = Path(__file__).resolve().parents[2] / "vendor" / "nautilus-finals-2025" / "ico"


def build_image(client: docker.DockerClient | None = None) -> str:
    """Build (or cache-hit) the ico image. Returns the resolved tag."""
    challenge = IcoChallenge()
    tag = challenge.vulbox.reference

    client = client or docker.from_env()
    try:
        client.images.get(tag)
        log.info("nautilus-ico: image %s already present; skipping build", tag)
        return tag
    except docker.errors.ImageNotFound:
        pass

    if not _FRAMEWORK_DIR.is_dir():
        raise FileNotFoundError(f"missing framework dir: {_FRAMEWORK_DIR}")
    if not _VENDOR_DIR.is_dir():
        raise FileNotFoundError(
            f"missing vendor dir: {_VENDOR_DIR}. "
            f"Run scripts/sync_nautilus.sh to pin-clone the upstream repo."
        )

    with tempfile.TemporaryDirectory(prefix="cyberarena-build-ico-") as tmp_root:
        ctx = Path(tmp_root)
        # Framework files at context root; vendor under <ctx>/ico/ so the
        # Dockerfile's `COPY ico/service/` resolves.
        for entry in _FRAMEWORK_DIR.iterdir():
            if entry.is_file():
                shutil.copy2(entry, ctx / entry.name)
        shutil.copytree(_VENDOR_DIR, ctx / "ico")

        log.info("nautilus-ico: building %s", tag)
        try:
            subprocess.run(
                ["docker", "build", "--platform=linux/amd64", "-t", tag, str(ctx)],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"docker build failed for {tag}\n"
                f"stdout:\n{e.stdout}\nstderr:\n{e.stderr}"
            ) from e
        log.info("nautilus-ico: built %s", tag)

    return tag
