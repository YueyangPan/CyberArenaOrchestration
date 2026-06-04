"""Image build for nautilus-ico.

The ico challenge needs a dedicated image (custom Pascal binary +
framework scripts under /arena/). We assemble the build context from
two sources:

  challenges/nautilus/ico/   (this repo) — Dockerfile, restart.sh,
                              checker.sh, entrypoint.sh, spec.toml
  vendor/nautilus-finals-2025/ico/  (upstream pin-clone) — Pascal source
                              under `service/` that the Dockerfile copies

Both directories must exist; the vendor checkout is created by
`scripts/sync_nautilus.sh` (a one-shot pin clone).

Tag is `cyberarena/chal-nautilus-ico:<pin>` where `<pin>` is the short
SHA of the vendor checkout, baked into the tag so cached images are
unambiguous after a vendor upgrade.

Shells out to `docker build` (not the python SDK) because the SDK's
legacy builder + `--platform=linux/amd64` combination errors on the
post-build image lookup (BuildKit returns a manifest list; the SDK
picks the wrong arch). The CLI uses buildx and handles cross-arch
cleanly — important on the arm64 dev mac since the Pascal toolchain
(ppcx64) only ships an x86_64 backend on Ubuntu.
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

_FRAMEWORK_DIR = Path(__file__).resolve().parents[1] / "nautilus" / "ico"
_VENDOR_DIR = Path(__file__).resolve().parents[2] / "vendor" / "nautilus-finals-2025" / "ico"


def build_image(client: docker.DockerClient | None = None) -> str:
    """Build the ico image (or no-op if it's already in the local cache).
    Returns the resolved tag."""
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
        # Framework files (Dockerfile, restart.sh, checker.sh, entrypoint.sh,
        # spec.toml) land at the context root.
        for entry in _FRAMEWORK_DIR.iterdir():
            if entry.is_file():
                shutil.copy2(entry, ctx / entry.name)
        # Upstream source lives under <ctx>/ico/ — the Dockerfile's
        # `COPY ico/service/` line is relative to the context root.
        shutil.copytree(_VENDOR_DIR, ctx / "ico")

        log.info("nautilus-ico: building %s (context=%s)", tag, ctx)
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
