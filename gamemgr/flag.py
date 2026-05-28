"""Flag store + submission verification.

Single source of truth for which flags are live, who owns each one, and who
has already submitted what. Lives entirely in memory — game state does not
need to survive process restart in MVP.

Reasons returned by `submit()`:
  ACCEPTED   — first submission of someone else's live flag by this team
  UNKNOWN    — flag string not in any (team, service) row
  OWN        — flag belongs to the submitter's own team
  DUPLICATE  — this (submitter, victim, service) was already submitted
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass, field


FLAG_BODY_LEN = 32
FLAG_BODY_ALPHABET = string.ascii_uppercase + string.digits


def new_flag() -> str:
    """Generate a flag of the standard `FLAG{<32 [A-Z0-9]>}` shape (matches v1)."""
    body = "".join(secrets.choice(FLAG_BODY_ALPHABET) for _ in range(FLAG_BODY_LEN))
    return f"FLAG{{{body}}}"


@dataclass(frozen=True)
class SubmitResult:
    accepted: bool
    reason: str   # ACCEPTED | UNKNOWN | OWN | DUPLICATE
    victim_team_id: str | None = None
    service: str | None = None


@dataclass
class FlagStore:
    # (owner_team_id, service_name) -> flag string
    planted: dict[tuple[str, str], str] = field(default_factory=dict)
    # Inverted index: flag -> (owner_team_id, service_name) for O(1) submit lookup.
    by_flag: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Per-submitter set of flag strings already accepted. Keying on the flag
    # string itself (not on (submitter, victim, service)) means rotation
    # automatically permits re-attack each round — a fresh flag has a new
    # string and is not yet in any submitter's set. An UNKNOWN check
    # in submit() runs first so stale-flag retries can never trip DUPLICATE.
    submissions: dict[str, set[str]] = field(default_factory=dict)

    def plant(self, team_id: str, service: str, flag: str) -> None:
        """Record that `team_id` has `flag` planted on its `service`.

        Re-planting (same team+service) replaces the prior flag and removes
        its inverted-index entry — the old flag becomes UNKNOWN on submit.
        Per-round rotation depends on this behavior.
        """
        key = (team_id, service)
        old = self.planted.get(key)
        if old is not None:
            self.by_flag.pop(old, None)
        self.planted[key] = flag
        self.by_flag[flag] = key

    def submit(self, submitter_team_id: str, flag: str) -> SubmitResult:
        owner = self.by_flag.get(flag)
        if owner is None:
            return SubmitResult(accepted=False, reason="UNKNOWN")
        victim_team_id, service = owner
        if victim_team_id == submitter_team_id:
            return SubmitResult(
                accepted=False, reason="OWN",
                victim_team_id=victim_team_id, service=service,
            )
        submitted = self.submissions.setdefault(submitter_team_id, set())
        if flag in submitted:
            return SubmitResult(
                accepted=False, reason="DUPLICATE",
                victim_team_id=victim_team_id, service=service,
            )
        submitted.add(flag)
        return SubmitResult(
            accepted=True, reason="ACCEPTED",
            victim_team_id=victim_team_id, service=service,
        )
