"""Challenge registry — name → Challenge instance + per-challenge lifecycle hooks.

Public entry points:

  build_challenge(name)                 — instantiate the Challenge for `name`
  known_challenge_names()               — sorted list of registered names
  initial_start(challenge, target, exec) — first-time start hook (deploy step)
  build_images(cfg)                     — build any image-bearing challenges
                                          referenced by the game config
  health_interval_secs(name, default)   — per-challenge poll interval lookup

Adding a new challenge:
  1. Implement it under `challenges/<name>/` (vulbox + functionality_test +
     flag_handler + restart_handler? + docs).
  2. Register the name → factory in `_CHALLENGE_FACTORIES`.
  3. If the challenge needs first-time setup that doesn't fit "restart"
     (e.g. ships a script + spawns it like notes), register a callable
     in `_INITIAL_START`. Defaults to a no-op for image-baked challenges
     whose Docker entrypoint handles startup.
  4. If the challenge needs a docker image build (rather than a pre-baked
     registry pull), register a builder in `_IMAGE_BUILDERS`.
  5. If the default health-poll interval (`DEFAULT_INTERVAL_SECS`)
     doesn't fit, register an override in `_HEALTH_INTERVAL_SECS`.
"""

from __future__ import annotations

import logging
from typing import Callable

from challenges.interface import Challenge, VulboxTarget
from challenges.nautilus_ico import IcoChallenge
from challenges.notes import NotesChallenge
from challenges.notes._server_script import SERVER_SCRIPT_PATH, SERVER_SOURCE, REMOTE_DIR

log = logging.getLogger(__name__)


# ── name -> Challenge factory ─────────────────────────────────────────


# Zero-arg factories so imports stay lazy (`IcoChallenge.vulbox` shells out
# to git rev-parse inside the vendor checkout — we don't want that at
# module-import time).
_CHALLENGE_FACTORIES: dict[str, Callable[[], Challenge]] = {
    "notes": NotesChallenge,
    "nautilus-ico": IcoChallenge,
}


def build_challenge(name: str) -> Challenge:
    """Instantiate the Challenge registered under `name`. Raises KeyError
    when the name isn't registered."""
    factory = _CHALLENGE_FACTORIES.get(name)
    if factory is None:
        raise KeyError(
            f"unknown challenge: {name!r}; "
            f"known: {sorted(_CHALLENGE_FACTORIES)}"
        )
    return factory()


def known_challenge_names() -> list[str]:
    """Stable sorted list of registered challenge names."""
    return sorted(_CHALLENGE_FACTORIES)


# ── per-challenge first-time start hook ───────────────────────────────


ExecFn = Callable[[str, str], tuple[int, str]]
"""(container_name, cmd) -> (rc, stdout). Privileged docker exec inside
the team's prod container; orchestration provides this."""

InitialStartFn = Callable[[VulboxTarget, ExecFn], None]
"""(target, exec_in_container) -> None. Raise on hard failure."""


def _notes_initial_start(target: VulboxTarget, exec_in: ExecFn) -> None:
    """Ship the notes daemon source into the container and spawn it.

    Notes runs in the generic prod-base image (no per-challenge baked
    image — yet); the script is delivered at deploy time and detached
    via nohup. Idempotent: a re-deploy overwrites the script and the
    server flag-rotation path (PUT a new flag) doesn't care about the
    in-memory store carrying over.
    """
    import base64

    port = target.ports["service"]
    # base64-encode the script source so the shell command stays a single
    # line (no embedded newlines / quotes to escape). The container has
    # base64(1) from coreutils via the prod-base image.
    b64 = base64.b64encode(SERVER_SOURCE.encode()).decode()
    deliver = (
        f"mkdir -p {REMOTE_DIR} && "
        f"echo {b64} | base64 -d > {SERVER_SCRIPT_PATH} && "
        f"chmod 755 {SERVER_SCRIPT_PATH}"
    )
    rc, out = exec_in(target.host, deliver)
    if rc != 0:
        raise RuntimeError(
            f"notes initial_start: script delivery failed on {target.host}: "
            f"rc={rc}, out={out[-200:]!r}"
        )
    exec_in(
        target.host,
        f"nohup python3 {SERVER_SCRIPT_PATH} {port} >/tmp/notes.log 2>&1 &",
    )
    # exec_run with `&` returns immediately; the rc here just reflects
    # whether the shell parsed the command. The actual readiness is
    # checked by the functionality_test health probe.


def _ico_initial_start(target: VulboxTarget, exec_in: ExecFn) -> None:
    """Kick the framework restart script so the ico binary is running.

    The ico image's entrypoint compiles ico.pas at first boot but
    doesn't spawn the binary; /arena/restart.sh does (recompile + run).
    Treating this as 'initial start' keeps deploy time short — we don't
    rebuild from scratch unless the binary is missing."""
    rc, out = exec_in(target.host, "/arena/restart.sh")
    if rc != 0:
        log.warning(
            "nautilus-ico initial_start: /arena/restart.sh rc=%d out=%r",
            rc, out[-200:] if out else "",
        )


_INITIAL_START: dict[str, InitialStartFn] = {
    "notes": _notes_initial_start,
    "nautilus-ico": _ico_initial_start,
}


def initial_start(challenge: Challenge, target: VulboxTarget, exec_in: ExecFn) -> None:
    """Run the per-challenge first-time start hook. No-op if the
    challenge didn't register one (image-baked entrypoint handles it)."""
    hook = _INITIAL_START.get(challenge.name)
    if hook is None:
        return
    hook(target, exec_in)


# ── per-challenge image build ─────────────────────────────────────────


ImageBuilderFn = Callable[[], str]
"""Returns the resolved image tag. Idempotent (cache-aware)."""


def _ico_build() -> str:
    # Lazy import — keep the docker SDK out of notes-only paths.
    from challenges.nautilus_ico.build import build_image
    return build_image()


_IMAGE_BUILDERS: dict[str, ImageBuilderFn] = {
    "nautilus-ico": _ico_build,
    # `notes` reuses the generic `cyberarena/prod-base` image (which the
    # orchestrator builds from topology), so no entry here.
}


# ── per-challenge health-poll interval ────────────────────────────────


_HEALTH_INTERVAL_SECS: dict[str, float] = {
    # ico's CHECKER probe talks to the live binary over TCP each tick;
    # 30s balances signal vs. load. notes is attack-only so it isn't
    # polled; an interval here would be ignored.
    "nautilus-ico": 30.0,
}


def health_interval_secs(name: str, default: float) -> float:
    """Return the per-challenge poll interval, falling back to `default`
    (typically `health.poller.DEFAULT_INTERVAL_SECS`) when no override
    is registered."""
    return _HEALTH_INTERVAL_SECS.get(name, default)


def build_images(cfg) -> list[str]:
    """Build (or cache-hit) every challenge image referenced by `cfg`.

    Iterates `cfg.challenges`, looks up each name's builder, and invokes
    it. Returns the list of resolved image tags. Idempotent: each
    builder skips when the tag is already present locally.
    """
    tags: list[str] = []
    for chcfg in cfg.challenges:
        builder = _IMAGE_BUILDERS.get(chcfg.name)
        if builder is None:
            continue
        tags.append(builder())
    return tags
