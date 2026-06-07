"""Agent registry — pick an AgentManager subclass by provider.

Resolution order:
  1. `cfg.provider` (canonical)
  2. first whitespace token of `cfg.cli` (back-compat for yamls
     without a provider field)
  3. DEFAULT (ClaudeAgentManager) so test stand-ins like `cli: sleep 60`
     keep working

Future CLIs register by adding one line to REGISTRY.
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
    """Look up the AgentManager class for `cfg` (see module docstring
    for resolution order)."""
    key = (cfg.provider or "").strip()
    if not key and cfg.cli:
        key = cfg.cli.split(None, 1)[0]
    cls = REGISTRY.get(key, DEFAULT)
    return cls()
