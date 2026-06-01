from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AgentStatus(str, Enum):
    NOT_INSTALLED = "NOT_INSTALLED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    TERMINATED = "TERMINATED"
    CRASHED = "CRASHED"


@dataclass(frozen=True)
class AgentConfig:
    cli: str
    # Which AgentManager subclass to build. Populated from
    # game.yaml's `agent.provider` (or inferred from `cli`'s leading
    # token if missing). See `agents.registry.build` for the
    # dispatch table.
    provider: str = "claude"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    # Text fed to the CLI on stdin (claude reads it as the user prompt in
    # headless mode). Empty → a no-op placeholder is used; smoke tests and
    # M5 prompt-builder override this with concrete instructions.
    system_prompt: str = ""

    # ── M5.6 attack-mode fields (all optional; empty → no SSH provisioning) ──
    # The team this agent belongs to; carried through to agent.env as
    # CYBERARENA_TEAM_ID so the LLM can self-identify in shell commands.
    team_id: str = ""
    # Sent as X-Team-Token on /game/flag/submit (matches game.yaml.teams[].token).
    team_token: str = ""
    # Hostname the agent will SSH into to pivot (its own prod's docker DNS name).
    own_prod_host: str = ""
    # Container name on the local docker daemon — the AgentManager uses it
    # to inject the pubkey + start sshd as part of install(). When empty,
    # ClaudeAgentManager skips the SSH provisioning step.
    own_prod_container_name: str = ""
    # Docker DNS names of opposing prod containers — the agent's attack targets.
    opposing_prod_hosts: tuple[str, ...] = ()
    # Where the agent POSTs flag submissions (typically resolves via
    # host.docker.internal which is wired in by Node.extra_hosts).
    submit_url: str = ""
    # M4.5: MCP endpoint URL. Empty → no MCP, agent stays on the curl/REST
    # fallback path (lets non-MCP tests keep working unchanged). Non-empty
    # → ClaudeAgentManager.install writes mcp.json and adds --mcp-config.
    # Composed from `MCP_PUBLIC_ENDPOINT` constant in gamemgr.mcp_server —
    # never hand-write "/mcp" / "/mcp/" literals elsewhere.
    mcp_endpoint: str = ""
    # Arbitrary extra env vars exported into agent.env. Useful escape hatch
    # for future adapters that need provider-specific or runtime knobs without
    # growing this dataclass further.
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentLogs:
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None


@dataclass(frozen=True)
class AgentState:
    status: AgentStatus
    pid: int | None = None
    wrapper_pid: int | None = None
    exit_code: int | None = None


