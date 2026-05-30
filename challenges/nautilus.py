"""NautilusSource — adapter for nautilus-institute/finals-2025.

Per-challenge integration lives in `challenges/nautilus/<name>/`:
each dir contains a hand-authored `Dockerfile`, `spec.toml`,
`restart.sh`, `checker.sh`, and `entrypoint.sh`. The upstream source
is pin-cloned into `vendor/nautilus-finals-2025/` via
`scripts/sync_nautilus.sh`. `NautilusSource.build_image()` assembles
the build context (per-challenge dir + the matching `vendor/<chal>/`
subdir) and tags the result `cyberarena/chal-nautilus-<name>:<pin>`.

The adapter is configured by the `challenge_sources:` block in
game.yaml; for v1 it only includes `ico` (see plan §scope).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

import docker

from challenges.source import ChallengeSource, ChallengeSpec, FlagMechanism

log = logging.getLogger(__name__)


_THIS_DIR = Path(__file__).resolve().parent
NAUTILUS_DIR = _THIS_DIR / "nautilus"
DEFAULT_VENDOR_PATH = _THIS_DIR.parent / "vendor" / "nautilus-finals-2025"


@dataclass(frozen=True)
class _Pin:
    """Identifier baked into image tags so cached images are unambiguous."""

    sha: str   # short SHA of the pinned vendor commit, e.g. "366520c"


class NautilusSource(ChallengeSource):
    """One adapter instance per game.yaml `challenge_sources:` entry.

    Args:
      repo_ref: path (relative to repo root) to the synced vendor checkout
        of nautilus-institute/finals-2025. Defaults to `vendor/nautilus-finals-2025/`.
      include: list of upstream challenge dir names to enable for this
        game (e.g. `["ico"]`). Only challenges present in BOTH
        `challenges/nautilus/<name>/` (our per-challenge dir with
        Dockerfile + spec.toml) AND `<repo_ref>/<name>/` (upstream source)
        are listed.
      client: optional docker client (injected by tests).
    """

    def __init__(
        self,
        repo_ref: str | Path = DEFAULT_VENDOR_PATH,
        include: list[str] | None = None,
        client: docker.DockerClient | None = None,
    ) -> None:
        self._vendor = Path(repo_ref)
        self._include = list(include) if include else []
        self._client = client

    # ── Discovery ──────────────────────────────────────────────────

    def list_challenges(self) -> list[ChallengeSpec]:
        if not self._vendor.is_dir():
            raise FileNotFoundError(
                f"nautilus vendor checkout missing: {self._vendor}. "
                f"Run scripts/sync_nautilus.sh to pin-clone the upstream repo."
            )

        pin = self._read_pin()
        specs: list[ChallengeSpec] = []
        for upstream_name in self._include:
            framework_dir = NAUTILUS_DIR / upstream_name
            vendor_dir = self._vendor / upstream_name
            if not framework_dir.is_dir():
                raise FileNotFoundError(
                    f"nautilus challenge {upstream_name!r} listed in include= but no "
                    f"framework dir at {framework_dir}. Author the per-challenge "
                    f"Dockerfile + spec.toml first."
                )
            if not vendor_dir.is_dir():
                raise FileNotFoundError(
                    f"nautilus challenge {upstream_name!r} listed in include= but no "
                    f"upstream dir at {vendor_dir}. Check spelling vs the "
                    f"vendor checkout."
                )

            spec_path = framework_dir / "spec.toml"
            specs.append(self._spec_from_toml(spec_path, pin, upstream_name))

        return specs

    def _spec_from_toml(
        self,
        spec_path: Path,
        pin: _Pin,
        upstream_name: str,
    ) -> ChallengeSpec:
        with open(spec_path, "rb") as f:
            raw = tomllib.load(f)

        # The spec's `name` (e.g. "nautilus-ico") is the game-level
        # unique id (referenced from game.yaml). The image tag uses the
        # bare upstream dir name (e.g. "ico") so the same image can be
        # reused across multiple game configs.
        name = raw["name"]
        mechanism = FlagMechanism(raw["flag_mechanism"])

        metadata: dict = {
            "flag_path": raw.get("flag_path"),
            "restart_cmd": raw["restart_cmd"],
            "upstream_name": upstream_name,
        }
        hc = raw.get("health_check", {})
        if hc:
            metadata["health_check"] = {
                "process_pattern": hc.get("process_pattern"),
                "checker_command": hc.get("checker_command"),
            }

        return ChallengeSpec(
            name=name,
            source="nautilus",
            port=int(raw["port"]),
            image_tag=f"cyberarena/chal-nautilus-{upstream_name}:{pin.sha}",
            flag_mechanism=mechanism,
            source_view=Path(raw["source_view"]) if "source_view" in raw else None,
            metadata=metadata,
        )

    def _read_pin(self) -> _Pin:
        """Short SHA of the vendor checkout. Used in image tags so cached
        images are unambiguous after sync_nautilus.sh upgrades the pin."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short=12", "HEAD"],
                cwd=self._vendor,
                check=True,
                capture_output=True,
                text=True,
            )
            return _Pin(sha=result.stdout.strip())
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            # Fallback: degraded but functional. Tag with "unpinned"; image
            # caching is by-content via docker layers so the worst case is
            # over-pulling, not correctness.
            log.warning("could not read vendor pin from %s: %s", self._vendor, e)
            return _Pin(sha="unpinned")

    # ── Image build ────────────────────────────────────────────────

    def build_image(self, spec: ChallengeSpec) -> str:
        """Build the docker image for `spec` and return the tag.

        Build context = a temporary directory combining:
            <framework>/<name>/Dockerfile, restart.sh, checker.sh, entrypoint.sh
            <vendor>/<name>/         (mounted as `<name>/` inside the context
                                       so the Dockerfile's `COPY <name>/service/`
                                       works)

        Idempotent: if the image with this tag already exists locally, just
        returns the tag without rebuilding.
        """
        client = self._client or docker.from_env()
        # If the tag already exists, skip rebuild.
        try:
            client.images.get(spec.image_tag)
            log.info("nautilus: image %s already present; skipping build", spec.image_tag)
            return spec.image_tag
        except docker.errors.ImageNotFound:
            pass

        # Upstream dir name (e.g. "ico") was stashed in metadata at spec-build time.
        upstream_name = spec.metadata.get("upstream_name")
        if not upstream_name:
            raise RuntimeError(
                f"NautilusSource.build_image: spec missing metadata['upstream_name'] "
                f"(spec={spec.name!r}, tag={spec.image_tag!r})"
            )
        framework_dir = NAUTILUS_DIR / upstream_name
        vendor_dir = self._vendor / upstream_name

        with tempfile.TemporaryDirectory(prefix=f"cyberarena-build-{upstream_name}-") as tmp_root:
            ctx = Path(tmp_root)
            # Copy framework files (Dockerfile, restart.sh, checker.sh, entrypoint.sh, spec.toml)
            # into the top level of the build context.
            for entry in framework_dir.iterdir():
                if entry.is_file():
                    shutil.copy2(entry, ctx / entry.name)
            # Copy vendor source under <ctx>/<chal_name>/ — the Dockerfile's
            # `COPY ico/service/` line is relative to the context root.
            shutil.copytree(vendor_dir, ctx / upstream_name)

            log.info("nautilus: building %s (context=%s)", spec.image_tag, ctx)
            # platform=linux/amd64 enforced because the Pascal toolchain
            # (ppcx64) only ships an x86_64 backend on Ubuntu — arm64
            # hosts would otherwise pull arm64 ubuntu and fail at fpc
            # invocation. QEMU emulation handles cross-arch on the dev mac.
            #
            # Shell out to `docker build` instead of docker-py because the
            # python SDK's legacy builder + platform= combination errors on
            # post-build image lookup (BuildKit returns a manifest list and
            # the legacy fetch picks the wrong arch). The CLI uses buildx
            # under the hood and handles this cleanly.
            try:
                subprocess.run(
                    [
                        "docker", "build",
                        "--platform=linux/amd64",
                        "-t", spec.image_tag,
                        str(ctx),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    f"docker build failed for {spec.image_tag}\n"
                    f"stdout:\n{e.stdout}\nstderr:\n{e.stderr}"
                ) from e
            log.info("nautilus: built %s", spec.image_tag)

        return spec.image_tag
