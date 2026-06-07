# GameMgr layer — UML

`GameMgr` is the orchestration executor. It owns the timeline, the
round loop, the artifact store, and the per-team agent handlers, and
holds references to two independent facilities: the FlagServer and the
HealthPoller. Game state and per-flag/per-probe mechanics live in those
facilities; GameMgr keeps only the public method shapes
(`plant_initial_flags`, `submit_flag`, `restart_service`, `flags`)
that callers expect, and delegates the work.

**Post-bus refactor (2026-06).** GameMgr no longer subscribes to
events. Internal control flow is driven by direct method calls
(`enter_defense_phase`, `enter_attack_phase`, `record_deploy_complete`,
…). `Timeline` is observation-only — used by FileSink + WebSocket panel
consumers. See [timeline.md](timeline.md), [flag.md](flag.md), and
[health.md](health.md).

## Class diagram

```mermaid
classDiagram
  class GameMgr {
    +config: GameConfig
    +orchestrator: Orchestrator
    +timeline: Timeline
    +round_loop: RoundLoop
    +flag_server: FlagServer
    +flags: FlagStore  «property → flag_server.store»
    +artifacts: ArtifactStore
    +health_checker: HealthChecker
    +health_poller: HealthPoller
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
    +on_round_start(round_id, phase) None
    +enter_defense_phase(round_id) list~Task~
    +enter_attack_phase(round_id) list~Task~
    +on_round_stop(round_id, phase) None
    +on_game_end(round_id, phase, payload) None
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

  class Timeline {
    -_history: list
    -_sinks: list
    -_streams: list
    +record(type_, ...) Event
    +attach_sink(cb) None
    +attach_file_sink(path) None
    +attach_stream(*, maxsize) Queue
    +detach_stream(q) None
    +history() list
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

  GameMgr "1" *-- "1" Timeline : owns
  GameMgr "1" *-- "1" RoundLoop : owns
  GameMgr "1" *-- "1" FlagServer : owns
  GameMgr "1" *-- "1" ArtifactStore : owns (lazy in start())
  GameMgr "1" *-- "1" HealthPoller : owns (lazy in start())
  Timeline ..> Event : records
  RoundLoop ..> Phase : tracks
  RoundLoop ..> GameMgr : calls on_round_start / enter_*_phase / on_round_stop / on_game_end
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
    HealthPoller running (per-spec interval)
  end note
```

## RoundLoop ↔ GameMgr dispatch

Direct method calls — no pub-sub. Order is encoded in each method
body (e.g. `enter_defense_phase` rotates flags BEFORE spawning kickoff
tasks). Long-running work is fire-and-forget via `asyncio.create_task`
so phase transitions never block.

| Callback | Fires on | Job |
|---|---|---|
| `on_round_start(round_id, phase)` | top of each round | record ROUND_START |
| `enter_defense_phase(round_id)` | round-loop enters DEFENSE | record PHASE_CHANGE → stop HealthPoller → `_rotate_flags` (FLAG_PLANTED records) → `_spawn_defense_kickoff` (DEFENSE_KICKOFF records + per-team handler tasks) |
| `enter_attack_phase(round_id)` | round-loop enters ATTACK | record PHASE_CHANGE → `HealthPoller.on_attack_enter` (immediate probe + periodic loop per target) → `_spawn_attack_kickoff` |
| `on_round_stop(round_id, phase)` | bottom of each round | record ROUND_STOP → `_persist_service_logs` (docker logs → service-logs/) |
| `on_game_end(round_id, phase, payload)` | round-loop finally | record GAME_END |
| `record_deploy_complete(...)` | MCP `report_deploy_complete` | record DEPLOY_COMPLETE always; drain `_defense_pending` + `end_phase_early` only when current phase is DEFENSE (late reports from claude that flushed past the phase boundary record on the timeline but must not cut ATTACK short) |
| `record_agent_ready(team_id)` | MCP `report_ready` | record AGENT_READY + set `_ready_events[team_id]` |

## Game lifecycle sequence

```mermaid
sequenceDiagram
  participant Op as Operator (main.py)
  participant G as GameMgr
  participant RL as RoundLoop
  participant TL as Timeline
  participant HP as HealthPoller
  participant H as Kickoff handler<br/>(registered)

  Op->>G: deploy_challenges + plant_initial_flags
  Op->>G: start(run_dir)
  G->>TL: attach_file_sink + build ArtifactStore + build HealthPoller
  G->>TL: record SERVICE_DEPLOYED + FLAG_PLANTED (R0)
  G->>G: install_all_agents() (parallel)
  G->>TL: AGENT_INSTALLED x N (one per team)
  G->>G: dry_run_all_agents() (parallel)
  G->>TL: DRY_RUN_START / DRY_RUN_OK · _FAILED x N
  G->>RL: run()

  loop per round
    RL->>G: on_round_start
    RL->>G: enter_defense_phase
    G->>HP: on_attack_exit (stop polling)
    G->>TL: PHASE_CHANGE + FLAG_PLANTED + DEFENSE_KICKOFF
    G->>H: spawn defense_kickoff_handler (per team, fire-and-forget)
    H-->>G: deploy_complete via MCP<br/>→ record_deploy_complete<br/>→ end_phase_early
    RL->>G: enter_attack_phase
    G->>HP: on_attack_enter (immediate + periodic probes)
    G->>TL: PHASE_CHANGE + ATTACK_KICKOFF
    G->>H: spawn attack_kickoff_handler (per team, fire-and-forget)
    H-->>G: submit_flag → record FLAG_SUBMITTED
    RL->>G: on_round_stop
  end

  RL->>G: on_game_end (records GAME_END)
  Op->>G: stop()
  Note over Op: write_snapshot(run_dir)<br/>→ runs/<ts>/panel.html
```

## File map

| Path | Role |
|---|---|
| `gamemgr/game_mgr.py` | `GameMgr` (the class) |
| `gamemgr/round.py` | `RoundLoop` + `Phase` enum (calls back into GameMgr) |
| `gamemgr/timeline.py` | `Timeline` (append-only telemetry; replaces EventBus) |
| `gamemgr/events.py` | `EventType` + `Event` + `new_event` (data shapes only) |
| `flag/store.py` | `FlagStore` + `new_flag` + `SubmitResult` (in-memory state) |
| `flag/server.py` | `FlagServer` (planting + rotation + submit + timeline records) |
| `gamemgr/artifacts.py` | `ArtifactStore` + `ArtifactKind` |
| `gamemgr/config.py` | `GameConfig` + `ChallengeConfig` + `RoundConfig` + YAML loader |
| `gamemgr/api.py` | FastAPI `build_app(game, shutdown)` |
| `gamemgr/mcp_server.py` | MCP tools (submit_flag, report_ready, restart_service, …) |
| `gamemgr/panel_snapshot.py` | `write_snapshot(run_dir)` |
| `health/checker.py` | `HealthChecker` — runs the challenge's `FunctionalityTest`, emits one event per `CheckResult` leaf |
| `health/poller.py` | `HealthPoller` (ATTACK-only periodic + trigger_now) |
