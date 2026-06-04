# Challenge interface v2 — pluggable Challenge ABC

> Status: planned, not yet executed.
> Driver: extensibility. Adding a new CTF challenge today touches
> `challenges/source.py` (ChallengeSpec metadata schema), `FlagServer`
> (per-mechanism plant dispatch), `HealthChecker` (hardcoded 4-level
> chain reading `spec.metadata.health_check.*`), `agents/prompts/*`
> (attack/defense prompts), and `orchestrator/service.py` (Service
> adapter for SERVICE_PROTOCOL plants). Five different files per
> challenge. v2 collapses all of it into one directory per challenge
> behind a single ABC root.

## Why now

Two challenges in tree (notes + nautilus-ico). Three is where the
per-mechanism dispatch in framework code starts to ossify
(SERVICE_PROTOCOL / FILE_AT_PATH / ENV_VAR / BINARY_PATCH enum is
already speculative coverage). Cleaner to push that dispatch into
each challenge's own `FlagHandler` and stop maintaining a
mechanism-enumeration table at the framework level.

The DEFCON live runs (2026-06-03) also showed how spread out
challenge knowledge is — debugging "where does the 4-level chain
get its checker_command from?" walked through 3 files. After v2,
"how does ico plant flags?" → open `challenges/nautilus_ico/flag_handler.py`.

## Target structure

```
CyberArenaOrchestration/
  challenges/
    interface/                ← moved from /challenge interface/
      __init__.py             # re-exports public surface
      challenge.py            # Challenge, VulboxImage, ChallengeDocs
      probe.py                # VulboxProbe, FunctionalityTest, FlagHandler,
                              # RestartHandler (NEW), VulboxTarget, CheckResult
      UML.md
    notes/                    # game-simple notes (attack-only)
      __init__.py             # exports NotesChallenge()
      challenge.py            # NotesChallenge(Challenge)
      functionality_test.py   # NotesFunctionalityTest — port-connect + LIST/COUNT
      flag_handler.py         # NotesFlagHandler — protocol-level PUT/GET
      prompts.py              # attack + defense prompts (defense unused for notes)
    nautilus_ico/             # DEFCON ico
      __init__.py
      challenge.py            # IcoChallenge(Challenge)
      functionality_test.py   # IcoFunctionalityTest — pgrep + tcp + checker.sh
      flag_handler.py         # IcoFlagHandler — docker exec write /flag
      restart_handler.py      # IcoRestartHandler — docker exec /arena/restart.sh
      prompts.py              # attack + defense prompts
      spec.toml               # kept (image_tag pin, vendor source_view, etc.)
    registry.py               # name → Challenge instance lookup
```

## Abstract interface — additions

Extend [challenges/interface/probe.py](../challenges/interface/probe.py)
with a third probe alongside `FunctionalityTest` and `FlagHandler`:

```python
class RestartHandler(VulboxProbe):
    """Rebuild + restart the service in-place after agent defense edits.

    Defense-only. Challenges without a defense workflow (e.g. notes)
    leave Challenge.restart_handler = None — the orchestrator skips
    DEFENSE_KICKOFF for such challenges.

    The orchestrator runs this under privileged access (root via
    docker exec); the agent only triggers it indirectly through the
    MCP `restart_service` tool. This keeps the restart script
    untouchable by the agent (root-owned 555 inside the image).
    """

    @abstractmethod
    def run(self, target: VulboxTarget) -> CheckResult: ...
```

And [challenges/interface/challenge.py](../challenges/interface/challenge.py)
gains one field:

```python
@dataclass(frozen=True)
class Challenge(ABC):
    ...
    restart_handler: RestartHandler | None = None
```

## Architecture diff — facility-by-facility

### FlagServer

```
v1 (today):
  FlagServer.plant_flags(round_id):
    for (team_id, service_name), service in _service_by_owner.items():   # SERVICE_PROTOCOL
        service.plant_flag(handle, new_flag())
    for (team_id, service_name), spec in _spec_by_owner.items():         # FILE_AT_PATH
        docker exec sh -c "printf %s flag > flag_path && chmod 644 flag_path"

v2:
  FlagServer.plant_flags(round_id):
    for team in topology.teams:
        for challenge in registry.deployable_on(team.id):
            target = game.target_for(team.id, challenge)
            flag = new_flag()
            handle = challenge.flag_handler.plant(target, flag)
            store.plant(team.id, challenge.name, flag, handle)
```

Dispatch by mechanism disappears from the framework. `FlagMechanism`
enum is retired. Each challenge's `flag_handler.plant` decides for
itself how the flag enters the vulbox.

### HealthChecker

```
v1 (today):
  HealthChecker.run_chain(team, service, container, spec):
    pgrep(spec.metadata.health_check.process_pattern)         # PROCESS
    tcp-connect(spec.port)                                    # TCP
    Service.is_alive(handle, PROTOCOL)  if adapter wired      # PROTOCOL
    docker exec spec.metadata.health_check.checker_command    # CHECKER
    # 4 SERVICE_HEALTH_CHECK events emitted, one per level

v2:
  HealthChecker.run_chain(team, challenge):
    target = game.target_for(team, challenge)
    tree   = challenge.functionality_test.run(target)         # CheckResult tree
    for leaf in flatten(tree):
        timeline.record(SERVICE_HEALTH_CHECK, level=leaf.name,
                        passed=leaf.passed, detail=leaf.detail)
```

The 4-level convention (PROCESS / TCP / PROTOCOL / CHECKER) survives
**as recommended naming** for CheckResult children — panel rendering
and operator habits don't change. Per-challenge `FunctionalityTest`
returns a tree with those four children by default; a non-DEFCON-style
challenge is free to return a different shape (one leaf, three leaves,
nested groups). `HealthLevel` enum stays in `challenges/interface/`
as that recommended-name constant; nothing in the framework branches
on its values anymore.

### RestartHandler / restart_service MCP tool

```
v1:
  GameMgr.restart_service(team_id, service):
    spec = _resolve_spec(service)
    restart_cmd = spec.metadata.restart_cmd       # framework reads spec
    asyncio.create_task(_run_restart_job(...)):
        docker exec restart_cmd
        timeline.record(SERVICE_RESTART_STARTED / COMPLETED / FAILED)
        await health_poller.trigger_now(team, service)

v2:
  GameMgr.restart_service(team_id, service):
    challenge = registry.get(service)
    if challenge.restart_handler is None:
        raise ValueError("not defense-capable")
    asyncio.create_task(_run_restart_job(...)):
        target = game.target_for(team_id, challenge)
        result = await challenge.restart_handler.run(target)    # challenge runs the script
        timeline.record(SERVICE_RESTART_STARTED / COMPLETED / FAILED)
        await health_poller.trigger_now(team_id, service)
```

The "which command to exec" decision moves from spec metadata into
each challenge's `RestartHandler`. `_run_restart_job` becomes a thin
wrapper: in-flight guard + record START/COMPLETED/FAILED + delegate to
the handler + trigger health probe.

The DEFENSE-capable filter (`_spawn_defense_kickoff`) changes from
`(spec.metadata or {}).get("restart_cmd")` to
`challenge.restart_handler is not None` — same intent, cleaner check.

## VulboxTarget construction

Facilities never built `VulboxTarget` before — `Service.plant_flag`,
`HealthChecker`, etc. took raw `handle`/`container_name`/`spec` args.
v2 introduces one helper on GameMgr:

```python
def target_for(self, team_id: str, challenge: Challenge) -> VulboxTarget:
    team = self._team(team_id)
    return VulboxTarget(
        host=team.prod,                              # container name on docker daemon
        ports=dict(challenge.vulbox.exposed_ports),  # named-port map
        meta={
            "team_id": team_id,
            "container_id": ...,
            "exec_in_container": self._exec_in_container,   # callable for handlers that docker-exec
        },
    )
```

Each handler is then framework-agnostic: it gets a `target.host`, a
named-port map, and a meta dict. Today's handlers will pull
`exec_in_container` out of `target.meta` to run docker exec — that's
the bridge between the abstract interface and the docker-exec world
we live in. Future transports (k8s, real SSH) replace what's in
`meta`; handler code is untouched.

## Migration stages

| Stage | Action | Files / area |
|---|---|---|
| 0 | Move `/challenge interface/` → `CyberArenaOrchestration/challenges/interface/`. Add `RestartHandler`. Update UML.md (add probe + arrows). | abstract interface |
| 1 | Write per-challenge code under `challenges/notes/` and `challenges/nautilus_ico/` — one file each for `challenge.py`, `functionality_test.py`, `flag_handler.py`, `prompts.py`, plus `restart_handler.py` for ico. | challenges/notes/*, challenges/nautilus_ico/* |
| 2 | `game.target_for(team_id, challenge)` helper on GameMgr. | gamemgr/game_mgr.py |
| 3 | `FlagServer`: replace mechanism dispatch with `challenge.flag_handler.plant(target, flag)`. Delete `_plant_file_at_path` + the SERVICE_PROTOCOL branch. | flag/server.py |
| 4 | `HealthChecker`: call `challenge.functionality_test.run(target)`, emit SERVICE_HEALTH_CHECK per leaf (`level = leaf.name`). | health/checker.py |
| 5 | `restart_service` path: delegate to `challenge.restart_handler.run(target)`. Defense-capable filter becomes `restart_handler is not None`. | gamemgr/game_mgr.py, gamemgr/mcp_server.py |
| 6 | Move prompts: `agents/prompts/notes.py` → `challenges/notes/prompts.py`; `agents/prompts/nautilus_ico.py` → `challenges/nautilus_ico/prompts.py`. `agents/prompts/__init__.py` keeps the by-name dispatcher, imports per-challenge modules. | challenges/*/prompts.py, agents/prompts/__init__.py |
| 7 | `challenges/registry.py`: name → Challenge instance lookup (replaces ChallengeSource ABC + BuiltinNotesSource + NautilusSource enumeration). | challenges/registry.py |
| 8 | Big cleanup: delete `challenges/source.py` (`ChallengeSpec`, `ChallengeSource`, `FlagMechanism`), `challenges/notes.py` (`BuiltinNotesSource`), `challenges/nautilus.py` (`NautilusSource`). Move `HealthLevel` to `challenges/interface/`. Drop `orchestrator/service.py` notes Service adapter (its plant logic lives in `NotesFlagHandler` now). | several files removed |
| 9 | `GameMgr.deploy_challenges`: pull image_tag / exposed_ports / env from `challenge.vulbox` instead of `_resolve_spec`. Drop `_resolve_spec`, `_spec_cache`, `_service_by_owner`, `_spec_by_owner`. | gamemgr/game_mgr.py |
| 10 | Tests: per-challenge unit tests (each `FunctionalityTest` / `FlagHandler` / `RestartHandler` against stubbed `target.meta["exec_in_container"]`). Migrate existing integration tests to construct Challenges directly. | tests/test_challenge_notes.py, tests/test_challenge_ico.py + updates to existing |
| 11 | Docs: refresh `docs/UML/challenges.md`, add Challenge + Probe ABCs to `docs/UML/overview.md` component diagram, update `docs/UML/flag.md` (drop mechanism dispatch table), update `docs/UML/health.md` (drop hard-coded 4-level wording). Update `docs/design_v1.7.md` Challenge row. | docs |

## Decisions pinned

- **restart_cmd home**: new `RestartHandler` probe subclass. Challenges with defense workflow expose one; attack-only challenges leave `Challenge.restart_handler = None`.
- **HealthLevel enum**: kept as recommended naming for CheckResult children. Framework doesn't branch on it; panel still renders `level` as a tag. Moved to `challenges/interface/probe.py` alongside CheckResult.
- **FlagMechanism enum**: retired. Each `FlagHandler.plant` is opaque to the framework.
- **Migration shape**: one-shot replacement. No adapter layer between v1 ChallengeSpec and v2 Challenge — there's enough test coverage now (186 passing, with FlagServer/HealthPoller already isolated as facilities) to do the swap atomically.
- **Prompts**: dispatch logic stays in `agents/prompts/__init__.py` (one entrypoint, lookup by challenge name). Per-challenge prompts.py defines the actual `build_attack_prompt(cfg)` / `build_defense_prompt(cfg, ...)` for that challenge.

## Risk + verification

- **Risk: stale spec.toml content** — ico's `spec.toml` carries `image_tag`, `port`, `flag_path`, `process_pattern`, `checker_command`, `interval_secs`, `restart_cmd`. After v2, `image_tag`/`port` go into `VulboxImage`, the rest are per-handler constants. The toml file may stay as a declarative input the IcoChallenge constructor reads, or be replaced with Python constants — open implementation choice in Stage 1.
- **Risk: VulboxTarget.meta becomes a junk drawer** — handlers reach into `meta["exec_in_container"]`. Document conventions in `challenges/interface/probe.py` and keep the key set small.
- **Risk: live-test gap** — unit tests + the existing DCF integration tests (test_dcf_health_levels, test_dcf_restart_tool, test_dcf_nautilus_ico, test_m5_flag) should all be migrated and pass. End-of-migration: one DEFCON live run with ico to confirm full path works.
- **No new event types** are introduced; SERVICE_HEALTH_CHECK / SERVICE_RESTART_*  / FLAG_PLANTED / FLAG_SUBMITTED / DEPLOY_COMPLETE shapes unchanged. Panel + JSONL persistence work without modification.

## After v2 — adding a new challenge

Becomes one PR touching only one directory:

```
challenges/<new_chal>/
  __init__.py
  challenge.py            # NewChallenge(Challenge)
  functionality_test.py
  flag_handler.py
  restart_handler.py      # only if defense-capable
  prompts.py
```

Plus one line in `challenges/registry.py` registering the new challenge.
Zero touches to `gamemgr/`, `flag/`, `health/`, or `agents/prompts/__init__.py`.
That's the test of whether v2 worked.
