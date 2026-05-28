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

    def is_alive(self, container: ContainerHandle, level: "HealthLevel | None" = None) -> bool:
        """Healthcheck at one of four levels (challenges/source.py:HealthLevel).

        Default (level=None or PROTOCOL) is the legacy behavior: a
        socket-connect probe on self.port. Higher levels (CHECKER) and
        lower levels (PROCESS, TCP) are adapter-overridden — the base
        implementation can only tell you "the port answers". The
        backwards-compat default keeps M5/M5.5/M5.6 call sites working
        without passing a level.
        """
        # Lazy import to avoid a circular: challenges/source.py imports
        # this module to subclass Service. Keeping the import inside the
        # method body breaks the cycle.
        from challenges.source import HealthLevel

        if level is None:
            level = HealthLevel.PROTOCOL

        if level == HealthLevel.PROCESS:
            # Default has no specific process to probe — adapters that know
            # their process_pattern should override. Conservative: report
            # True if we can't tell, since this base impl predates the
            # framework's process-level concept.
            return True

        # TCP / PROTOCOL / CHECKER all fall back to the same simple port
        # probe in the base class. Adapters override for richer checks.
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

    def plant_flag(self, container: ContainerHandle, flag: str) -> None:
        """Plant `flag` onto this service's instance in `container`.

        No default — every challenge that participates in the M5 flag chain
        must implement this in a way that fits its own protocol (notes does
        a PUT; an HTTP challenge would POST to a write endpoint; etc.).
        """
        raise NotImplementedError(f"{self.name} does not implement plant_flag")