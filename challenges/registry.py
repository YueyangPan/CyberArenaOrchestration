"""Challenge registry — name → Challenge instance + per-challenge hooks.

Public entry points:
  build_challenge(name)                  instantiate the Challenge
  known_challenge_names()                sorted list of registered names
  initial_start(challenge, target, exec) post-deploy first-start hook
  build_images(cfg)                      docker build for cfg's challenges
  health_interval_secs(name, default)    per-challenge poll interval

Adding a new challenge: implement it under `challenges/<name>/`, then
register at minimum a factory in `_CHALLENGE_FACTORIES`. Add to
`_IMAGE_BUILDERS` / `_INITIAL_START` / `_HEALTH_INTERVAL_SECS` only
when defaults don't fit.
"""

from __future__ import annotations

import logging
from typing import Callable

from challenges.interface import Challenge, VulboxTarget
from challenges.nautilus_ico import IcoChallenge
from challenges.notes import NotesChallenge

log = logging.getLogger(__name__)


# ── name -> Challenge factory ─────────────────────────────────────────


# Zero-arg factories so imports stay lazy — `IcoChallenge.vulbox` shells
# out to `git rev-parse` inside the vendor checkout, not safe at
# module-import time.
_CHALLENGE_FACTORIES: dict[str, Callable[[], Challenge]] = {
    "notes": NotesChallenge,
    "nautilus-ico": IcoChallenge,
}


def build_challenge(name: str) -> Challenge:
    """Instantiate the registered Challenge. Raises KeyError if unknown."""
    factory = _CHALLENGE_FACTORIES.get(name)
    if factory is None:
        raise KeyError(
            f"unknown challenge: {name!r}; "
            f"known: {sorted(_CHALLENGE_FACTORIES)}"
        )
    return factory()


def known_challenge_names() -> list[str]:
    return sorted(_CHALLENGE_FACTORIES)


# ── per-challenge first-start hook ────────────────────────────────────


ExecFn = Callable[[str, str], tuple[int, str]]
"""(container_name, cmd) -> (rc, stdout). Privileged docker exec."""

InitialStartFn = Callable[[VulboxTarget, ExecFn], None]


def _ico_initial_start(target: VulboxTarget, exec_in: ExecFn) -> None:
    """Kick /arena/restart.sh so the ico binary is running.

    The ico image bakes the binary but doesn't spawn it on container
    boot — restart.sh both spawns (first-start) and recompiles
    (defense rebuilds). Treating first-start as "restart" keeps the
    two code paths identical.
    """
    rc, out = exec_in(target.host, "/arena/restart.sh")
    if rc != 0:
        log.warning(
            "nautilus-ico initial_start: /arena/restart.sh rc=%d out=%r",
            rc, out[-200:] if out else "",
        )


_INITIAL_START: dict[str, InitialStartFn] = {
    # notes has no entry — its image entrypoint spawns the daemon directly.
    "nautilus-ico": _ico_initial_start,
}


def initial_start(challenge: Challenge, target: VulboxTarget, exec_in: ExecFn) -> None:
    """Run the per-challenge first-start hook (no-op if unregistered)."""
    hook = _INITIAL_START.get(challenge.name)
    if hook is None:
        return
    hook(target, exec_in)


# ── per-challenge image build ─────────────────────────────────────────


ImageBuilderFn = Callable[[], str]


def _ico_build() -> str:
    # Lazy import so notes-only paths don't load the docker SDK.
    from challenges.nautilus_ico.build import build_image
    return build_image()


def _notes_build() -> str:
    from challenges.notes.build import build_image
    return build_image()


_IMAGE_BUILDERS: dict[str, ImageBuilderFn] = {
    "notes":         _notes_build,
    "nautilus-ico":  _ico_build,
}


def build_images(cfg) -> list[str]:
    """Build (or cache-hit) every image referenced by cfg.challenges."""
    tags: list[str] = []
    for chcfg in cfg.challenges:
        builder = _IMAGE_BUILDERS.get(chcfg.name)
        if builder is None:
            continue
        tags.append(builder())
    return tags


# ── per-challenge health-poll interval ────────────────────────────────


_HEALTH_INTERVAL_SECS: dict[str, float] = {
    # ico's CHECKER talks to the live binary each tick; 30s balances
    # signal vs. load. notes is attack-only so it isn't polled.
    "nautilus-ico": 30.0,
}


def health_interval_secs(name: str, default: float) -> float:
    """Per-challenge poll interval; `default` when unregistered."""
    return _HEALTH_INTERVAL_SECS.get(name, default)
