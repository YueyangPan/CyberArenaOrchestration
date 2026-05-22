from __future__ import annotations

import logging

import docker
from docker.errors import NotFound

from models.topology import Network

log = logging.getLogger(__name__)


class NetworkManager:
    """Manages docker networks for the arena.

    Exposes a backend-agnostic interface (set_cross_team_network) that
    GameMgr calls in later milestones — implementation deferred to M3.
    """

    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._client = client or docker.from_env()

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
        """Open/close the cross-team game_net.

        M3 implementation: docker network connect/disconnect each ServiceContainer
        to/from game_net. Defense=False (disconnect), Attack=True (reconnect).
        """
        raise NotImplementedError("set_cross_team_network: implemented in M3")