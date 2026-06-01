# GameMgr layer — UML

`GameMgr` is the domain controller. It owns the event bus, the round
loop, the flag store, the artifact store, and the per-team agent
handlers. Everything that's "the game" (vs. "the CLI" or "the host")
lives here.

## Class diagram

```mermaid
classDiagram
  class GameMgr {
    +config: GameConfig
    +orchestrator: Orchestrator
    +bus: EventBus
    +round_loop: RoundLoop
    +flags: FlagStore
    +artifacts: ArtifactStore
    +run_dir: Path
    +deploy_challenges() None
    +plant_initial_flags() list
    +plant_flags(round_id) list
    +start(run_dir) None
    +stop() None
    +submit_flag(team_id, flag) SubmitResult
    +restart_service(team_id, service) str
    +install_all_agents() None
    +dry_run_all_agents(*, timeout_secs) None
    +record_phase_started(...) None
    +record_phase_done(...) None
    +record_agent_installed(team_id) None
    +record_agent_ready(team_id) None
    +record_deploy_complete(team_id, service, duration_ms) None
    +handle_for(container_name) ContainerHandle
    +agent_config_for(team_id, *, is_first) AgentConfig
    +defense_agent_config_for(team_id, *, service_name, is_first) AgentConfig
    +register_install_agent_handler(h) None
    +register_dry_run_handler(h) None
    +register_defense_kickoff_handler(h) None
    +register_attack_kickoff_handler(h) None
  }

  class RoundLoop {
    +bus: EventBus
    +network: NetworkManager
    +config: RoundConfig
    +phase: Phase
    +round: int
    +run() None
    +stop() None
    +end_phase_early() None
    -_enter_phase(phase, *, allow_cross_team, duration) None
    -_wait(secs) None
  }

  class Phase {
    <<enumeration>>
    INIT
    PREFLIGHT
    SETUP
    DEFENSE
    ATTACK
    STOPPED
  }

  class EventBus {
    -_subscribers: list
    -_history: list
    +subscribe(cb) None
    +unsubscribe(cb) None
    +publish(event) None
    +history() list
  }

  class FileSink {
    -path: Path
    +__call__(event) None
    +close() None
  }

  class Event {
    +id: str
    +ts: float
    +type: str
    +team_id: str
    +round_id: int
    +phase: str
    +payload: dict
  }

  class EventType {
    <<enumeration>>
    ROUND_START / ROUND_STOP / PHASE_CHANGE
    SERVICE_DEPLOYED / FLAG_PLANTED / FLAG_SUBMITTED
    AGENT_INSTALLED / AGENT_READY / AGENT_PROMPT_SENT
    AGENT_COMPLETED / AGENT_TIMEOUT / AGENT_FAILED
    DRY_RUN_START / DRY_RUN_OK / DRY_RUN_FAILED
    DEFENSE_KICKOFF / ATTACK_KICKOFF
    SERVICE_RESTART_STARTED / _COMPLETED / _FAILED
    SERVICE_HEALTH_CHECK / DEPLOY_COMPLETE
    GAME_END
  }

  class FlagStore {
    -planted: dict
    -submissions: dict
    +plant(team_id, service, flag) None
    +submit(team_id, flag) SubmitResult
  }

  class ArtifactStore {
    -_run_dir: Path
    -_root: Path
    +path_for(kind, *, team_id, round_id, filename) Path
  }

  GameMgr "1" *-- "1" EventBus : owns
  GameMgr "1" *-- "1" RoundLoop : owns
  GameMgr "1" *-- "1" FlagStore : owns
  GameMgr "1" *-- "1" ArtifactStore : owns (lazy in start())
  EventBus o-- FileSink : subscribed in start()
  EventBus ..> Event : publishes
  RoundLoop ..> Phase : tracks
  RoundLoop ..> EventBus : publishes ROUND_START / PHASE_CHANGE / ROUND_STOP / GAME_END
```

## Phase state machine

```mermaid
stateDiagram-v2
  [*] --> INIT
  INIT --> PREFLIGHT
  PREFLIGHT --> SETUP
  SETUP --> DEFENSE : round = 1
  DEFENSE --> ATTACK : timer expires OR all teams DEPLOY_COMPLETE
  ATTACK --> DEFENSE : round < total_rounds
  ATTACK --> STOPPED : round == total_rounds OR stop()
  STOPPED --> [*]
  note right of DEFENSE
    cross-team net: CLOSED
    rotate flags on entry
    early-end via DEPLOY_COMPLETE
  end note
  note left of ATTACK
    cross-team net: OPEN
    end-of-defense health check on entry
  end note
```

## Event bus subscribers (in subscription order)

`bus.publish` awaits subscribers sequentially. Order matters — a
slow subscriber blocks the rest until it returns.

| # | Subscriber | Fires on | Job |
|---|---|---|---|
| 1 | `_on_persist_service_logs` | `ROUND_STOP` | `docker logs <team{N}_prod>` → `runs/<ts>/artifacts/<team>/round-N/docker.log` |
| 2 | `_on_defense_end_health_check` | `PHASE_CHANGE` → ATTACK | run 4-level health chain per team |
| 3 | `_on_rotation_event` | `PHASE_CHANGE` → DEFENSE | `plant_flags(round_id)` + emit FLAG_PLANTED — MUST be before kickoff |
| 4 | `_on_defense_kickoff` | `PHASE_CHANGE` → DEFENSE | emit DEFENSE_KICKOFF + await asyncio.gather of defense handlers |
| 5 | `_on_attack_kickoff` | `PHASE_CHANGE` → ATTACK | emit ATTACK_KICKOFF + await asyncio.gather of attack handlers |
| 6 | `_on_deploy_complete` | `DEPLOY_COMPLETE` | drain `_defense_pending`; signal RoundLoop early-end |
| + | `FileSink` | every event | append one JSON line to `events.jsonl` (subscribed in `start()`) |

## Game lifecycle sequence

```mermaid
sequenceDiagram
  participant Op as Operator (main.py)
  participant G as GameMgr
  participant RL as RoundLoop
  participant Bus as EventBus
  participant H as Kickoff handler<br/>(registered)

  Op->>G: deploy_challenges + plant_initial_flags
  Op->>G: start(run_dir)
  G->>Bus: subscribe FileSink<br/>+ build ArtifactStore
  G->>Bus: emit SERVICE_DEPLOYED + FLAG_PLANTED (R0)
  G->>G: install_all_agents() (parallel)
  G->>Bus: AGENT_INSTALLED x N (one per team)
  G->>G: dry_run_all_agents() (parallel)
  G->>Bus: DRY_RUN_START / DRY_RUN_OK · _FAILED x N
  G->>RL: run()

  loop per round
    RL->>Bus: ROUND_START
    RL->>Bus: PHASE_CHANGE → DEFENSE
    Note over Bus,H: rotation subscriber fires FIRST<br/>(FLAG_PLANTED before kickoff)
    Bus->>H: defense_kickoff_handler (per team)
    H-->>RL: deploy_complete via MCP → end_phase_early
    RL->>Bus: PHASE_CHANGE → ATTACK
    Bus->>H: attack_kickoff_handler (per team)
    H-->>Bus: FLAG_SUBMITTED (via REST or MCP)
    RL->>Bus: ROUND_STOP
  end

  RL->>Bus: GAME_END
  Op->>G: stop()
  Note over Op: write_snapshot(run_dir)<br/>→ runs/<ts>/panel.html
```

## File map

| Path | Role |
|---|---|
| `gamemgr/game_mgr.py` | `GameMgr` (the class) |
| `gamemgr/round.py` | `RoundLoop` + `Phase` enum |
| `gamemgr/events.py` | `EventBus` + `EventType` + `Event` + `FileSink` |
| `gamemgr/flag.py` | `FlagStore` + `new_flag` + `SubmitResult` |
| `gamemgr/artifacts.py` | `ArtifactStore` + `ArtifactKind` |
| `gamemgr/config.py` | `GameConfig` + `ChallengeConfig` + `RoundConfig` + YAML loader |
| `gamemgr/api.py` | FastAPI `build_app(game, shutdown)` |
| `gamemgr/mcp_server.py` | MCP tools (submit_flag, report_ready, restart_service, …) |
| `gamemgr/panel_snapshot.py` | `write_snapshot(run_dir)` |
