"""Generic Service ABC for anything deployed onto a container.

A Service is defined independently of any node — every lifecycle method
receives a ContainerHandle so a single Service instance can be deployed
onto multiple containers (same vulnerable app on every team prod).

is_alive has a generic default (port-level socket connect inside the
container). Adapters override it when a protocol-level healthcheck is
warranted (e.g., notes: LIST -> COUNT; HTTP: GET /health).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import docker


@dataclass(frozen=True)
class ContainerHandle:
    """Opaque handle to a running container passed to Service methods."""

    container_id: str
    name: str
    networks: list[str]


class Service(ABC):
    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._client = client or docker.from_env()

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def port(self) -> int | None:
        return None

    @property
    def protocol(self) -> str | None:
        return None

    @abstractmethod
    def install(self, container: ContainerHandle) -> None: ...

    @abstractmethod
    def start(self, container: ContainerHandle) -> None: ...

    @abstractmethod
    def stop(self, container: ContainerHandle) -> None: ...

    def is_alive(self, container: ContainerHandle) -> bool:
        """Default healthcheck: docker exec a Python socket connect to self.port.

        Override in adapters for protocol-level checks.
        """
        if self.port is None:
            return True
        probe = (
            f"import socket,sys; s=socket.socket(); s.settimeout(2); "
            f"s.connect(('127.0.0.1', {self.port})); s.close()"
        )
        result = self._client.containers.get(container.name).exec_run(
            ["python3", "-c", probe]
        )
        return result.exit_code == 0