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
    issues belt-and-suspenders pkill on timeout. Returns the raw result.

    Caller responsibilities:
      - Persist `result.stdout` / `result.stderr` to artifacts
      - Emit AGENT_PROMPT_SENT (before this call) +
        AGENT_COMPLETED / AGENT_TIMEOUT / AGENT_FAILED (after, based
        on `result.timed_out` and `result.exit_code`)

    See `GameMgr.record_phase_done` for the canonical caller-side
    flow that already does both.
    """
    transport.write_file(handle, mgr.PROMPT_PATH, prompt.encode())
    result = await transport.run_command(
        handle,
        mgr.phase_command(model=model),
        timeout=timeout,
    )
    if result.timed_out:
        # SIGTERM via proc.terminate() doesn't always propagate through
        # `docker exec` to the in-container CLI; pkill makes sure.
        transport.kill_process(handle, mgr.PHASE_KILL_PATTERN)
    return result
