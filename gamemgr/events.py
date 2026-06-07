"""Event types + factory. Append-only telemetry goes through
`gamemgr.timeline.Timeline`; this module just owns the data shapes."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class EventType(str, Enum):
    ROUND_START = "ROUND_START"
    PHASE_CHANGE = "PHASE_CHANGE"
    ROUND_STOP = "ROUND_STOP"
    SERVICE_DEPLOYED = "SERVICE_DEPLOYED"
    # Agent lifecycle: host-emitted INSTALLED (prep done — env / mcp.json /
    # SSH pivot / workdir staged) → MCP-emitted READY (LLM proved the
    # channel works by calling report_ready). Per-phase invocation events
    # (AGENT_PROMPT_SENT / COMPLETED / TIMEOUT / FAILED) cover everything
    # downstream of install. No separate launch step — claude is spawned
    # per-phase by `AgentManager.run_phase`.
    AGENT_INSTALLED = "AGENT_INSTALLED"
    AGENT_READY = "AGENT_READY"
    FLAG_PLANTED = "FLAG_PLANTED"
    FLAG_SUBMITTED = "FLAG_SUBMITTED"
    # Agent-self-reported deploy completion (agent → MCP).
    # Distinct from SERVICE_DEPLOYED which is the host-side initial install.
    DEPLOY_COMPLETE = "DEPLOY_COMPLETE"
    # DEFCON framework restart-job chain (orchestrator-emitted). Async by
    # design — STARTED fires before the background task, COMPLETED/FAILED
    # fire when the task settles. All three share a `job_id` in the payload.
    SERVICE_RESTART_STARTED = "SERVICE_RESTART_STARTED"
    SERVICE_RESTART_COMPLETED = "SERVICE_RESTART_COMPLETED"
    SERVICE_RESTART_FAILED = "SERVICE_RESTART_FAILED"
    # Per-level service health probe emitted after each restart, one event
    # per HealthLevel (PROCESS → TCP → PROTOCOL → CHECKER). Payload carries
    # {level, passed, detail}. Catches "hollow patch" cheese paths where
    # lower levels pass but CHECKER (the lifted upstream poller) fails.
    SERVICE_HEALTH_CHECK = "SERVICE_HEALTH_CHECK"
    # DEFCON defense kickoff — emitted once per (team, service) on
    # PHASE_CHANGE → DEFENSE entry. Signals that a defense agent should
    # be spawned for that team. The actual `ClaudeAgentManager.install()`
    # is delegated to a handler registered via
    # `GameMgr.register_defense_kickoff_handler` so GameMgr doesn't have
    # to own the agent supervisor (lets the API server / demo / tests
    # wire whichever manager they need).
    DEFENSE_KICKOFF = "DEFENSE_KICKOFF"
    # Mirror of DEFENSE_KICKOFF — emitted once per team on
    # PHASE_CHANGE → ATTACK entry. One attack agent per team per round
    # (vs. defense's per-team-per-service) because an attack agent
    # attacks every opposing team in a single run.
    ATTACK_KICKOFF = "ATTACK_KICKOFF"
    # Round loop terminated — fired exactly once at the end of the game,
    # whether via natural exit (round counter == total_rounds), stop()
    # signal, or unhandled exception. Payload carries the cause and the
    # final round / phase the loop exited from.
    GAME_END = "GAME_END"
    # Persistent-memory agent (v1): per-phase invocation events fired
    # by the kickoff handlers around each `ClaudeAgentManager.run_phase`
    # call. AGENT_INSTALLED already exists; these cover the per-phase
    # cycle that lives inside the round loop.
    AGENT_PROMPT_SENT = "AGENT_PROMPT_SENT"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_FAILED = "AGENT_FAILED"
    # Pre-R1 dry-run readiness probe.
    # DRY_RUN_START — orchestrator emits per team before invoking the
    #   agent with the dry-run prompt.
    # DRY_RUN_OK    — orchestrator emits when AGENT_READY arrives for
    #   that team within the dry-run window (agent successfully called
    #   the existing `report_ready` MCP tool — proves the whole setup
    #   chain works end-to-end).
    # DRY_RUN_FAILED — orchestrator emits on timeout / process crash
    #   when AGENT_READY never arrives for that team.
    DRY_RUN_START = "DRY_RUN_START"
    DRY_RUN_OK = "DRY_RUN_OK"
    DRY_RUN_FAILED = "DRY_RUN_FAILED"


@dataclass(frozen=True)
class Event:
    id: str
    ts: float
    type: str
    team_id: str | None
    round_id: int
    phase: str
    payload: dict = field(default_factory=dict)


def new_event(
    type_: str | EventType,
    *,
    team_id: str | None = None,
    round_id: int = 0,
    phase: str = "",
    payload: dict | None = None,
) -> Event:
    return Event(
        id=uuid.uuid4().hex[:12],
        ts=time.time(),
        type=str(type_.value if isinstance(type_, EventType) else type_),
        team_id=team_id,
        round_id=round_id,
        phase=phase,
        payload=payload or {},
    )


def new_run_dir(base: Path | str = "runs") -> Path:
    """Create runs/<utc-timestamp>/ and return the path."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    p = Path(base) / stamp
    p.mkdir(parents=True, exist_ok=True)
    return p