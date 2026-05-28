"""Per-run artifact persistence — typed path allocator.

Forensic / replay / scoring milestones drop their outputs into stable
paths under `runs/<utc-ts>/artifacts/<kind>/team{N}/round-{N}/<filename>`.
The kinds are a closed set (one enum value per slot), so new artifact
types add a single line here + one consumer — no per-caller path
negotiation.

v1 fills three kinds (api-logs, agent-logs, service-logs); the rest
are reserved as empty subdirs so callers landing later don't have to
re-decide paths.

See docs/DEFCON_challenge_framework_plan_v1.md for the full design.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path


class ArtifactKind(str, Enum):
    """Closed set of artifact slots under runs/<ts>/artifacts/<kind>/."""

    API_LOG = "api-logs"              # LLM stream-json transcript per agent phase
    AGENT_LOG = "agent-logs"          # supervisor stderr / exit_code (process-level)
    SERVICE_LOG = "service-logs"      # docker logs snapshot per round
    SOURCE_SNAPSHOT = "source-snapshots"  # reserved: tar of /srv/challenge/<chal>/
    BINARY_DIFF = "binary-diffs"     # reserved
    PCAP = "pcaps"                    # reserved
    EXPLOIT = "exploits"              # reserved
    REPLAY_BUNDLE = "replay-bundles"  # reserved


_ARTIFACTS_DIRNAME = "artifacts"


_README = """\
# Run artifacts

Per-run forensic outputs. Layout:

```
artifacts/<kind>/team{N}/round-{N}/<filename>
```

Kinds (per `gamemgr.artifacts.ArtifactKind`):

- `api-logs/` — LLM stream-json transcript per agent phase (every turn:
  prompt, assistant message, tool_use, tool_result, usage tokens)
- `agent-logs/` — supervisor diagnostics (stderr, exit_code) at the
  process level — distinct from the API trace
- `service-logs/` — `docker logs` snapshot per (team, challenge, round)
- `source-snapshots/` — reserved (tar of /srv/challenge/<chal>/)
- `binary-diffs/` — reserved
- `pcaps/` — reserved
- `exploits/` — reserved
- `replay-bundles/` — reserved
"""


class ArtifactStore:
    """Allocator for artifact file paths within one run directory."""

    def __init__(self, run_dir: Path | str) -> None:
        self._run_dir = Path(run_dir)
        self._root = self._run_dir / _ARTIFACTS_DIRNAME
        self._root.mkdir(parents=True, exist_ok=True)
        # Reserve empty subdirs for every kind so the layout is visible
        # even before any artifact lands. README explains the contract.
        for kind in ArtifactKind:
            (self._root / kind.value).mkdir(exist_ok=True)
        readme = self._root / "README.md"
        if not readme.exists():
            readme.write_text(_README)

    def path_for(
        self,
        kind: ArtifactKind,
        *,
        team_id: str,
        round_id: int,
        filename: str,
    ) -> Path:
        """Return runs/<ts>/artifacts/<kind>/<team_id>/round-<N>/<filename>.

        Parents are created on demand. Rejects `..` in filename — paths
        must stay inside the run's artifact root. (Without this guard a
        filename like '../../etc/passwd' would write outside the run dir.)
        """
        if ".." in Path(filename).parts:
            raise ValueError(
                f"filename must not contain '..': {filename!r}"
            )
        # Reject absolute filenames too — they'd silently bypass the prefix.
        if Path(filename).is_absolute():
            raise ValueError(
                f"filename must be relative: {filename!r}"
            )

        dest = (
            self._root
            / kind.value
            / team_id
            / f"round-{round_id}"
            / filename
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest

    @property
    def root(self) -> Path:
        """The artifacts/ directory inside this run. For tests + introspection."""
        return self._root
