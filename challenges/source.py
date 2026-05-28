"""DEFCON framework — pluggable challenge catalog interface.

A `ChallengeSource` is one backing catalog (built-in notes, nautilus
finals, picoCTF, custom git, …). It produces `ChallengeSpec`s that
describe each deployable challenge and knows how to materialize their
docker images. The rest of the system (GameMgr, Orchestrator) consumes
the specs without caring where they came from.

See docs/DEFCON_challenge_framework_plan_v1.md for the full design.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class FlagMechanism(str, Enum):
    """How a flag is delivered into the running service.

    Adapters declare the mechanism per-challenge in their spec; the
    framework wires the corresponding plant strategy at deploy time.
    """

    FILE_AT_PATH = "file_at_path"     # write flag bytes to a path in the container
    ENV_VAR = "env_var"                # set FLAG=<value> in container env
    BINARY_PATCH = "binary_patch"     # rewrite a sentinel inside the compiled binary
    # Service.plant_flag() owns the delivery via the service's own protocol
    # (e.g. notes does a PUT over its TCP socket). No infrastructure-level
    # write happens at deploy time; the orchestrator just calls Service.
    # Added during impl — the plan's original 3 values didn't cover notes.
    SERVICE_PROTOCOL = "service_protocol"


class HealthLevel(str, Enum):
    """Four layers of "is this service healthy?".

    Higher levels imply the lower ones — CHECKER passing means
    PROTOCOL/TCP/PROCESS already passed. The split exists so a
    "hollow patch" (agent stubs the vulnerable code path but keeps
    benign protocol responses) is visible at the CHECKER layer even
    though PROCESS/TCP/PROTOCOL all pass.
    """

    PROCESS = "process"      # pid alive (orchestrator: pgrep)
    TCP = "tcp"              # port accepts connections
    PROTOCOL = "protocol"    # speaks the protocol (real request → real response)
    CHECKER = "checker"      # gameplay-correct: vuln still reachable, flag retrievable


@dataclass(frozen=True)
class ChallengeSpec:
    """Materialized description of one deployable challenge.

    The adapter's `list_challenges()` returns a list of these; the rest
    of the framework consumes them without knowing which adapter built them.
    """

    name: str                                # unique within the game; game.yaml refs this
    source: str                              # adapter key: "notes" | "nautilus" | …
    port: int                                # service port inside the container
    image_tag: str                           # docker image (e.g. cyberarena/chal-nautilus-ico:<sha>)
    flag_mechanism: FlagMechanism

    # Subpath inside the upstream challenge dir whose contents the
    # adapter COPIES into the runtime image at build time, ending up at
    # /srv/challenge/<name>/ inside the container.
    #
    # NEVER a host bind mount — that would alias every team's container
    # to the same host dir, leaking edits across teams and back into the
    # vendor tree. Each (team, challenge) container gets its own
    # writable copy via the container entrypoint.
    source_view: Path | None = None

    # Opaque to the framework — the adapter owns the contents.
    # Typical keys (per docs): flag_path, restart_cmd, health_check
    # (process_pattern, checker_command). The adapter consumes these
    # when materializing the Service instance.
    metadata: dict = field(default_factory=dict)


class ChallengeSource(ABC):
    """Backing catalog of challenges.

    Subclasses include the built-in notes (a tiny Python TCP daemon)
    and external CTF adapters (nautilus, picoCTF, …). Two methods:
    enumerate the catalog, and build the docker image for a spec.
    """

    @abstractmethod
    def list_challenges(self) -> list[ChallengeSpec]:
        """Return every deployable challenge this source provides."""

    @abstractmethod
    def build_image(self, spec: ChallengeSpec) -> str:
        """Build (or verify cached) docker image; return the image tag.

        For sources with pre-baked images this can be a no-op that just
        returns spec.image_tag. For sources that compile from source
        (nautilus), this drives `docker build` against the per-challenge
        Dockerfile + the upstream source dir.
        """
