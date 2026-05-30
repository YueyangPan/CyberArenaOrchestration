from __future__ import annotations

import logging

import docker
from docker.errors import NotFound

from models.topology import Network, TopologyGraph

log = logging.getLogger(__name__)

GAME_NET = "game_net"
# Nets that are toggled per phase (ATTACK opens / DEFENSE closes). Any
# network whose name starts with one of these prefixes is treated as
# cross-team. game_net carries prod ↔ prod traffic; attack_net_<team>
# carries that team's dev → opposing prod traffic (no dev ↔ dev so
# dev containers stay isolated from each other).
_CROSS_TEAM_NET_PREFIXES = ("game_net", "attack_net_")


def _is_cross_team(net_name: str) -> bool:
    return any(net_name == p or net_name.startswith(p) for p in _CROSS_TEAM_NET_PREFIXES)


class NetworkManager:
    """Manages docker networks for the arena.

    Owns the cross-team network switch (attach/detach for every
    cross-team net listed below) per phase. Backend is docker network
    attach/detach for MVP; an iptables backend (Linux optimization, no
    reconnect interruption) is a future extension sharing this interface.
    """

    def __init__(
        self,
        client: docker.DockerClient | None = None,
        graph: TopologyGraph | None = None,
    ) -> None:
        self._client = client or docker.from_env()
        # Per-network membership: cross_team net name → list of node ids
        # that should be attached when that net is open. Built from the
        # static topology so phase switching is purely a state-restore
        # operation against a known target set.
        self._cross_team_membership: dict[str, list[str]] = {}
        if graph is not None:
            for node in graph.nodes:
                for net_name in node.networks:
                    if _is_cross_team(net_name):
                        self._cross_team_membership.setdefault(net_name, []).append(node.id)

    def create(self, network: Network) -> str:
        """Create a docker network. Idempotent — returns existing id if present."""
        try:
            existing = self._client.networks.get(network.name)
            log.info("network %s already exists (%s)", network.name, existing.short_id)
            return existing.id
        except NotFound:
            pass

        ipam_pool = docker.types.IPAMPool(subnet=network.subnet)
        ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])
        created = self._client.networks.create(
            name=network.name,
            driver="bridge",
            ipam=ipam_config,
            check_duplicate=True,
        )
        log.info("created network %s (%s, %s)", network.name, created.short_id, network.subnet)
        return created.id

    def remove(self, name: str) -> None:
        """Remove a docker network. Idempotent."""
        try:
            net = self._client.networks.get(name)
        except NotFound:
            log.info("network %s already gone", name)
            return
        net.remove()
        log.info("removed network %s", name)

    def set_cross_team_network(self, allow: bool) -> None:
        """Open or close every cross-team network for the registered nodes.

        DEFENSE (allow=False): disconnect every (container, cross_team_net)
        pair so the agent can't reach anything off-team while patching.
        ATTACK  (allow=True):  reconnect each one to its declared nets.

        Idempotent: skips containers already in the target state. May
        reset existing TCP connections — see m3_network_plan.md.

        Iterates `_cross_team_membership` so this works for both the
        original `game_net` (prod↔prod) and any number of per-team
        `attack_net_<team>` networks (each team's dev → opposing prod).
        """
        for net_name, members in self._cross_team_membership.items():
            try:
                net = self._client.networks.get(net_name)
            except NotFound:
                log.warning("set_cross_team_network: %s not found", net_name)
                continue
            for name in members:
                try:
                    container = self._client.containers.get(name)
                except NotFound:
                    log.warning("set_cross_team_network: container %s not found; skip", name)
                    continue
                container.reload()
                attached = net_name in container.attrs["NetworkSettings"]["Networks"]
                if allow and not attached:
                    net.connect(container)
                    log.info("connected %s to %s", name, net_name)
                elif not allow and attached:
                    net.disconnect(container)
                    log.info("disconnected %s from %s", name, net_name)