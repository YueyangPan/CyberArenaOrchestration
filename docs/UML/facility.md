# Facility abstraction — UML

`facility/` defines the boundary between a facility (FlagServer,
HealthPoller, …) and its host (GameMgr): three ports bundled in a
`FacilityContext`, plus a thin `Facility` ABC. Stdlib-only; imports
nothing from `gamemgr` / `flag` / `health`, so the boundary stays
acyclic. Design rationale + the deferred Phase 2/3 lives in
[../facility_abstraction_plan_v1.md](../facility_abstraction_plan_v1.md).

## Class diagram

```mermaid
classDiagram
  class ExecResult {
    <<frozen>>
    +exit_code: int
    +stdout: str
    +stderr: str
    +timed_out: bool
  }

  class StatusPort {
    <<Protocol>>
    +status() dict
  }
  class EventSink {
    <<Protocol>>
    +record(type_, *, team_id, round_id, phase, payload)
  }
  class ExecPort {
    <<Protocol>>
    +exec(team_id, role, cmd, *, timeout_s) ExecResult
  }

  class FacilityContext {
    <<frozen>>
    +status: StatusPort
    +events: EventSink
    +exec: ExecPort?
  }
  class Facility {
    <<abstract>>
    +name: str
    +start()* 
    +stop()*
  }

  class LocalStatusPort
  class LocalEventSink
  class LocalExecPort

  LocalStatusPort ..|> StatusPort
  LocalEventSink ..|> EventSink
  LocalExecPort ..|> ExecPort
  FacilityContext o-- StatusPort
  FacilityContext o-- EventSink
  FacilityContext o-- ExecPort

  FlagServer --|> Facility
  HealthPoller --|> Facility
  FlagServer ..> FacilityContext : uses status + events
  HealthChecker ..> FacilityContext : uses status + events
```

`exec` is consumed by the *target builder* (`GameMgr.target_for`), not by
FlagServer/HealthChecker directly — they reach exec through the
`VulboxTarget.meta["exec_in_container"]` callable the builder installs.

## In-process wiring (today)

```mermaid
flowchart LR
  subgraph GameMgr
    status["status()"]
    tl["Timeline.record"]
    dx["_exec_in_container"]
    rc["_resolve_container"]
  end
  subgraph FacilityContext
    SP[LocalStatusPort] --> status
    ES[LocalEventSink] --> tl
    EP[LocalExecPort] --> rc
    EP --> dx
  end
  FlagServer --> SP
  FlagServer --> ES
  HealthChecker --> SP
  HealthChecker --> ES
  TF["target_for shim<br/>(container,cmd)->(rc,out)"] --> EP
  challenge["challenge handler<br/>flag_handler / functionality_test / restart_handler"] --> TF
```

`LocalExecPort` resolves `(team_id, role)` → container host-side, then runs
the blocking docker exec on a daemon thread with a `join(timeout_s)` deadline
so a hung exec can't wedge the round loop.

## Future: containerized (Phase 2/3, not built)

Same facility code, different adapters — the boundary is unchanged:

```mermaid
flowchart LR
  subgraph FacilityContainer
    FlagServer2[FlagServer] --> HSP[HttpStatusPort]
    FlagServer2 --> HES[HttpEventSink]
    TF2[target_for shim] --> HEP[HttpExecPort]
  end
  subgraph GameMgr2[GameMgr]
    GS["GET /game/status"]
    EV["POST /facility/events<br/>(auth, source stamped server-side)"]
    EX["POST /facility/exec<br/>(auth, resolves container)"]
  end
  HSP --> GS
  HES --> EV
  HEP --> EX
```

## What it does NOT do

- **Not a daemon framework.** `Facility.start/stop` default to no-ops;
  facilities are still driven by GameMgr phase callbacks (`rotate`,
  `on_attack_enter`), not a self-owned loop.
- **Not a new event bus.** `EventSink` is a one-way append mirroring
  `Timeline.record`; nothing subscribes.
- **Does not change challenge code.** The `(container_name, cmd) -> (rc,
  output)` contract in `challenges/interface/probe.py` is untouched; the
  exec transport swap happens behind the `target_for` shim.

## File map

| Path | Role |
|---|---|
| `facility/ports.py` | `StatusPort` / `EventSink` / `ExecPort` + `ExecResult` |
| `facility/base.py` | `Facility` ABC + `FacilityContext` |
| `facility/adapters_local.py` | `Local*` in-process adapters |
| `facility/__init__.py` | re-exports |
