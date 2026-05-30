# Persistent-Memory Agent — Plan v1

## What we're building

**One-shot install per team. Per-phase `claude -p --continue`
launched directly from the host via `docker exec`. Memory carries
across phases via Claude CLI's `--continue`.**

No in-container daemon. No file IPC for control signals. The
orchestrator is the controller; Claude in the container is a
short-lived process spawned once per phase.

### Key architectural ideas

> **The persistent component is Claude CLI's session store, not any
> process.**
>
> Claude is short-lived per phase. Memory persistence is purely a
> filesystem property: `/root/.claude/projects/<workdir-cwd>/<id>.jsonl`
> survives between invocations. `--continue` resumes it.

> **Assumption — Claude CLI session selection is cwd-scoped.**
>
> `--continue` resumes the most-recent session under
> `/root/.claude/projects/<cwd-encoded>/`. We pin every claude
> invocation to `/tmp/agent/workdir/` so only this arena's sessions
> are visible. If a future Claude release breaks this, the manager
> will need to persist an explicit session id (`claude --resume <id>`)
> instead. The dedicated `workdir/` exists for this reason.

## Why no in-container daemon controlling the agent

An earlier draft put a Python daemon (`ClaudeAgentRunner`) inside the
dev container, fed by file IPC (`cue`, `kill`, `shutdown`, `done.json`),
to invoke Claude per phase. The daemon is unnecessary in **both**
modes we care about:

**Sync mode (what this plan builds).** The orchestrator drives every
phase. The daemon's job — invoke Claude, watch for exit, capture
output, escalate on timeout — collapses to a single host-side
`asyncio.create_subprocess_exec` call:

| Daemon feature | Host-driven equivalent |
|---|---|
| Stable PID across phases | `handle.name` (container) IS the stable identity |
| File IPC for cue / done | `subprocess.communicate()` returns directly |
| Timeout kill (SIGTERM → SIGKILL) | `asyncio.wait_for` + `proc.terminate()` / `.kill()` |
| Atomic `done.json` writes | host has the result in memory; no race |
| Transcript append | host writes artifacts directly |
| `--continue` session memory | works identically regardless of who invokes claude |

**Async mode (deferred).** The agent itself becomes the autonomous
loop — it calls MCP tools to query round state, decides whether to
defend or attack, and acts on its own clock. A daemon between
orchestrator and agent would have nothing to decide; both sides
already talk through MCP. The agent runs as either (a) one
long-running `claude -p` invocation that processes the whole game in
a single conversation, or (b) periodic short `claude -p --continue`
re-invokes driven by the orchestrator. Neither needs a daemon
mediating.

So the in-container daemon stays dropped permanently, not just
deferred. The persistent artifact across phases is Claude CLI's
session store on disk, not any process.

---

## Architecture

```
Host orchestrator (ClaudeAgentManager)
    │
    │ uses an AgentTransport (pluggable backend) for ALL
    │ side effects on the agent host. Two implementations:
    │   DockerExecBackend   — local: docker exec / docker cp
    │   SSHBackend          — remote VM / AWS: ssh + scp
    │
    │ For each phase:
    │   1. transport.write_file(handle, "/tmp/agent/prompt.txt", …)
    │   2. transport.write_file(handle, "/tmp/agent/phase_context.json", …)
    │   3. await transport.run_command(handle, [...claude...],
    │                                  stdin=…, timeout=phase_secs)
    │   4. Write per-phase artifacts on the host
    │
    ▼
team{N}_dev (today: docker container; tomorrow: remote VM)
    /tmp/agent/{agent.env, mcp.json, workdir/}   ← static, install once
    /root/.claude/projects/<workdir-cwd>/        ← Claude CLI session store
        └── <id>.jsonl   ← grows turn-by-turn; --continue resumes it
```

Per round:

```
R1 DEFENSE → host writes prompt+context → host docker-execs claude → claude exits → host collects artifacts
R1 ATTACK  → same
R2 DEFENSE → same
…
```

Each `claude` invocation passes `--continue`, so each one resumes the
prior conversation. Memory is just a file Claude reads at startup.

---

## Transport abstraction

`ClaudeAgentManager` never calls `docker exec` directly. All side
effects on the agent host go through an `AgentTransport` interface so
we can swap the local-Docker backend for a remote-SSH backend (or
anything else) without touching the manager.

```python
from typing import Protocol
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


class AgentTransport(Protocol):
    """Pluggable execution backend for one agent's host.

    Implementations: DockerExecBackend (local) and SSHBackend (remote VM).
    The manager never imports either directly — it takes whatever
    transport the config wires up.
    """

    async def connect(self, handle: AgentHandle) -> None:
        """One-time setup of the transport itself. For docker this is
        typically a no-op (`docker exec` is stateless). For SSH it
        establishes the connection / opens a control master so later
        run_command calls are cheap."""

    async def run_command(
        self,
        handle: AgentHandle,
        cmd: list[str] | str,
        *,
        stdin: bytes | None = None,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run cmd on the agent host. Returns exit_code + stdout +
        stderr + timed_out. On timeout the backend escalates SIGTERM →
        SIGKILL and surfaces `timed_out=True`."""

    def write_file(
        self,
        handle: AgentHandle,
        remote_path: str,
        content: bytes,
    ) -> None:
        """Write content to a path on the agent host. Atomic where
        cheap (tmp + rename); idempotent."""

    def read_file(self, handle: AgentHandle, remote_path: str) -> bytes:
        """Read a file from the agent host. Used to pull artifacts
        when the backend can't return them inline via run_command
        stdout (e.g. large transcript files)."""

    def kill_process(self, handle: AgentHandle, pattern: str) -> None:
        """Force-kill any process on the host matching pattern (e.g.
        'claude -p'). Belt-and-suspenders for the run_command timeout
        path — used after run_command has already returned timed_out=True
        if signals didn't propagate cleanly."""
```

`AgentHandle` is opaque to the manager. Each transport interprets it:
- `DockerExecBackend`: handle carries `container_name`.
- `SSHBackend`: handle carries `host`, `port`, `user`, key path, etc.

The two v1 implementations:

| Backend | `run_command` does | `write_file` does | `kill_process` does |
|---|---|---|---|
| `DockerExecBackend` | `asyncio.create_subprocess_exec("docker", "exec", "-i", name, ...)` | `docker exec -i name tee path` (or `docker cp` for binary) | `docker exec name pkill -KILL -f pattern` |
| `SSHBackend` (future) | `asyncio.create_subprocess_exec("ssh", host, ...)` with stdin piped through | `scp -` or `ssh host "cat > path"` | `ssh host pkill -KILL -f pattern` |

The manager-facing API is identical. Moving from local Docker to a
remote AWS VM is a config-only change:

```yaml
team1:
  agent_transport:
    type: docker   # or: ssh
    container: team1_dev          # docker-specific
    # host: 10.0.0.5              # ssh-specific
    # user: arena_agent
    # key: ~/.ssh/arena_team1
```

`v1 ships only `DockerExecBackend`. `SSHBackend` is a sibling file
that someone can write when we need to run agents on remote VMs.

---

## Lifecycle

### 1. Game start — `install()` runs ONCE per team

| Step | Action |
|---|---|
| 1 | `npm install -g @anthropic-ai/claude-code` |
| 2 | `_provision_ssh_pivot` (existing M5.6 SSH key push) |
| 3 | Write `/tmp/agent/{mcp.json, agent.env}` |
| 4 | `mkdir -p /tmp/agent/workdir` (claude's isolated cwd) |
| 5 | Emit `AGENT_INSTALLED` |

Nothing launches persistently. The next phase's `run_phase` is what
actually starts Claude for the first time.

### 2. Each phase — `run_phase()`

```python
result: DoneInfo = await mgr.run_phase(
    handle, prompt_text, round_id=N, phase="DEFENSE",
    timeout=phase_secs,
)
# → emits AGENT_PROMPT_SENT  (before subprocess starts)
# → emits AGENT_COMPLETED    (clean exit, exit_code 0)
# → emits AGENT_TIMEOUT      (timer expired; subprocess was killed)
# → emits AGENT_FAILED       (non-zero exit, not from timeout)
```

`run_phase` is `async` so the FastAPI event loop stays free to serve
the agent's MCP tool calls during its turn. Steps:

1. Write `/tmp/agent/prompt.txt` and `/tmp/agent/phase_context.json`
   into the container (provenance — prompt.txt is also what Claude
   reads via stdin redirect).
2. Emit `AGENT_PROMPT_SENT`.
3. `asyncio.create_subprocess_exec` of `docker exec ... claude -p --continue …`.
4. `await asyncio.wait_for(proc.communicate(), timeout=phase_secs)`.
5. On `TimeoutError`: `proc.terminate()` → 2s grace → `proc.kill()` if
   still alive → `docker exec team_dev pkill -KILL -f 'claude -p'` as
   belt-and-suspenders (in case signals don't propagate cleanly
   through `docker exec`).
6. Write stdout / stderr / metadata into the artifact dir on the host.
7. Emit `AGENT_COMPLETED` / `AGENT_TIMEOUT` / `AGENT_FAILED`.

### 3. Game end

Nothing to tear down inside the containers. `Orchestrator.teardown()`
reaps the dev containers as today; the session file under
`/root/.claude/projects/` dies with the container.

---

## `ClaudeAgentManager.run_phase` (host-side; ~30 LOC)

```python
import asyncio, json, time
from dataclasses import dataclass


@dataclass(frozen=True)
class DoneInfo:
    exit_code: int
    timed_out: bool
    started_at: float
    ended_at: float
    duration_ms: int
    stdout_path: str   # path in the host artifact dir
    stderr_path: str


class ClaudeAgentManager:
    def __init__(self, transport: AgentTransport, ...) -> None:
        self.transport = transport
        # ...

    # install() reduced to one-shot setup using transport primitives:
    #   transport.run_command(handle, ["npm", "install", "-g", ...])
    #   _provision_ssh_pivot  (still uses transport.run_command + write_file)
    #   transport.write_file(handle, "/tmp/agent/mcp.json", ...)
    #   transport.write_file(handle, "/tmp/agent/agent.env", ...)
    #   transport.run_command(handle, ["mkdir", "-p", "/tmp/agent/workdir"])
    #   emit AGENT_INSTALLED
    # No process is launched here.

    async def run_phase(
        self,
        handle: AgentHandle,
        prompt: str,
        *,
        round_id: int,
        phase: str,
        timeout: float,
    ) -> DoneInfo:
        # 1. Persist prompt + context for provenance.
        ctx = json.dumps({"round_id": round_id, "phase": phase})
        self.transport.write_file(handle, "/tmp/agent/prompt.txt", prompt.encode())
        self.transport.write_file(handle, "/tmp/agent/phase_context.json", ctx.encode())

        started_at = time.time()
        # 2. Run claude through the transport. Transport handles the
        #    timeout escalation (SIGTERM → SIGKILL) and surfaces a
        #    CommandResult with stdout/stderr/exit_code/timed_out.
        result: CommandResult = await self.transport.run_command(
            handle,
            [
                "bash", "-c",
                "cd /tmp/agent/workdir && "
                "set -a && . /tmp/agent/agent.env && set +a && "
                "claude -p --continue "
                "--output-format stream-json --verbose "
                "--dangerously-skip-permissions "
                "--model \"$CLAUDE_MODEL\" "
                "--mcp-config /tmp/agent/mcp.json "
                "< /tmp/agent/prompt.txt",
            ],
            timeout=timeout,
        )

        if result.timed_out:
            # Belt-and-suspenders for transports where SIGTERM may not
            # propagate through (e.g. docker exec).
            self.transport.kill_process(handle, "claude -p")

        ended_at = time.time()

        # 3. Write artifacts on the host directly.
        artifact_dir = self._artifact_dir(handle, round_id, phase)
        (artifact_dir / f"stream-{phase}.jsonl").write_bytes(result.stdout)
        (artifact_dir / f"agent-{phase}.log").write_bytes(result.stderr)
        (artifact_dir / f"metadata-{phase}.json").write_text(json.dumps({
            "round_id": round_id,
            "phase": phase,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": int((ended_at - started_at) * 1000),
            "prompt_first_line": prompt.splitlines()[0] if prompt else "",
        }))

        return DoneInfo(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=int((ended_at - started_at) * 1000),
            stdout_path=str(artifact_dir / f"stream-{phase}.jsonl"),
            stderr_path=str(artifact_dir / f"agent-{phase}.log"),
        )
```

Notice what the manager DOESN'T do: subprocess management, signal
escalation, transport-specific quirks (e.g. `docker exec` signal
propagation). All of that lives behind `AgentTransport`.

`subprocess` / `asyncio` cover everything the runner would do
manually: process lifecycle, timeout escalation, exit-code capture,
output collection. No helper layer needed.

**Test surface**: `run_phase` is testable by mocking
`asyncio.create_subprocess_exec` to return a fake process with
controllable `returncode` / `communicate()` / `terminate()` / `kill()`.
One docker-gated end-to-end test exercises install + 2-phase
`run_phase` and verifies session memory carries via `--continue`.

---

## File layout

In the container — written once at install, read on each phase:

| Path | Lifecycle |
|---|---|
| `/tmp/agent/agent.env` | static; sourced inside the docker-exec bash command |
| `/tmp/agent/mcp.json` | static; passed via `--mcp-config` |
| `/tmp/agent/workdir/` | static dir; claude's cwd for `--continue` session isolation |
| `/tmp/agent/prompt.txt` | overwritten per phase (provenance; Claude reads it via `< prompt.txt`) |
| `/tmp/agent/phase_context.json` | overwritten per phase (provenance only, not consumed at runtime) |
| `/root/.claude/projects/<workdir-cwd>/<id>.jsonl` | Claude CLI session store; grows turn-by-turn |

On the host — populated per phase by `run_phase`:

| Artifact path | What it is |
|---|---|
| `runs/<ts>/artifacts/team{N}/round-{N}/stream-{phase}.jsonl` | Claude's stdout (stream-json transcript) |
| `runs/<ts>/artifacts/team{N}/round-{N}/agent-{phase}.log` | Claude's stderr |
| `runs/<ts>/artifacts/team{N}/round-{N}/metadata-{phase}.json` | `{round_id, phase, exit_code, timed_out, duration_ms, prompt_first_line, started_at, ended_at}` |

No `cue` / `done.json` / `runner.pid` / `kill` / `shutdown`. None of
those exist in this design.

---

## Manager ↔ Container interaction

```
Host orchestrator                          team{N}_dev container
   │                                           │
   │ install():                                │
   │  docker exec write env/mcp.json/workdir ► │
   │  emit AGENT_INSTALLED                     │
   │                                           │
   │ ── per phase ────────────────────────────  │
   │  docker exec write prompt + context ────► │
   │  emit AGENT_PROMPT_SENT                   │
   │  docker exec claude -p --continue ──────► │ ◄── new claude process
   │  await proc.communicate(timeout=phase)    │     reads & updates
   │                                           │     session.jsonl
   │  on timeout: terminate / kill / pkill ──► │ ◄── claude killed
   │  ◄── stdout, stderr, exit_code            │     claude exits
   │  write artifacts on host                  │
   │  emit AGENT_COMPLETED / TIMEOUT / FAILED  │
   │                                           │
   │ ── next phase ───────────────────────────  │
```

No asynchronous handoff. Each `run_phase` is a blocking `await` on
the host. The agent's MCP tool calls (`submit_flag`, `restart_service`,
`report_deploy_complete`) flow back over the existing FastMCP transport
to the orchestrator's FastAPI process, unaffected by the docker-exec
subprocess.

---

## Events

Reuse existing:
- `AGENT_INSTALLED` — once per team after install completes.
- `AGENT_READY` — agent called MCP `report_ready` (unchanged).

Drop:
- `AGENT_LAUNCHED` — there's no persistent launch. (Or keep firing it
  alongside `AGENT_INSTALLED` for back-compat with M4.5 tests; TBD
  during implementation.)

New (per-phase):
- `AGENT_PROMPT_SENT` — before subprocess starts. Payload: `{phase, round_id, prompt_first_line}`.
- `AGENT_COMPLETED` — clean exit. Payload: `{exit_code, duration_ms, phase, round_id}`.
- `AGENT_TIMEOUT` — phase timer expired; subprocess killed. Same shape.
- `AGENT_FAILED` — non-zero exit, not from timeout. Same shape + `error_excerpt`.

---

## Code changes

| File | Change |
|---|---|
| `agents/transport.py` (new) | `AgentTransport` Protocol + `CommandResult` + `AgentHandle` types. Pure interface module; no implementations. |
| `agents/transport_docker.py` (new) | `DockerExecBackend` — `run_command` via `asyncio.create_subprocess_exec("docker", "exec", ...)`, `write_file` via `docker exec ... tee`, `kill_process` via `docker exec ... pkill`. |
| `agents/transport_ssh.py` (deferred) | `SSHBackend` — sibling file for future remote-VM runs. Not in scope for v1. |
| `agents/claude.py` | `ClaudeAgentManager` takes an `AgentTransport` via `__init__`. `install()` becomes one-shot setup using `transport.run_command` + `transport.write_file` (no direct docker calls). New async `run_phase(handle, prompt, *, round_id, phase, timeout) → DoneInfo`. Existing `_launch` retires entirely. |
| `models/agent.py` | New `DoneInfo` frozen dataclass. (CommandResult / AgentHandle live in `agents/transport.py` to keep `models/` free of transport concerns.) |
| `gamemgr/game_mgr.py` | Add once-per-game install hook before the round loop (parallel via `asyncio.gather`). Kickoff handlers do `await run_phase(...)` instead of `install()`. Track first-prompt-per-team in `_agent_briefed: set[str]` so the long briefing only goes out once. |
| `gamemgr/events.py` | Add `AGENT_PROMPT_SENT`, `AGENT_COMPLETED`, `AGENT_TIMEOUT`, `AGENT_FAILED`. |
| `agents/prompts/*` | First-phase prompt = full briefing + first task. Subsequent prompts = 2–3 lines. |
| `panel/panel.html` | Render new event chips. |
| `main.py` | Construct `DockerExecBackend()` and pass it to `ClaudeAgentManager(transport=...)`. `_wire_defense_kickoff` → `_wire_phase_kickoffs` (no `install` in the kickoff path). |
| `tests/test_dcf_transport_docker.py` (new) | Mock `asyncio.create_subprocess_exec`; verify `run_command` timeout escalation, `write_file` round-trip, `kill_process` invocation. |
| `tests/test_dcf_run_phase.py` (new) | Unit-test `run_phase` against a fake `AgentTransport` (in-memory) — exercises the manager logic without any subprocess. One docker-gated live test wires the real `DockerExecBackend` and verifies `--continue` memory carries across two phases. |

**Existing tests** (`test_m4_agent.py`, `test_m4_5_install_emits.py`,
`test_m5_6_agent_attack.py`) each call `install()` once in isolation,
so they don't need behavioral changes — `install()`'s public shape
(params, callbacks) is preserved; only its internals (no longer
launching the supervisor) change. The on_installed callback still
fires; on_launched may need to either fire alongside on_installed or
be retired (TBD during implementation).

**Estimate**: ~120 LOC + 4 tests
(`AgentTransport` Protocol ~20 LOC, `DockerExecBackend` ~50 LOC,
`run_phase` + install refactor ~50 LOC; plus 1 transport test, 2
`run_phase` unit tests against a fake transport, 1 live docker
end-to-end).

---

## Risks

| Risk | Mitigation |
|---|---|
| `docker exec` doesn't forward SIGTERM cleanly to in-container claude | Belt-and-suspenders `docker exec team_dev pkill -KILL -f 'claude -p'` after `proc.kill()`. |
| `--continue` resumes wrong session | Dedicated `workdir/` cwd isolates sessions to this arena's run. |
| Container exits mid-phase (e.g. crashes) | `docker exec` subprocess fails immediately; surfaces as `AGENT_FAILED`. |
| MCP transport hiccups while `run_phase` is blocking | `run_phase` is async; FastAPI / FastMCP keep responding. |
| Conversation tokens grow unbounded | Deferred. Options: `--fork-session` at round boundary, compress-history MCP tool. |
| `/root/.claude/projects/` carries over from a previous run | Container is recreated by `Orchestrator.setup` each game; session store is wiped. |

---

## Async mode (deferred, NOT in scope)

When we want autonomous agents — decide what to do and when, defend
and attack in parallel — the agent itself becomes the driver. The
orchestrator stops sending per-phase prompts; the agent queries round
state via new MCP tools and acts on its own clock.

**No in-container daemon comes back here either.** The agent calls
MCP directly through the existing FastMCP transport; a daemon
mediating between the agent and the orchestrator would just be a
relay with nothing to decide.

Sketch:
- Add MCP tools: `get_round_state`, `list_opposing_prods`,
  `list_active_flags`.
- Replace `run_phase`-per-phase with a single `start_agent_session` at
  game start: one `docker exec ... claude -p --continue` invocation
  carrying a "you drive the loop yourself" prompt.
- Agent runs internally: polls MCP for state, calls action tools,
  decides when its work is done.

Two constraints v1 honors so async stays reachable:
- ❌ Don't bake phase-time-budgets into per-challenge prompts (so
  async-mode prompts can omit them).
- ✅ Keep MCP tools additive — never change existing tool shapes.

---

## Decisions (resolved)

| # | Decision | Choice |
|---|---|---|
| D1 | In-container daemon to control claude, or host-driven? | **Host-driven, permanently.** A controller daemon is redundant in sync mode (orchestrator does the controlling via `docker exec`) AND in async mode (agent self-controls via MCP). See "Why no in-container daemon" above. |
| D2 | Coexist with the old `install + _launch` model? | **Fully replace** for the game flow. `install()` keeps its public shape (params, callbacks) but no longer launches a supervisor; it just stages files. M4 / M4.5 / M5.6 tests stay valid since they exercise `install()` in isolation. |
| D3 | Per-phase artifacts? | **stdout + stderr + metadata.json** in `runs/<ts>/artifacts/team{N}/round-{N}/`. Written directly by the host from the subprocess result; no round-trip through the container. |
| D4 | One session per team, or separate defense/attack sessions? | **One session.** Operator-role model — defense knowledge informs attack and vice versa. |
| D5 | First-phase prompt = briefing + first task, or briefing only + task only per phase? | **Combined first prompt.** First `run_phase` carries briefing + first task. Subsequent prompts are 2–3 line task cues. |
| D6 | Manager talks to the agent host directly via `docker exec`, or through a transport interface? | **Transport interface.** Manager depends on an `AgentTransport` Protocol with `run_command` / `write_file` / `read_file` / `kill_process`. v1 ships `DockerExecBackend`; remote-VM runs (AWS, etc.) get an `SSHBackend` sibling later. Manager logic is unchanged when the backend swaps. |
