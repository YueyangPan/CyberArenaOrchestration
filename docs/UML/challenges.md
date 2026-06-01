# Challenge layer — UML

Pluggable challenge model: a **ChallengeSource** discovers challenges
(from a built-in module or a vendored upstream repo), produces a
**ChallengeSpec** per challenge, and the spec drives image builds +
flag plant + health checks. Two sources today: built-in `notes` and
upstream `nautilus`.

## Class diagram

```mermaid
classDiagram
  class ChallengeSource {
    <<abstract>>
    +list_challenges() list~ChallengeSpec~
    +build_image(spec) str
  }

  class BuiltinNotesSource {
    +list_challenges() list~ChallengeSpec~
    +build_image(spec) str
  }

  class NautilusSource {
    +include: list~str~
    +list_challenges() list~ChallengeSpec~
    +build_image(spec) str
    -_read_pin() _Pin
  }

  class ChallengeSpec {
    <<dataclass · frozen>>
    +name: str
    +source: str
    +port: int
    +image_tag: str
    +flag_mechanism: FlagMechanism
    +source_view: Path
    +metadata: dict
  }

  class FlagMechanism {
    <<enumeration>>
    SERVICE_PROTOCOL
    FILE_AT_PATH
  }

  class HealthLevel {
    <<enumeration>>
    PROCESS
    TCP
    PROTOCOL
    CHECKER
  }

  class Service {
    <<abstract>>
    +name: str
    +port: int
    +install(container) None
    +start(container) None
    +plant_flag(container, flag) None
    +is_alive(container, level) bool
  }

  class NotesService {
    +install(...) None
    +start(...) None
    +plant_flag(...) None
  }

  ChallengeSource <|-- BuiltinNotesSource
  ChallengeSource <|-- NautilusSource
  ChallengeSource ..> ChallengeSpec : produces
  ChallengeSpec ..> FlagMechanism : tagged with
  Service <|-- NotesService
  BuiltinNotesSource ..> NotesService : pairs with
```

## Two flag mechanisms — two deploy paths

```mermaid
flowchart TD
  Spec[ChallengeSpec.flag_mechanism]
  Spec -->|SERVICE_PROTOCOL| ServPath["GameMgr.deploy_challenges:<br/>build Service · service.install(handle) · service.start(handle)<br/>plant_flag → service.plant_flag (e.g. PUT over notes' TCP socket)<br/>example: notes"]
  Spec -->|FILE_AT_PATH| FilePath["GameMgr.deploy_challenges:<br/>service is baked into the image<br/>trigger spec.metadata.restart_cmd (e.g. /arena/restart.sh)<br/>plant_flag → docker exec write to spec.metadata.flag_path<br/>example: nautilus-ico"]
```

`spec.metadata` keys per mechanism:

| Key | When | Used by |
|---|---|---|
| `restart_cmd` | FILE_AT_PATH | `GameMgr.restart_service` — the MCP-triggered rebuild |
| `flag_path` | FILE_AT_PATH | `GameMgr._plant_file_at_path` |
| `health_check.process_pattern` | both | `_emit_health_chain` PROCESS step |
| `health_check.checker_command` | nautilus | `_emit_health_chain` CHECKER step (upstream poller) |
| `upstream_name` | nautilus | image tag computation |

## Health chain — 4 levels of "is the service alive"

```mermaid
graph LR
  Process[PROCESS<br/>pgrep -f]
  TCP[TCP<br/>socket.connect to port]
  Protocol[PROTOCOL<br/>Service.is_alive]
  Checker[CHECKER<br/>arena/checker.sh]
  Process --> TCP --> Protocol --> Checker
```

Each level is a deeper "are you really alive". Cheap sanity →
expensive gameplay correctness. Hollow patches that break gameplay
get caught at CHECKER even if PROCESS/TCP/PROTOCOL pass.

`_emit_health_chain` fires after every `restart_service` job AND
once at DEFENSE→ATTACK transition (`_on_defense_end_health_check`).
See [game_manager.md](game_manager.md) and the four health levels
discussion in [persistent_memory_agent_plan_v1.md](../persistent_memory_agent_plan_v1.md).

## Registry: how `main.py setup` picks sources

```mermaid
sequenceDiagram
  participant Main as main.py setup
  participant Reg as challenges.registry
  participant BN as BuiltinNotesSource
  participant N as NautilusSource

  Main->>Reg: build_all_images(cfg)
  Reg->>Reg: sources_for(cfg) — inspect challenges[].name
  alt has "notes"
    Reg->>BN: list_challenges() · build_image(spec)
  end
  alt any name starts with "nautilus-"
    Reg->>N: list_challenges(include=names)
    Reg->>N: build_image(spec) for each
  end
  Reg-->>Main: list of image tags
```

`NautilusSource` only instantiates if at least one challenge name
matches — saves pulling the vendored upstream repo when running a
notes-only game.

## File map

| Path | Role |
|---|---|
| `challenges/source.py` | `ChallengeSpec`, `ChallengeSource` ABC, `FlagMechanism`, `HealthLevel` enums |
| `challenges/notes.py` | `BuiltinNotesSource` + `NotesService` |
| `challenges/nautilus.py` | `NautilusSource` (vendored upstream challenges) |
| `challenges/nautilus/ico/spec.toml` | DEFCON framework spec for ico |
| `challenges/registry.py` | `default_sources` · `sources_for(cfg)` · `build_all_images(cfg)` · `build(type_)` |
| `vendor/nautilus-finals-2025/` | vendored upstream challenge tree (git-ignored) |
