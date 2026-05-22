from __future__ import annotations

import logging

import docker
from docker.errors import APIError, NotFound

from models.topology import Node

log = logging.getLogger(__name__)


class ContainerManager:
    """Manages docker containers for the arena."""

    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._client = client or docker.from_env()

    def create_and_start(self, node: Node) -> str:
        """Create + start a container per Node spec; attach all listed networks.

        Idempotent on name: removes any pre-existing container with the
        same name first.
        """
        self.remove(node.id)

        ports = self._parse_ports(node.ports)
        primary_net = node.networks[0] if node.networks else None

        container = self._client.containers.create(
            image=node.image,
            name=node.id,
            hostname=node.id,
            command=None,
            network=primary_net,
            ports=ports,
            environment=node.env,
            detach=True,
        )
        log.info("created container %s (%s, image=%s)", node.id, container.short_id, node.image)

        for extra_net in node.networks[1:]:
            self._client.networks.get(extra_net).connect(container)
            log.info("connected %s to %s", node.id, extra_net)

        container.start()
        log.info("started container %s", node.id)
        return container.id

    def remove(self, name: str) -> None:
        """Stop + remove a container. Idempotent."""
        try:
            c = self._client.containers.get(name)
        except NotFound:
            return
        try:
            c.stop(timeout=3)
        except APIError:
            pass
        c.remove(force=True)
        log.info("removed container %s", name)

    def exec(self, name: str, cmd: list[str] | str) -> tuple[int, str]:
        """Run a command inside a container; return (exit_code, output)."""
        c = self._client.containers.get(name)
        result = c.exec_run(cmd, demux=False)
        out = result.output.decode(errors="replace") if result.output else ""
        return result.exit_code, out

    def is_running(self, name: str) -> bool:
        try:
            c = self._client.containers.get(name)
        except NotFound:
            return False
        c.reload()
        return c.status == "running"

    @staticmethod
    def _parse_ports(specs: list[str]) -> dict[str, int]:
        """'8001:80' -> {'80/tcp': 8001}."""
        out: dict[str, int] = {}
        for spec in specs:
            host, container = spec.split(":")
            out[f"{int(container)}/tcp"] = int(host)
        return out