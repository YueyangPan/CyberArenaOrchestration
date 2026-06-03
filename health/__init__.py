"""Health-check facility.

Two pieces:
  * `HealthChecker` — pure 4-level probe primitive (PROCESS → TCP →
    PROTOCOL → CHECKER). Stateless; depends only on the resolvers
    injected at construction.
  * `HealthPoller` — periodic scheduler. Driven by GameMgr at phase
    boundaries; does not subscribe to anything. Exposes `trigger_now`
    for the restart-post one-shot probe.

The package is independent of `gamemgr` internals — it consumes
ChallengeSpec via the existing source ABC and reports outcomes to a
`Timeline` instance. Pluggable per `Service` ABC (no demo-service
specifics hardcoded).
"""

from .checker import HealthChecker
from .poller import HealthPoller, PollTarget

__all__ = ["HealthChecker", "HealthPoller", "PollTarget"]
