"""Agent registry — picks an AgentManager subclass by provider.

Primary key is `cfg.provider` (from game.yaml's `agent.provider:`).
For back-compat with yamls that don't set it, falls back to the first
whitespace-separated token of `cfg.cli`:

  provider: claude                                 -> ClaudeAgentManager
  (provider unset, cli: "claude --print")          -> ClaudeAgentManager
  (provider unset, cli: "sleep 60")                -> ClaudeAgentManager (test stand-in)
  provider: codex                                  -> CodexAgentManager (NotImplementedError until added)

Future CLIs (codex / gemini / openai_compat) register themselves here
by adding a single line to REGISTRY.
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
    """Look up the right AgentManager class for `cfg`.

    Resolution order:
      1. `cfg.provider` (the canonical knob)
      2. first token of `cfg.cli` (back-compat; matches the M4-era yaml)
      3. DEFAULT (ClaudeAgentManager) — so stand-ins like `cli: sleep 60`
         keep working with no provider set
    """
    key = (cfg.provider or "").strip()
    if not key and cfg.cli:
        key = cfg.cli.split(None, 1)[0]
    cls = REGISTRY.get(key, DEFAULT)
    return cls()
