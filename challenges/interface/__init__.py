"""Abstract challenge interface.

Defines `Challenge` + supporting types. No imports from the rest of the
codebase — challenge authors can implement against this package alone.
Concrete challenges live under `challenges/<name>/`.
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
