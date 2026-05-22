"""Generic Service ABC for anything deployed onto a prod container.

A Service is defined independently of any node — every lifecycle method
receives a ContainerHandle so a single Service instance can be deployed
onto multiple containers (same vulnerable app on every team prod).

M1 defines the interface only; concrete services land in M2 (porting the
notes service from the reference repo's challenge_simple).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ContainerHandle:
    """Opaque handle to a running container passed to Service methods."""

    container_id: str
    name: str
    networks: list[str]


class Service(ABC):
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

    @abstractmethod
    def is_alive(self, container: ContainerHandle) -> bool: ...