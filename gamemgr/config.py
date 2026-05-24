"""Loader for the full game.yaml — topology + challenges + round config + agents.

Also resolves `${VAR}` placeholders against the process environment (after
loading .env) so secrets never live in the committed yaml.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from models.agent import AgentConfig
from models.topology import Network, Node, Team, TopologyGraph


@dataclass(frozen=True)
class ChallengeConfig:
    name: str
    type: str
    port: int
    deploy_on: list[str]    # team ids


@dataclass(frozen=True)
class RoundConfig:
    defense_secs: int = 30
    attack_secs: int = 30
    total_rounds: int = 3


@dataclass(frozen=True)
class GameConfig:
    topology: TopologyGraph
    challenges: list[ChallengeConfig] = field(default_factory=list)
    rounds: RoundConfig = field(default_factory=RoundConfig)
    agents: dict[str, AgentConfig] = field(default_factory=dict)  # team_id -> AgentConfig


_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_MAX_EXPAND_DEPTH = 8


def load(path: str | Path, *, env_file: str | Path | None = ".env") -> GameConfig:
    if env_file is not None and Path(env_file).is_file():
        load_dotenv(env_file, override=False)
    with open(path) as f:
        raw = yaml.safe_load(f)
    raw = _expand_env_vars(raw)
    return _from_dict(raw)


def _expand_env_vars(node):
    """Walk a parsed-yaml tree and substitute ${VAR} on every string value.

    Recursion is over the *tree structure* (dict/list/str), not over the
    substituted text. To still support env values that themselves contain
    `${...}` we re-run the substitution on each string in a bounded loop
    with a visited set to detect cycles.
    """
    if isinstance(node, dict):
        return {k: _expand_env_vars(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_env_vars(v) for v in node]
    if isinstance(node, str):
        return _expand_str(node)
    return node


def _expand_str(s: str) -> str:
    visited: set[str] = set()
    for _ in range(_MAX_EXPAND_DEPTH):
        matches = _VAR_RE.findall(s)
        if not matches:
            return s
        for var in matches:
            if var in visited:
                raise ValueError(
                    f"cyclic env var reference involving ${{{var}}} while expanding {s!r}"
                )
            if var not in os.environ:
                raise ValueError(f"env var {var} referenced in game.yaml but not set")
        visited.update(matches)
        s = _VAR_RE.sub(lambda m: os.environ[m.group(1)], s)
    raise ValueError(f"env var expansion exceeded depth {_MAX_EXPAND_DEPTH} on {s!r}")


def _from_dict(raw: dict) -> GameConfig:
    teams_raw = raw.get("teams", [])
    teams = [Team(id=t["id"], dev=t["dev"], prod=t["prod"]) for t in teams_raw]
    nodes = [
        Node(
            id=n["id"],
            image=n["image"],
            networks=list(n.get("networks", [])),
            ports=list(n.get("ports", [])),
            install_list=list(n.get("install_list", [])),
            env=dict(n.get("env", {})),
        )
        for n in raw.get("nodes", [])
    ]
    networks = [Network(name=name, subnet=cfg["subnet"]) for name, cfg in raw.get("networks", {}).items()]
    topology = TopologyGraph(teams=teams, nodes=nodes, networks=networks)

    challenges = [
        ChallengeConfig(
            name=c["name"],
            type=c["type"],
            port=int(c["port"]),
            deploy_on=list(c["deploy_on"]),
        )
        for c in raw.get("challenges", [])
    ]

    rc = raw.get("round_config", {})
    rounds = RoundConfig(
        defense_secs=int(rc.get("defense_secs", 30)),
        attack_secs=int(rc.get("attack_secs", 30)),
        total_rounds=int(rc.get("total_rounds", 3)),
    )

    agents: dict[str, AgentConfig] = {}
    for t in teams_raw:
        ag = t.get("agent")
        if ag is None:
            continue
        agents[t["id"]] = AgentConfig(
            cli=ag["cli"],
            model=ag.get("model", ""),
            api_key=ag.get("api_key", ""),
            base_url=ag.get("base_url", ""),
        )

    return GameConfig(topology=topology, challenges=challenges, rounds=rounds, agents=agents)
