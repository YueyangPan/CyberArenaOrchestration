"""Loader for the full game.yaml — topology + challenges + round config."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

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


def load(path: str | Path) -> GameConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return _from_dict(raw)


def _from_dict(raw: dict) -> GameConfig:
    teams = [Team(id=t["id"], dev=t["dev"], prod=t["prod"]) for t in raw.get("teams", [])]
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

    return GameConfig(topology=topology, challenges=challenges, rounds=rounds)