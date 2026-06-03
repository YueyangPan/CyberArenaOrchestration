# Cyber Arena — System Design (v1.7)

> v1.7 supersedes [design_v1.6.md](design_v1.6.md). It captures the
> post-DEFCON system: a pluggable `ChallengeSource` framework, the
> nautilus-ico challenge running on the unchanged M5.6 topology, and
> the agent defense loop (edit → MCP `restart_service` → smoke →
> `report_deploy_complete`). v1.6 stays as the prior baseline.

| Module | Status | Delta from v1.6 |
|--------|--------|-----------------|
| Orchestrator | ✅ | unchanged |
| GameMgr | ✅ | orchestration executor only — owns `Timeline`, holds FlagServer + HealthPoller; defense/attack kickoff dispatch via direct methods (`enter_defense_phase`/`enter_attack_phase`/`on_round_stop`/…); no longer subscribes to events |
| Timeline | ✅ replaces EventBus | append-only telemetry: `record()` + `attach_file_sink` + `attach_stream`; nothing internal subscribes |
| Flag facility | ✅ new `flag/` package | `FlagStore` (state + decision logic) + `FlagServer` (plant / rotate / submit + Timeline records); GameMgr keeps `plant_initial_flags` / `submit_flag` / `flags` as thin delegates |
| Health facility | ✅ new `health/` package | `HealthChecker` (4-level probe primitive) + `HealthPoller` (ATTACK-only periodic + on-demand `trigger_now`); driven by GameMgr at phase boundaries |
| Challenges | ✅ 2 adapters | `notes` (toy) + `nautilus-ico` via new `ChallengeSource` ABC |
| Agent (LLM) | ✅ | defense prompt; SSHes in as `arena_agent` (non-root) when target is a challenge prod |
| MCP server | ✅ 4 tools | + `restart_service(service) → {job_id}` |
| ArtifactStore | ✅ layout reserved | 3 kinds wired-ready (api/agent/service logs); 5 reserved |
| Observation Panel | ✅ | consumes via `timeline.attach_stream` (per-connection queue) instead of bus subscribe |
| Scoreboard | ⬜ deferred | as before |

---

## 1. Departures from v1.6

1. **`ChallengeSource` ABC + `ChallengeSpec` frozen dataclass** in
   `challenges/source.py`. Adapters: `BuiltinNotesSource`, `NautilusSource`.
   Spec carries name / source / port / image_tag / `flag_mechanism` /
   `source_view` / opaque `metadata`. `FlagMechanism` enum:
   `FILE_AT_PATH` / `ENV_VAR` / `BINARY_PATCH` / `SERVICE_PROTOCOL`.
2. **`HealthLevel` enum** (4 values: PROCESS / TCP / PROTOCOL / CHECKER).
   `Service.is_alive(handle, level=PROTOCOL)` — default keeps v1.6 callers
   backwards-compatible. CHECKER detects passive-echo cheese (socat
   stub passes PROCESS+TCP but not CHECKER).
3. **Async `restart_service(team_id, service) → job_id`.** MCP tool
   returns `{job_id}` immediately; orchestrator runs `spec.restart_cmd`
   via `docker exec` in a background asyncio task. Records
   `SERVICE_RESTART_STARTED` → `COMPLETED|FAILED` (with `duration_ms`)
   → per-level `SERVICE_HEALTH_CHECK` (via `HealthPoller.trigger_now`).
   Re-entrant call for the same `(team, service)` rejected at the MCP layer.
4. **`DEFENSE_KICKOFF` event + injected handler.** RoundLoop calls
   `game.enter_defense_phase(round_id)`, which rotates flags, records
   `DEFENSE_KICKOFF` per (team, defense-capable service), and spawns
   per-team handler tasks. Idempotent per `(round_id, team_id, service)`.
   Defense-capable ⇔ spec has `restart_cmd` (notes excluded).
4a. **Timeline + direct-method dispatch** (post-v1.7 bus refactor —
   2026-06). The legacy `EventBus` is replaced by `gamemgr.timeline.Timeline`:
   append-only telemetry only, no subscribers driving internal logic.
   PHASE_CHANGE / DEPLOY_COMPLETE / AGENT_READY etc. drive control flow
   via direct GameMgr methods (`enter_defense_phase`, `enter_attack_phase`,
   `on_deploy_complete`, `record_agent_ready` …). Slow / long-running
   work (kickoff handlers) is fire-and-forget via `asyncio.create_task`
   so no consumer can block another.
4b. **`HealthPoller`** (post-bus refactor) — independent facility in
   `health/`. Owns the 4-level probe (`HealthChecker.run_chain`).
   GameMgr drives it: `enter_attack_phase` → `poller.on_attack_enter`
   (immediate probe per defense-capable target + periodic loop),
   `enter_defense_phase` → `poller.on_attack_exit`. Restart-post probe
   uses `poller.trigger_now`. Per-spec `health_check.interval_secs`;
   default fallback when missing.
5. **`arena_agent` non-root SSH user inside challenge containers.**
   M5.6's "root SSH" was for attack-mode introspection; with defense
   mode + MCP-mediated restart the agent doesn't need root. Per-challenge
   Dockerfile creates `arena_agent`; sshd has `PermitRootLogin no`.
   `/srv/challenge/<name>/` owned `arena_agent:arena_agent 755`;
   `/arena/*.sh` + `/opt/challenge_src/<name>/` root-owned 555.
6. **Source baked into image, never bind-mounted.** Read-only template
   at `/opt/challenge_src/<chal>/`, copied to writable
   `/srv/challenge/<chal>/` in entrypoint. Per-container writable layer
   — no cross-team aliasing, no vendor-tree poisoning.
7. **Two-user model per challenge container.** `arena_agent` (SSH) +
   `ico` (service runtime). Pascal server `DropPrivileges('ico')` per
   child fork — missing the second user crashes the per-client fork
   mid-handshake.
8. **`ArtifactStore` (closed-set `ArtifactKind` enum, 8 slots).**
   Stable layout under `runs/<ts>/artifacts/<kind>/team{N}/round-{N}/`.
   `..` and absolute filenames rejected at allocator. v1 plans 3 kinds
   (api-logs / agent-logs / service-logs); 5 reserved.

---

## 2. Topology — **unchanged from v1.6**

Same M5.6 layout: one `team{N}_prod` per team; `team{N}_net` + `game_net`;
ATTACK attaches `game_net`, DEFENSE detaches. When a game runs ico,
each `team{N}_prod` is launched from `cyberarena/chal-nautilus-ico:<pin>`
instead of `cyberarena/prod-base` — the only delta is the prod image.
v1's per-(team, challenge) container split was considered and deferred.

---

## 3. Defense workflow

```
RoundLoop._enter_defense
  → game.enter_defense_phase(round_id):
      timeline.record(PHASE_CHANGE, phase=defense)
      health_poller.on_attack_exit()         # stop polling during DEFENSE
      _rotate_flags(round_id)                # plant + FLAG_PLANTED records
      _spawn_defense_kickoff(round_id):
        for each (team, defense-capable challenge):
          timeline.record(DEFENSE_KICKOFF)
          if handler registered:
            asyncio.create_task(handler(team_id, cfg, round_id, "defense", is_first))
```

Agent recipe (per `build_defense_prompt`):
1. SSH to own `team{N}_prod` as `arena_agent`.
2. Edit `/srv/challenge/<name>/` source.
3. Call `mcp__cyberarena__restart_service(service=<name>)` →
   returns `{job_id}` immediately.
4. TCP-probe `127.0.0.1:<port>` every few seconds until reachable.
5. Call `mcp__cyberarena__report_deploy_complete(...)`.

Restart timeline records (post-bus refactor):

```
SERVICE_RESTART_STARTED   { job_id, team_id, service, started_at }
SERVICE_RESTART_COMPLETED { job_id, team_id, service, duration_ms }
SERVICE_HEALTH_CHECK      { team_id, service, level=process,  passed, detail }   # via HealthPoller.trigger_now
SERVICE_HEALTH_CHECK      { team_id, service, level=tcp,      passed, detail }
SERVICE_HEALTH_CHECK      { team_id, service, level=protocol, passed, detail }
SERVICE_HEALTH_CHECK      { team_id, service, level=checker,  passed, detail }
DEPLOY_COMPLETE           { service, duration_ms }            # agent-emitted via MCP
```

Failure path: `SERVICE_RESTART_FAILED` with `{error, duration_ms}`;
no health chain runs.

During ATTACK, HealthPoller also fires the same `SERVICE_HEALTH_CHECK`
chain on its own cadence (per-spec `interval_secs`) for every
defense-capable target — so the panel reflects ongoing service health,
not just post-restart snapshots.

---

## 4. Container layout (nautilus-ico)

| Path | Owner | Mode | Notes |
|------|-------|------|-------|
| `/srv/challenge/ico/` | `arena_agent:arena_agent` | 755 | agent's edit surface (writable copy) |
| `/opt/challenge_src/ico/` | `root:root` | 555 | read-only template |
| `/arena/restart.sh`, `/arena/checker.sh`, `/arena/entrypoint.sh` | `root:root` | 555 | framework scripts, agent-readable but not writable |
| `/flag` | `root:root` | 644 | planted at deploy; ico's `InitAuthorData` reads on CONNECT |

Image: `cyberarena/chal-nautilus-ico:<short-sha-of-vendor-pin>`,
built `--platform=linux/amd64` (Pascal toolchain has no arm backend).

---

## 5. Event types (post-DEFCON)

| Event | New? | Source |
|-------|------|--------|
| `ROUND_START`, `PHASE_CHANGE`, `ROUND_STOP` | | round loop |
| `SERVICE_DEPLOYED`, `FLAG_PLANTED`, `FLAG_SUBMITTED` | | host |
| `AGENT_INSTALLED` / `LAUNCHED` / `READY` | M4.5 | host + MCP |
| `DEPLOY_COMPLETE` | M4.5 | agent (MCP) |
| `SERVICE_RESTART_STARTED` / `COMPLETED` / `FAILED` | **v1.7** | host |
| `SERVICE_HEALTH_CHECK` | **v1.7** | host (per level) |
| `DEFENSE_KICKOFF` | **v1.7** | host |

---

## 6. MCP tools (4)

| Tool | Returns | Errors |
|------|---------|--------|
| `submit_flag(flag)` | `{accepted, reason}` | (M4.5) |
| `report_ready()` | `{ok: true}` | (M4.5) |
| `report_deploy_complete(service, duration_ms)` | `{ok: true}` | InvalidParams on unknown service (M4.5) |
| `restart_service(service)` | `{job_id}` | InvalidParams on unknown service / no restart_cmd / in-flight |

All tools resolve team via `X-Team-Token` header. Bad token → generic
`Unauthorized`; never emits a bus event (oracle-hardening invariant
preserved from M4.5).

---

## 7. Configuration

`config/game.yaml` — unchanged single `challenges:` section. v1.7 ships
one defense-capable challenge per game; two-section
`challenge_sources:` + `challenges:` schema (per plan) deferred until a
second source lands.

`scripts/sync_nautilus.sh` pin-clones
`nautilus-institute/finals-2025` at SHA `366520c0cc43`. Vendor tree
gitignored; `NautilusSource.list_challenges()` raises with an
actionable message if not synced.

---

## 8. What's still deferred

(See [DEFCON_challenge_framework_report_v1.md](DEFCON_challenge_framework_report_v1.md) §Deferred for the full list.)

Headline items: real ico CHECKER (lift full poller), `ArtifactStore`
wiring into `fetch_logs()` + end-of-round `docker logs`,
`get_restart_status(job_id)` MCP tool, two-section game.yaml schema,
multi-challenge-per-team (per-(team, challenge) containers + 3-net
split), patch-quality scoring.
