from __future__ import annotations

import logging
import shlex

from models.topology import Node, TopologyGraph

from .infra.docker import ContainerManager
from .infra.network import NetworkManager

log = logging.getLogger(__name__)


class Orchestrator:
    """Container + network lifecycle for the arena.

    setup() order:
      1. create docker networks
      2. create + start containers, attach networks
      3. run install_list inside each container (apt / pip)
      4. pre-flight check (stub per design section 2)

    teardown() reverses.
    """

    def __init__(
        self,
        graph: TopologyGraph,
        containers: ContainerManager | None = None,
        network: NetworkManager | None = None,
    ) -> None:
        self.graph = graph
        self.containers = containers or ContainerManager()
        self.network = network or NetworkManager()

    def setup(self) -> None:
        log.info("orchestrator setup: %d networks, %d nodes",
                 len(self.graph.networks), len(self.graph.nodes))

        for net in self.graph.networks:
            self.network.create(net)

        for node in self.graph.nodes:
            self.containers.create_and_start(node)

        for node in self.graph.nodes:
            self._run_install_list(node)

        self._preflight()
        log.info("orchestrator setup complete")

    def teardown(self) -> None:
        log.info("orchestrator teardown")

        for node in self.graph.nodes:
            self.containers.remove(node.id)

        for net in self.graph.networks:
            self.network.remove(net.name)

        log.info("orchestrator teardown complete")

    def _run_install_list(self, node: Node) -> None:
        if not node.install_list:
            return
        pkgs = " ".join(shlex.quote(p) for p in node.install_list)
        cmd = f"bash -c 'apt-get update && apt-get install -y --no-install-recommends {pkgs}'"
        log.info("install_list on %s: %s", node.id, node.install_list)
        rc, out = self.containers.exec(node.id, cmd)
        if rc != 0:
            raise RuntimeError(f"install_list failed on {node.id} (rc={rc}):\n{out}")

    def _preflight(self) -> None:
        # MVP stub per design section 2.
        log.info("pre-flight: starting checks")
        log.info("pre-flight: all checks passed")