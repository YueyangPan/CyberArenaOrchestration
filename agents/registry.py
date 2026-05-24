"""Agent registry — picks an AgentManager subclass by cli flavor.

The flavor is the first whitespace-separated token of cfg.cli, e.g.

  cli: claude --dangerously-skip-permissions     -> "claude"
  cli: sleep 60                                  -> "sleep"   (test override)
  cli: codex --some-flag                         -> "codex"

The 'sleep' override and any other non-LLM stand-ins reuse ClaudeAgentManager
(same supervisor-bash mechanics, just skips the npm install step).
"""

from __future__ import annotations

from models.agent import AgentConfig

from .claude import ClaudeAgentManager
from .manager import AgentManager

REGISTRY: dict[str, type[AgentManager]] = {
    "claude": ClaudeAgentManager,
}

DEFAULT: type[AgentManager] = ClaudeAgentManager


def build(cfg: AgentConfig) -> AgentManager:
    flavor = cfg.cli.split(None, 1)[0] if cfg.cli else ""
    cls = REGISTRY.get(flavor, DEFAULT)
    return cls()
