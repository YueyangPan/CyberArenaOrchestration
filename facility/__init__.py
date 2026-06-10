"""Facility abstraction.

A facility (FlagServer, HealthPoller, …) is a unit of game-server work
that talks to its host (GameMgr) only through three ports — status,
events, exec — bundled in a `FacilityContext`. Today every facility runs
in-process with the `adapters_local` shims; the boundary is defined so a
facility can later run in its own container against HTTP adapters with no
change to facility logic. See `docs/facility_abstraction_plan_v1.md`.
"""

from .base import Facility, FacilityContext
from .adapters_local import LocalEventSink, LocalExecPort, LocalStatusPort
from .ports import EventSink, ExecPort, ExecResult, StatusPort

__all__ = [
    "Facility",
    "FacilityContext",
    "StatusPort",
    "EventSink",
    "ExecPort",
    "ExecResult",
    "LocalStatusPort",
    "LocalEventSink",
    "LocalExecPort",
]
