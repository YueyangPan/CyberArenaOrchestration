# Flag facility — UML

`flag/` is an independent package consumed by `GameMgr`. Two classes:

- **`FlagStore`** — pure in-memory state. Owns the
  ACCEPTED/UNKNOWN/OWN/DUPLICATE decision logic. Re-planting a flag
  drops the old one from the inverted index (so it submits as UNKNOWN
  after rotation). No I/O, no timeline, no docker.
- **`FlagServer`** — orchestration over the store. Plants flags into
  challenge containers (SERVICE_PROTOCOL via the adapter's
  `plant_flag(handle, flag)`, FILE_AT_PATH via `docker exec` write),
  runs per-round rotation, verifies submissions, and records
  `FLAG_PLANTED` / `FLAG_SUBMITTED` on the Timeline.

GameMgr keeps the public surface (`plant_initial_flags`, `plant_flags`,
`submit_flag`, `flags`) and delegates every call to FlagServer.
Pluggable per `Service` ABC + `FlagMechanism` — no demo-service
specifics are hardcoded into the facility.

## Class diagram

```mermaid
classDiagram
  class FlagStore {
    +planted: dict~tuple~team,svc~,str~
    -by_flag: dict~str,tuple~
    -submissions: dict~str,set~
    +plant(team_id, service, flag) None
    +submit(submitter_team_id, flag) SubmitResult
  }

  class SubmitResult {
    <<frozen>>
    +accepted: bool
    +reason: str
    +victim_team_id: str?
    +service: str?
  }

  class FlagServer {
    +timeline: Timeline
    +topology: TopologyGraph
    +store: FlagStore
    -_service_by_owner: dict
    -_spec_by_owner: dict
    -_handle: HandleResolver
    -_exec: ExecFn
    -_status: StatusResolver
    -_last_planted_round: int
    +plant_initial_flags() list
    +plant_flags(round_id) list
    +rotate(round_id) None
    +submit(team_id, flag) SubmitResult
    +emit_planted_setup() None
  }

  class FlagMechanism {
    <<enumeration>>
    SERVICE_PROTOCOL
    FILE_AT_PATH
    ENV_VAR
    BINARY_PATCH
  }

  FlagServer "1" *-- "1" FlagStore : owns
  FlagStore ..> SubmitResult : returns
  FlagServer ..> FlagMechanism : dispatches plant by spec.flag_mechanism
  FlagServer ..> Timeline : records FLAG_PLANTED / FLAG_SUBMITTED
  FlagServer ..> Service : delegates SERVICE_PROTOCOL plant
```

## Plant dispatch

`plant_flags(round_id)` walks two mutable dicts that GameMgr.deploy_challenges
populates and FlagServer holds by reference:

| Path | Trigger | Mechanism |
|---|---|---|
| `_service_by_owner[(team, svc)] → Service` | spec.flag_mechanism = `SERVICE_PROTOCOL` | `service.plant_flag(handle, flag)` — adapter owns wire protocol (e.g. notes does a PUT) |
| `_spec_by_owner[(team, svc)] → ChallengeSpec` | spec.flag_mechanism = `FILE_AT_PATH` | `docker exec <prod> sh -c 'printf %s <flag> > <flag_path> && chmod 644 <flag_path>'` |

Both paths update `store.plant(team, svc, flag)` so submissions resolve
correctly afterwards. `ENV_VAR` and `BINARY_PATCH` mechanisms are
defined in `FlagMechanism` but not yet dispatched here — adding them
is one switch arm.

## Rotation lifecycle

```mermaid
sequenceDiagram
  participant Op as Operator (main.py)
  participant G as GameMgr
  participant FS as FlagServer
  participant TL as Timeline
  participant C as Container (prod)

  Op->>G: plant_initial_flags()
  G->>FS: plant_initial_flags()
  FS->>C: plant_flag(handle, flag)<br/>or docker exec write
  FS-->>G: list~(team, svc, flag)~
  Note over FS: emit_planted_setup() in start() records FLAG_PLANTED ×N

  loop per round
    G->>FS: rotate(round_id)   «from enter_defense_phase»
    FS->>FS: idempotency guard (skip if same round)
    FS->>C: plant_flag / docker exec write (per team×svc)
    FS->>TL: record FLAG_PLANTED ×N
  end

  Note over Op,FS: Submission path (MCP or REST)
  Op->>G: submit_flag(team_id, flag)
  G->>FS: submit(team_id, flag)
  FS->>FS: store.submit() — ACCEPTED / OWN / UNKNOWN / DUPLICATE
  FS->>TL: record FLAG_SUBMITTED (flag string NEVER in payload)
  FS-->>G: SubmitResult
  G-->>Op: SubmitResult
```

## What it does NOT do

- **Not a subscriber.** GameMgr drives rotation and submit calls
  directly; FlagServer doesn't listen to PHASE_CHANGE or anything else.
- **Doesn't own the kickoff handler set or `_defense_pending`** —
  those are defense-phase orchestration state on GameMgr.
- **Doesn't manage challenge deployment** — that's still
  `GameMgr.deploy_challenges()`; FlagServer just plants into already-
  deployed services.
- **Doesn't leak flag strings.** The flag never appears in
  `FLAG_PLANTED` or `FLAG_SUBMITTED` payloads — those go to the public
  panel + on-disk JSONL.

## File map

| Path | Role |
|---|---|
| `flag/__init__.py` | re-exports `FlagServer`, `FlagStore`, `SubmitResult`, `new_flag` |
| `flag/store.py` | `FlagStore` + `SubmitResult` + `new_flag` (state + decision logic) |
| `flag/server.py` | `FlagServer` (plant/rotate/submit + timeline records) |
