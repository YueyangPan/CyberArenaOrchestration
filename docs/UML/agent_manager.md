# Agent layer — UML

Two orthogonal abstractions:

- **AgentManager** (ABC) — per-CLI recipe: install + phase_command +
  the `run_phase` glue that drives one phase.
- **AgentTransport** (Protocol) — per-host-kind execution surface
  (docker exec today; SSH later).

Adding a new CLI = one new `AgentManager` subclass (~50–80 LOC).
Adding a new host kind = one new `AgentTransport`. Neither change
touches the other axis.

## Class diagram

```mermaid
classDiagram
  class AgentManager {
    <<abstract>>
    +PROMPT_PATH: str
    +PHASE_KILL_PATTERN: str
    +install(container, cfg, *, on_installed) None
    +phase_command(*, model, session_marker) list~str~
    +run_phase(transport, handle, prompt, *, timeout, model) CommandResult
  }

  class ClaudeAgentManager {
    +PROMPT_PATH = "/var/lib/cyberarena/prompt.txt"
    +PHASE_KILL_PATTERN = "claude -p"
    +install(...) None
    +phase_command(model, session_marker) list~str~
    +status(container) AgentState
    +kill(container) None
    +fetch_logs(container) AgentLogs
  }

  class AgentTransport {
    <<Protocol>>
    +connect(handle) None
    +run_command(handle, cmd, *, timeout, stdin, cwd, env) CommandResult
    +write_file(handle, path, content) None
    +read_file(handle, path) bytes
    +kill_process(handle, pattern) None
    +kill_session(handle, marker) None
  }

  class DockerExecBackend {
    -_client: docker.DockerClient
    +run_command(...) CommandResult
    +write_file(...) None
    +read_file(...) bytes
    +kill_process(...) None
    +kill_session(...) None
  }

  class CommandResult {
    +exit_code: int
    +stdout: bytes
    +stderr: bytes
    +timed_out: bool
    +pkilled_at: float?
  }

  class AgentConfig {
    +cli: str
    +provider: str
    +model: str
    +api_key: str
    +base_url: str
    +team_id: str
    +team_token: str
    +own_prod_host: str
    +opposing_prod_hosts: tuple
    +mcp_endpoint: str
    +system_prompt: str
  }

  class registry {
    <<module>>
    +REGISTRY: dict
    +build(cfg) AgentManager
  }

  AgentManager <|-- ClaudeAgentManager
  AgentTransport <|.. DockerExecBackend
  registry ..> AgentManager : builds
  registry ..> AgentConfig : dispatches on provider
  AgentManager ..> AgentTransport : run_phase writes file +<br/>runs command + kill_session
  AgentManager ..> CommandResult : returns
```

## Per-CLI extension point

Adding a new CLI (codex / gemini / openai_compat) is one new class —
inherited `run_phase` keeps the glue identical:

```python
class CodexAgentManager(AgentManager):
    PROMPT_PATH = "/var/lib/cyberarena/prompt.txt"
    PHASE_KILL_PATTERN = "codex --resume"

    def install(self, container, cfg, *, on_installed=None):
        # npm install -g @openai/codex
        # write env, .codex/config.json, ssh pivot...

    def phase_command(self, *, model=None, session_marker=None):
        return ["bash", "-c", "cd ... && codex --resume ... < $PROMPT_PATH"]
```

Then one line in `agents/registry.py`:

```python
REGISTRY = {"claude": ClaudeAgentManager, "codex": CodexAgentManager}
```

The transport handles host plumbing; `run_phase` on the ABC handles
the prompt-stage / invoke / kill-on-timeout dance — neither needs to
be reimplemented.

## One-phase sequence

```mermaid
sequenceDiagram
  autonumber
  participant Game as GameMgr
  participant Mgr as AgentManager
  participant Tx as AgentTransport
  participant Host as Agent host
  participant Bus as EventBus

  Game->>Bus: AGENT_PROMPT_SENT (record_phase_started)
  Game->>Mgr: run_phase(tx, handle, prompt, timeout)
  Mgr->>Tx: write_file(PROMPT_PATH, prompt)
  Tx->>Host: stage prompt.txt
  Mgr->>Mgr: phase_command(model, session_marker)
  Mgr->>Tx: run_command(argv, timeout)
  Tx->>Host: spawn CLI under setsid -w (env CYBERARENA_PHASE=<marker>)
  Note over Host: CLI reads PROMPT_PATH + appends<br/>session store (--continue)
  Host-->>Tx: stdout / stderr / exit_code
  Tx-->>Mgr: CommandResult
  alt timed_out
    Mgr->>Tx: kill_session(session_marker)
    Tx->>Host: PGID kill (best-effort) + SIGKILL pids w/ CYBERARENA_PHASE=<marker> in /proc/*/environ
  end
  Mgr-->>Game: CommandResult
  Note over Game: write 3 artifacts (record_phase_done)
  Game->>Bus: AGENT_COMPLETED · TIMEOUT · FAILED
```

## File map

| Path | Role |
|---|---|
| `agents/manager.py` | `AgentManager` ABC + `run_phase` glue |
| `agents/claude.py` | `ClaudeAgentManager` + paths constants |
| `agents/transport.py` | `AgentTransport` Protocol + `CommandResult` + `AgentHandle` |
| `agents/transport_docker.py` | `DockerExecBackend` |
| `agents/registry.py` | `REGISTRY` + `build(cfg)` |
| `agents/prompts/__init__.py` | Prompt dispatcher + shared helpers |
| `challenges/<name>/prompts.py` | Per-challenge `build_attack_prompt` / `build_defense_prompt` |
| `models/agent.py` | `AgentConfig`, `AgentStatus`, `AgentState`, `AgentLogs` |
