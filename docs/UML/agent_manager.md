# Agent layer — UML

Three orthogonal pieces compose to invoke one LLM-agent CLI for one
phase:

- **AgentManager** (ABC) — per-CLI recipe (data + 2 methods)
- **AgentTransport** (Protocol) — per-host-kind execution surface
- **agents.runner** — free function that composes the two

Adding a new CLI = one new `AgentManager` subclass (~50–80 LOC). Adding
a new host kind = one new `AgentTransport`. Neither change touches
the other axis.

## Class diagram

```mermaid
classDiagram
  class AgentManager {
    <<abstract>>
    +PROMPT_PATH: str
    +PHASE_KILL_PATTERN: str
    +install(container, cfg, *, on_installed) None
    +phase_command(*, model) list~str~
  }

  class ClaudeAgentManager {
    +PROMPT_PATH = "/var/lib/cyberarena/prompt.txt"
    +PHASE_KILL_PATTERN = "claude -p"
    +install(...) None
    +phase_command(model) list~str~
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
  }

  class DockerExecBackend {
    -_client: docker.DockerClient
    +run_command(...) CommandResult
    +write_file(...) None
    +read_file(...) bytes
    +kill_process(...) None
  }

  class CommandResult {
    +exit_code: int
    +stdout: bytes
    +stderr: bytes
    +timed_out: bool
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

  class runner {
    <<module>>
    +run_phase_via_transport(mgr, transport, handle, prompt, *, timeout, model) CommandResult
  }

  AgentManager <|-- ClaudeAgentManager
  AgentTransport <|.. DockerExecBackend
  registry ..> AgentManager : builds
  registry ..> AgentConfig : dispatches on provider
  runner ..> AgentManager : reads PROMPT_PATH<br/>+ PHASE_KILL_PATTERN<br/>+ phase_command()
  runner ..> AgentTransport : write_file +<br/>run_command +<br/>kill_process
  runner ..> CommandResult : returns
```

## Per-CLI extension point

Adding a new CLI (codex / gemini / openai_compat) is one new class:

```python
class CodexAgentManager(AgentManager):
    PROMPT_PATH = "/var/lib/cyberarena/prompt.txt"
    PHASE_KILL_PATTERN = "codex --resume"

    def install(self, container, cfg, *, on_installed=None):
        # npm install -g @openai/codex
        # write env, .codex/config.json, ssh pivot...

    def phase_command(self, *, model=None):
        return ["bash", "-c", "cd ... && codex --resume ... < $PROMPT_PATH"]
```

Then one line in `agents/registry.py`:
```python
REGISTRY = {"claude": ClaudeAgentManager, "codex": CodexAgentManager}
```

No copy-pasted host plumbing — the transport + runner already handle
that.

## One-phase sequence

```mermaid
sequenceDiagram
  autonumber
  participant Game as GameMgr
  participant Run as runner
  participant Mgr as AgentManager
  participant Tx as AgentTransport
  participant Host as Agent host
  participant Bus as EventBus

  Game->>Bus: AGENT_PROMPT_SENT (record_phase_started)
  Game->>Run: run_phase_via_transport(mgr, tx, handle, prompt, timeout)
  Run->>Mgr: phase_command(model)
  Mgr-->>Run: argv
  Run->>Tx: write_file(mgr.PROMPT_PATH, prompt)
  Tx->>Host: stage prompt.txt
  Run->>Tx: run_command(argv, timeout)
  Tx->>Host: spawn CLI
  Note over Host: CLI reads + appends<br/>session store (--continue)
  Host-->>Tx: stdout / stderr / exit_code
  Tx-->>Run: CommandResult
  alt timed_out
    Run->>Tx: kill_process(mgr.PHASE_KILL_PATTERN)
    Tx->>Host: pkill -KILL -f
  end
  Run-->>Game: CommandResult
  Note over Game: write 3 artifacts (record_phase_done)
  Game->>Bus: AGENT_COMPLETED · TIMEOUT · FAILED
```

## File map

| Path | Role |
|---|---|
| `agents/manager.py` | `AgentManager` ABC |
| `agents/claude.py` | `ClaudeAgentManager` + paths constants |
| `agents/transport.py` | `AgentTransport` Protocol + `CommandResult` + `AgentHandle` |
| `agents/transport_docker.py` | `DockerExecBackend` |
| `agents/runner.py` | `run_phase_via_transport` |
| `agents/registry.py` | `REGISTRY` + `build(cfg)` |
| `agents/prompts/__init__.py` | Prompt dispatcher + short cues |
| `agents/prompts/{notes,nautilus_ico}.py` | Per-challenge briefings |
| `models/agent.py` | `AgentConfig`, `AgentStatus`, `AgentState`, `AgentLogs` |
