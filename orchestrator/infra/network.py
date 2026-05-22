from __future__ import annotations

import logging

import docker
from docker.errors import NotFound

from models.topology import Network, TopologyGraph

log = logging.getLogger(__name__)

GAME_NET = "game_net"


class NetworkManager:
    """Manages docker networks for the arena.

    Owns the cross-team network switch (game_net attach/detach for service
    containers). Backend is docker network attach/detach for MVP; an
    iptables backend (Linux optimization, no reconnect interruption) is a
    future extension that shares this interface.
    """

    def __init__(
        self,
        client: docker.DockerClient | None = None,
        graph: TopologyGraph | None = None,
    ) -> None:
        self._client = client or docker.from_env()
        # Service containers = nodes attached to game_net (prod containers).
        # Dev containers are intentionally excluded so they never get touched
        # by phase switching — see m3_network_plan.md.
        if graph is not None:
            self._service_containers = [n.id for n in graph.nodes if GAME_NET in n.networks]
        else:
            self._service_containers = []

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
        """Open or close cross-team game_net for all registered service containers.

        DEFENSE (allow=False) -> disconnect each service container from game_net.
        ATTACK  (allow=True)  -> connect each service container back to game_net.

        Idempotent: skips containers that are already in the target state. May
        reset existing TCP connections — see m3_network_plan.md.
        """
        try:
            net = self._client.networks.get(GAME_NET)
        except NotFound:
            log.warning("set_cross_team_network: %s not found", GAME_NET)
            return

        for name in self._service_containers:
            try:
                container = self._client.containers.get(name)
            except NotFound:
                log.warning("set_cross_team_network: container %s not found; skip", name)
                continue
            container.reload()
            attached = GAME_NET in container.attrs["NetworkSettings"]["Networks"]
            if allow and not attached:
                net.connect(container)
                log.info("connected %s to %s", name, GAME_NET)
            elif not allow and attached:
                net.disconnect(container)
                log.info("disconnected %s from %s", name, GAME_NET)