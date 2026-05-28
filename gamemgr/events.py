"""Event Bus — async pub-sub with in-memory history and a JSONL file sink."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


class EventType(str, Enum):
    ROUND_START = "ROUND_START"
    PHASE_CHANGE = "PHASE_CHANGE"
    ROUND_STOP = "ROUND_STOP"
    SERVICE_DEPLOYED = "SERVICE_DEPLOYED"
    # M4.5 lifecycle chain: host-emitted INSTALLED (prep done, not launched)
    # → host-emitted LAUNCHED (supervisor pid captured) → MCP-emitted READY
    # (LLM proved the channel works). Only the first two are load-bearing.
    AGENT_INSTALLED = "AGENT_INSTALLED"
    AGENT_LAUNCHED = "AGENT_LAUNCHED"
    AGENT_READY = "AGENT_READY"
    FLAG_PLANTED = "FLAG_PLANTED"
    FLAG_SUBMITTED = "FLAG_SUBMITTED"
    # Agent-self-reported deploy completion (M4.5; agent → MCP).
    # Distinct from SERVICE_DEPLOYED which is the host-side initial install.
    DEPLOY_COMPLETE = "DEPLOY_COMPLETE"


@dataclass(frozen=True)
class Event:
    id: str
    ts: float
    type: str
    team_id: str | None
    round_id: int
    phase: str
    payload: dict = field(default_factory=dict)


def new_event(
    type_: str | EventType,
    *,
    team_id: str | None = None,
    round_id: int = 0,
    phase: str = "",
    payload: dict | None = None,
) -> Event:
    return Event(
        id=uuid.uuid4().hex[:12],
        ts=time.time(),
        type=str(type_.value if isinstance(type_, EventType) else type_),
        team_id=team_id,
        round_id=round_id,
        phase=phase,
        payload=payload or {},
    )


Subscriber = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._history: list[Event] = []

    def subscribe(self, callback: Subscriber) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Subscriber) -> None:
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    async def publish(self, event: Event) -> None:
        self._history.append(event)
        for cb in list(self._subscribers):
            try:
                await cb(event)
            except Exception:
                log.exception("event subscriber failed; dropping")

    def history(self) -> list[Event]:
        return list(self._history)


class FileSink:
    """Subscriber that appends every event as one JSON line to a file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "a", buffering=1)  # line-buffered

    async def __call__(self, event: Event) -> None:
        self._f.write(json.dumps(asdict(event)) + "\n")

    def close(self) -> None:
        try:
            self._f.flush()
            self._f.close()
        except Exception:
            pass


def new_run_dir(base: Path | str = "runs") -> Path:
    """Create runs/<utc-timestamp>/ and return the path."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    p = Path(base) / stamp
    p.mkdir(parents=True, exist_ok=True)
    return p