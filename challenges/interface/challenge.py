"""Challenge — the top-level composition root every challenge author implements.

A Challenge is the unit the orchestrator picks up, deploys to every team,
scores, and reports on. One subclass per CTF problem (or per real-world
vulnerable repo we're wrapping).

No imports from the rest of this codebase — stdlib only — so adapters can
be authored against this file alone.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .probe import FlagHandler, FunctionalityTest, RestartHandler


@dataclass(frozen=True)
class VulboxImage:
    """Pointer to the docker image that IS the production environment.

    The image is treated as a black box. It must self-start the vulnerable
    app on container boot (via ENTRYPOINT/CMD inside the image). The
    orchestrator only knows how to:
      - `docker run` it (pulling if needed, or building from context),
      - publish the ports named in `exposed_ports`,
      - inject `env` and any orchestrator-level overrides,
      - hand the resulting host/port to probes via VulboxTarget.

    Fields:
      reference     — what to deploy. EITHER a registry ref like
                      "ghcr.io/org/notes-vulbox:v1" OR a local image tag
                      that will be built from `build_context` first.
      exposed_ports — name -> container port the image listens on. The
                      names must match the names a paired probe declares
                      in `required_ports`; that's the wiring contract.
      env           — default env vars baked into every deployment. Per-
                      team overrides (flag injection, team token, etc.)
                      are layered on top by the orchestrator.
      build_context — relative path to a directory containing a Dockerfile.
                      If set, the orchestrator builds before deploying. If
                      None, the image is pulled from a registry.
    """

    reference: str
    exposed_ports: dict[str, int]
    env: dict[str, str] = field(default_factory=dict)
    build_context: str | None = None


@dataclass(frozen=True)
class ChallengeDocs:
    """Authoring-time documents bundled with a challenge.

    All three are plain text (markdown allowed). They are surfaced to
    different audiences:

      intro          — for humans: dashboard tooltips, lobby pages,
                       writeups. "What is this app, what stack, where did
                       the vuln come from."
      agent_prompts  — for autonomous agents driving a team. Keys are role
                       names the orchestrator knows ("attacker",
                       "defender", "auditor", …). Each value is the
                       per-role system prompt for that challenge: the
                       agent gets it appended to its base prompt when this
                       challenge is in play.
      func_test_spec — the functional contract that `FunctionalityTest`
                       enforces, written for humans. Used by reviewers
                       writing patches and by anyone debugging a flapping
                       check. Should match what the test actually probes.
    """

    intro: str
    agent_prompts: dict[str, str]
    func_test_spec: str


class Challenge(ABC):
    """One CTF challenge / vulnerable repo, packaged for the orchestrator.

    Implement once per challenge. The same instance is reused across all
    teams: per-team state must NOT live on `self`, only on the
    `VulboxTarget` handed to probes at call time.

    The five properties below are the contract:

        vulbox             -> what to deploy
        functionality_test -> how to verify it still works
        flag_handler       -> how to plant and retrieve flags
        restart_handler    -> how to rebuild + restart after defense edits
                              (None if the challenge is attack-only)
        docs               -> what to tell humans and agents about it

    Keep these as cheap-to-construct properties (return cached instances)
    rather than expensive factory calls — the orchestrator may read them
    repeatedly.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable kebab-case id, e.g. "notes", "blog", "shopxo-cve-2024-x".

        Used as the registry key, container name prefix, and log scope.
        """
        ...

    @property
    @abstractmethod
    def vulbox(self) -> VulboxImage: ...

    @property
    @abstractmethod
    def functionality_test(self) -> FunctionalityTest: ...

    @property
    @abstractmethod
    def flag_handler(self) -> FlagHandler: ...

    @property
    def restart_handler(self) -> RestartHandler | None:
        """Defense-mode rebuild + restart. Default None: the challenge
        is attack-only and the orchestrator's defense-kickoff path
        skips it. Override and return a `RestartHandler` instance to
        opt into the DEFCON-style edit→restart→verify workflow."""
        return None

    @property
    @abstractmethod
    def docs(self) -> ChallengeDocs: ...
