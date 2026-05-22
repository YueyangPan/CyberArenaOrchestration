"""Cyber Arena orchestrator CLI."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from orchestrator import topology
from orchestrator.orchestrator import Orchestrator

DEFAULT_CONFIG = Path(__file__).parent / "config" / "game.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cyberarena")
    parser.add_argument("command", choices=["setup", "teardown"])
    parser.add_argument("--config", "-c", default=str(DEFAULT_CONFIG))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    graph = topology.load(args.config)
    orch = Orchestrator(graph=graph)

    if args.command == "setup":
        orch.setup()
    else:
        orch.teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())