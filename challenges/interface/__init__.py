"""Abstract challenge interface.

A `Challenge` bundles the artifacts the orchestrator needs to deploy,
score, test, restart, and explain a vulnerable application:

    challenge = (vulbox, functionality_test, flag_handler,
                 restart_handler?, docs)

This package defines ONLY abstract base classes and plain data containers.
It intentionally has no dependency on anything else in this codebase so it
can be shared with challenge authors who write adapters in isolation.

Concrete challenges (notes/, nautilus_ico/, …) live one level up under
`challenges/<name>/` and import from here.
"""

from __future__ import annotations

from .challenge import Challenge, ChallengeDocs, VulboxImage
from .probe import (
    CheckResult,
    FlagHandler,
    FunctionalityTest,
    HealthLevel,
    RestartHandler,
    VulboxProbe,
    VulboxTarget,
)

__all__ = [
    "Challenge",
    "ChallengeDocs",
    "VulboxImage",
    "VulboxProbe",
    "VulboxTarget",
    "FunctionalityTest",
    "FlagHandler",
    "RestartHandler",
    "CheckResult",
    "HealthLevel",
]
