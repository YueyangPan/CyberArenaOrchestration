# Persistent-Memory Agent — Plan v1

## What we're building

**Install once per team. Per phase, the orchestrator runs
`claude -p --continue` on the agent host through a pluggable
`AgentTransport`. Memory carries across phases via Claude CLI's
session store on disk.**

No in-container daemon. No file IPC for control signals. The
orchestrator is the controller; Claude is a short-lived process
spawned once per phase.

> **The persistent component is Claude CLI's session store on disk,
> not any process.** Claude exits between phases. `--continue`
> reloads the session under
> `/root/.claude/projects/<workdir-cwd>/<id>.jsonl` on the next
> invocation.

> **Assumption — Claude CLI session selection is cwd-scoped.** We
> pin every invocation to `/tmp/agent/workdir/` so `--continue` only
> sees this arena's sessions. If a future Claude release changes the
> cwd → session mapping, switch to explicit `--resume <id>`.

---

## Architecture

```
Host orchestrator (ClaudeAgentManager)
    │ all side effects via AgentTransport (pluggable backend)
    │   DockerExecBackend (v1) │ SSHBackend (deferred)
    ▼
agent host  (today: docker container; tomorrow: remote VM)
    /tmp/agent/{agent.env, mcp.json, workdir/}   ← static; install once
    /root/.claude/projects/<workdir-cwd>/        ← Claude session store
        └── <id>.jsonl   ← grows turn-by-turn; --continue resumes it
```

Per round, sync mode:

```
R1 DEFENSE  → write prompt → run_command(claude -p --continue) → artifacts
R1 ATTACK   → same
R2 DEFENSE  → same
…
```

Each invocation is a fresh process; `--continue` makes it one logical
conversation.

### Why no in-container daemon

An earlier draft put a Python daemon (`ClaudeAgentRunner`) inside the
container, fed by file IPC (`cue`, `kill`, `shutdown`, `done.json`).
Redundant in both modes:

| Daemon's job | Where it actually lives |
|---|---|
| Stable PID across phases | Container/host IS the stable identity |
| Cue / done file IPC | `transport.run_command` returns directly |
| SIGTERM → SIGKILL on timeout | Inside the transport's `run_command` |
| Atomic `done.json` | Result is in memory on the host |
| Transcript append | Host writes artifacts directly |
| `--continue` memory | Works regardless of invoker |

In **sync mode** the orchestrator controls phases. In **async mode**
the agent self-controls via MCP. A daemon between them has nothing to
decide. Drop permanently.

---

## Transport abstraction

`ClaudeAgentManager` never touches `docker exec` directly. All host
side effects go through `AgentTransport`, so swapping local Docker
for remote SSH is a backend change, not a manager change.

```python
@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


class AgentTransport(Protocol):
    async def connect(self, handle: AgentHandle) -> None:
        """One-time transport setup. No-op for docker; opens a control
        master / persistent connection for ssh."""

    async def run_command(
        self, handle: AgentHandle, cmd: list[str] | str,
        *, stdin: bytes | None = None, timeout: float | None = None,
        cwd: str | None = None, env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run cmd. Returns exit/stdout/stderr/timed_out. Backend
        handles its own SIGTERM → SIGKILL escalation on timeout."""

    def write_file(self, handle, remote_path: str, content: bytes) -> None: ...
    def read_file(self, handle, remote_path: str) -> bytes: ...

    def kill_process(self, handle, pattern: str) -> None:
        """Belt-and-suspenders after a run_command timeout — used when
        SIGTERM doesn't propagate cleanly (e.g. through `docker exec`)."""
```

`AgentHandle` is opaque to the manager. Each backend interprets it:

| Backend | handle carries | `run_command` | `write_file` | `kill_process` |
|---|---|---|---|---|
| `DockerExecBackend` (v1) | `container_name` | `docker exec -i ...` | `docker exec ... tee` | `docker exec ... pkill -KILL -f ...` |
| `SSHBackend` (deferred) | `host`, `user`, `key` | `ssh ...` | `ssh ... 'cat > path'` | `ssh ... pkill -KILL -f ...` |

---

## Lifecycle

### 1. Game start — `install()` once per team

1. `transport.run_command(["npm", "install", "-g", "@anthropic-ai/claude-code"])`
2. `_provision_ssh_pivot` (existing M5.6 SSH key push, now via transport)
3. `transport.write_file` → `/tmp/agent/mcp.json` and `agent.env`
4. `transport.run_command(["mkdir", "-p", "/tmp/agent/workdir"])`
5. Emit `AGENT_INSTALLED`

No process launches here. The next phase's `run_phase` is what first
starts Claude.

### 2. Each phase — `run_phase(handle, prompt, *, round_id, phase, timeout) → DoneInfo`

See full code below. In short:

1. `transport.write_file` → `prompt.txt` + `phase_context.json`
2. Emit `AGENT_PROMPT_SENT`
3. `await transport.run_command([... claude -p --continue ...], timeout=phase_secs)`
4. On `timed_out`: `transport.kill_process(handle, "claude -p")` belt-and-suspenders
5. Write `stream-{phase}.jsonl`, `agent-{phase}.log`, `metadata-{phase}.json` to the host artifact dir
6. Emit `AGENT_COMPLETED` / `AGENT_TIMEOUT` / `AGENT_FAILED`

### 3. Game end

Nothing to tear down inside the agent host. `Orchestrator.teardown()`
reaps the dev containers as today; the session store dies with them.

---

## `ClaudeAgentManager.run_phase` (~30 LOC)

```python
@dataclass(frozen=True)
class DoneInfo:
    exit_code: int
    timed_out: bool
    started_at: float
    ended_at: float
    duration_ms: int
    stdout_path: str
    stderr_path: str


class ClaudeAgentManager:
    def __init__(self, transport: AgentTransport, ...) -> None:
        self.transport = transport

    async def run_phase(
        self, handle: AgentHandle, prompt: str,
        *, round_id: int, phase: str, timeout: float,
    ) -> DoneInfo:
        ctx = json.dumps({"round_id": round_id, "phase": phase})
        self.transport.write_file(handle, "/tmp/agent/prompt.txt", prompt.encode())
        self.transport.write_file(handle, "/tmp/agent/phase_context.json", ctx.encode())

        started_at = time.time()
        result = await self.transport.run_command(
            handle,
            ["bash", "-c",
             "cd /tmp/agent/workdir && "
             "set -a && . /tmp/agent/agent.env && set +a && "
             "claude -p --continue "
             "--output-format stream-json --verbose "
             "--dangerously-skip-permissions "
             "--model \"$CLAUDE_MODEL\" --mcp-config /tmp/agent/mcp.json "
             "< /tmp/agent/prompt.txt"],
            timeout=timeout,
        )
        if result.timed_out:
            self.transport.kill_process(handle, "claude -p")
        ended_at = time.time()

        d = self._artifact_dir(handle, round_id, phase)
        (d / f"stream-{phase}.jsonl").write_bytes(result.stdout)
        (d / f"agent-{phase}.log").write_bytes(result.stderr)
        (d / f"metadata-{phase}.json").write_text(json.dumps({
            "round_id": round_id, "phase": phase,
            "exit_code": result.exit_code, "timed_out": result.timed_out,
            "started_at": started_at, "ended_at": ended_at,
            "duration_ms": int((ended_at - started_at) * 1000),
            "prompt_first_line": prompt.splitlines()[0] if prompt else "",
        }))

        return DoneInfo(
            exit_code=result.exit_code, timed_out=result.timed_out,
            started_at=started_at, ended_at=ended_at,
            duration_ms=int((ended_at - started_at) * 1000),
            stdout_path=str(d / f"stream-{phase}.jsonl"),
            stderr_path=str(d / f"agent-{phase}.log"),
        )
```

Subprocess management, signal escalation, and transport-specific
quirks are entirely behind `AgentTransport`. Unit-test `run_phase`
against an in-memory `FakeTransport`.

---

## File layout

In the agent host (written by transport; read by Claude or for
provenance):

| Path | Lifecycle |
|---|---|
| `/tmp/agent/agent.env`, `mcp.json` | static; install once |
| `/tmp/agent/workdir/` | static dir; claude's cwd for `--continue` isolation |
| `/tmp/agent/prompt.txt` | overwritten per phase; Claude reads via stdin redirect |
| `/tmp/agent/phase_context.json` | overwritten per phase; provenance only |
| `/root/.claude/projects/<workdir-cwd>/<id>.jsonl` | Claude CLI session store; grows turn-by-turn |

On the orchestrator host, per phase (written by `run_phase`):

| Artifact path | What |
|---|---|
| `runs/<ts>/artifacts/team{N}/round-{N}/stream-{phase}.jsonl` | Claude stdout (stream-json transcript) |
| `runs/<ts>/artifacts/team{N}/round-{N}/agent-{phase}.log` | Claude stderr |
| `runs/<ts>/artifacts/team{N}/round-{N}/metadata-{phase}.json` | `{round_id, phase, exit_code, timed_out, duration_ms, prompt_first_line, started_at, ended_at}` |

No `cue` / `done.json` / `runner.pid` / `kill` / `shutdown` — none
exist in this design.

---

## Manager ↔ host interaction

```
ClaudeAgentManager                  agent host (container or VM)
   │                                  │
   │ install():                       │
   │   transport.run_command (npm)    │
   │   transport.write_file (env/mcp) │
   │   emit AGENT_INSTALLED           │
   │                                  │
   │ ── per phase ──────────────────  │
   │   transport.write_file (prompt)  │
   │   emit AGENT_PROMPT_SENT         │
   │   transport.run_command(claude ──►│ claude -p --continue
   │     -p --continue, timeout)      │   reads + updates session.jsonl
   │   ◄── stdout, stderr, exit_code  │ claude exits
   │   on timed_out:                  │
   │     transport.kill_process ─────►│ pkill claude
   │   write artifacts on host        │
   │   emit AGENT_COMPLETED / TIMEOUT │
   │     / FAILED                     │
```

No async handoff; each `run_phase` is a blocking `await` on the
orchestrator. The agent's MCP tool calls flow back over the existing
FastMCP transport — they're independent of the subprocess.

---

## Events

Reuse: `AGENT_INSTALLED` (once per team), `AGENT_READY` (MCP).

New (per phase):
- `AGENT_PROMPT_SENT` — before subprocess starts. Payload: `{phase, round_id, prompt_first_line}`.
- `AGENT_COMPLETED` — clean exit (exit_code 0). Payload: `{exit_code, duration_ms, phase, round_id}`.
- `AGENT_TIMEOUT` — phase timer expired; subprocess killed. Same shape.
- `AGENT_FAILED` — non-zero exit, not from timeout. Same shape + `error_excerpt`.

Drop or keep-for-back-compat: `AGENT_LAUNCHED` (no persistent launch
anymore; decide during implementation).

---

## Code changes

| File | Change |
|---|---|
| `agents/transport.py` (new) | `AgentTransport` Protocol + `CommandResult` + `AgentHandle`. Pure interface. |
| `agents/transport_docker.py` (new) | `DockerExecBackend` — `asyncio.create_subprocess_exec("docker", "exec", ...)` under the hood. |
| `agents/transport_ssh.py` (deferred) | `SSHBackend` sibling for remote VMs. Out of scope for v1. |
| `agents/claude.py` | `ClaudeAgentManager(transport=...)`. `install()` becomes one-shot setup via transport primitives. New async `run_phase(...) → DoneInfo`. `_launch` retires. |
| `models/agent.py` | New `DoneInfo` frozen dataclass. |
| `gamemgr/game_mgr.py` | Pre-loop install hook (parallel via `asyncio.gather`). Kickoff handlers call `run_phase` instead of `install`. `_agent_briefed: set[str]` so the long briefing only goes out once. |
| `gamemgr/events.py` | Add `AGENT_PROMPT_SENT`, `AGENT_COMPLETED`, `AGENT_TIMEOUT`, `AGENT_FAILED`. |
| `agents/prompts/*` | First phase = full briefing + first task. Subsequent = 2–3 lines. |
| `panel/panel.html` | Render new event chips. |
| `main.py` | Construct `DockerExecBackend()` + inject. `_wire_defense_kickoff` → `_wire_phase_kickoffs`. |
| `tests/test_dcf_transport_docker.py` (new) | Mock `asyncio.create_subprocess_exec`; verify timeout escalation, write/read/kill paths. |
| `tests/test_dcf_run_phase.py` (new) | Manager logic against an in-memory `FakeTransport`; one docker-gated live test runs 2 phases and verifies `--continue` memory carries. |

**Existing tests** (M4 / M4.5 / M5.6) each call `install()` once in
isolation. `install()`'s public shape is preserved; only internals
change. No behavioral changes expected; re-run the suite to confirm.

**Estimate**: ~120 LOC + 4 tests.

---

## Risks

| Risk | Mitigation |
|---|---|
| Signals don't propagate through `docker exec` | `transport.kill_process` (pkill) after `run_command` times out |
| `--continue` resumes a wrong session | Dedicated `workdir/` cwd isolates this arena's sessions |
| Container exits mid-phase | `run_command` fails immediately → `AGENT_FAILED` |
| MCP transport hiccups while `run_phase` is awaiting | `run_phase` is async; FastAPI / FastMCP keep responding |
| Conversation tokens grow unbounded | Deferred: `--fork-session` at round boundary or compress-history MCP tool |
| Stale session store from a prior run | Container is recreated by `Orchestrator.setup` each game |

---

## Async mode (parking lot)

For autonomous agents (decide what to do, defend + attack in parallel,
query round state via MCP):
- Add MCP tools: `get_round_state`, `list_opposing_prods`, `list_active_flags`.
- Replace per-phase `run_phase` with one long-running `run_command` at
  game start carrying a "you drive yourself" prompt.
- Agent loops internally via MCP; orchestrator owns timers + flag
  rotation + network switching.

v1 constraints to keep async reachable:
- ❌ Don't bake phase-time-budgets into per-challenge prompts.
- ✅ Keep MCP tools additive; never reshape existing tools.

---

## Decisions (resolved)

| # | Decision | Choice |
|---|---|---|
| D1 | In-container daemon or host-driven? | **Host-driven, permanently.** Daemon is redundant in sync (orchestrator controls) AND async (agent self-controls via MCP). |
| D2 | Coexist with old `install + _launch`, or replace? | **Fully replace.** `install()` keeps its public shape; internals stop launching a supervisor. M4 / M4.5 / M5.6 tests stay valid. |
| D3 | Per-phase artifacts? | **stdout + stderr + metadata.json** under `runs/<ts>/artifacts/team{N}/round-{N}/`. |
| D4 | One session per team, or separate defense/attack sessions? | **One session.** Operator-role model; defense knowledge informs attack. |
| D5 | First-phase prompt = briefing + first task, or briefing-only at install + tasks later? | **Combined first prompt.** Subsequent prompts are 2–3 line task cues. |
| D6 | Manager talks to host directly, or through a transport interface? | **Transport interface.** `AgentTransport` Protocol; `DockerExecBackend` v1; `SSHBackend` deferred. Manager unchanged when backend swaps. |
