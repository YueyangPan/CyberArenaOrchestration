"""Per-phase CLI invocation — the 3-line dance, factored out.

Composition of two abstractions:
  * `AgentManager` (CLI-flavor recipe: where to drop the prompt, how
    to invoke the CLI, what process pattern to pkill on timeout).
  * `AgentTransport` (host execution: write a file, run a command,
    kill a process).

Callers (main.py's kickoff handler, tests) get one async function that
stages the prompt, runs the CLI through the transport with a phase
timeout, and falls back to pkill if SIGTERM didn't propagate. Returns
the raw `CommandResult` — persistence (stream-{phase}.jsonl etc.) and
event emission (AGENT_COMPLETED / TIMEOUT / FAILED) are the caller's
business (GameMgr.record_phase_done handles both).

This module is intentionally tiny: it's just the glue, not an
abstraction. Splitting it out keeps `AgentManager` data-only and
prevents the "method that does five different things" smell that
`run_phase` had grown into.
"""

from __future__ import annotations

import secrets

from .manager import AgentManager
from .transport import AgentHandle, AgentTransport, CommandResult


async def run_phase_via_transport(
    mgr: AgentManager,
    transport: AgentTransport,
    handle: AgentHandle,
    prompt: str,
    *,
    timeout: float,
    model: str | None = None,
) -> CommandResult:
    """Run one phase. Stages prompt, awaits CLI invocation with timeout,
    issues a precision SIGKILL on the timed-out invocation's process
    group. Returns the raw result.

    The per-invocation `session_marker` is what makes the on-timeout
    kill non-racy. `phase_command` runs claude under `setsid -w` and
    drops the new session leader's PID into
    `/tmp/cyberarena_phase_<marker>.pid`. On timeout the runner calls
    `transport.kill_session(handle, marker)` which signals only the
    PGID from that pidfile — so a sibling claude that the next phase
    just started in the same container is unaffected.

    Caller responsibilities:
      - Persist `result.stdout` / `result.stderr` to artifacts
      - Emit AGENT_PROMPT_SENT (before this call) +
        AGENT_COMPLETED / AGENT_TIMEOUT / AGENT_FAILED (after, based
        on `result.timed_out` and `result.exit_code`)

    See `GameMgr.record_phase_done` for the canonical caller-side
    flow that already does both.
    """
    transport.write_file(handle, mgr.PROMPT_PATH, prompt.encode())
    session_marker = secrets.token_hex(6)
    result = await transport.run_command(
        handle,
        mgr.phase_command(model=model, session_marker=session_marker),
        timeout=timeout,
    )
    if result.timed_out:
        # SIGTERM via proc.terminate() on the docker exec wrapper
        # doesn't always propagate to the in-container child; signal
        # the whole process group via the pidfile marker as the safety
        # net. Only THIS phase's claude (+ its subprocesses) dies.
        transport.kill_session(handle, session_marker)
    return result
