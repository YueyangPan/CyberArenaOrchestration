# Persistent-Memory Agent — Plan v1

## What we're building

Two components per team:

- **`ClaudeAgentManager`** *(host side, exists)* — controls agent
  lifecycle from outside the container. Drives the runner via file
  IPC over `docker exec`.
- **`ClaudeAgentRunner`** *(in-container, new)* — Python daemon that
  lives the whole game. Watches `/tmp/agent/cue`, runs `claude -p
  --continue` per cue, writes `done.json` on exit, loops.

Memory carries across phases via Claude CLI's `--continue` (it picks
up the most-recent session in the runner's cwd — empirically
verified). Defense and attack share one conversation.

**No tmux. No PTY. No screen scraping.** Turn completion = process
exit.

## Non-goals

- Single long-lived Claude process. Each phase is a fresh `claude -p
  --continue` invocation; the runner is the persistent process, not
  Claude itself.
- One stable PID for "the agent". The **runner's PID** is stable;
  Claude's PID changes each phase. Both are tracked.
- Async mode (deferred — see "Async mode" parking lot below).

---

## Architecture

```
Host orchestrator (ClaudeAgentManager)
    │  file IPC via docker exec
    ▼
team{N}_dev container
    /tmp/agent/runner.py        ← long-lived Python daemon (PID stable)
        │
        │ for each phase:
        ▼
    cd /tmp/agent/workdir
    claude -p --continue --mcp-config mcp.json --model … < prompt.txt
        │
        │ writes session to /root/.claude/projects/<workdir-cwd>/<id>.jsonl
        ▼
    exits → runner writes done.round{N}.{phase}.json → loop
```

Per round:

```
R1 DEFENSE  →  manager drops prompt+cue → runner runs claude → done →
R1 ATTACK   →  manager drops prompt+cue → runner runs claude → done →
R2 DEFENSE  →  …
```

Each `claude` invocation is a new process but resumes the prior
conversation.

---

## Lifecycle

### 1. Game start — `install()` runs ONCE per team

| Step | Action |
|---|---|
| 1 | `npm install -g @anthropic-ai/claude-code` |
| 2 | `_provision_ssh_pivot` (existing M5.6 SSH key push) |
| 3 | Write `/tmp/agent/{mcp.json, agent.env, runner.py}` (runner.py is copied from `agents/runner.py` in the repo) |
| 4 | `mkdir -p /tmp/agent/workdir` (claude's isolated cwd — `--continue` resumes only this arena's sessions) |
| 5 | `nohup setsid python3 /tmp/agent/runner.py </dev/null >runner.stdout 2>runner.stderr &` |
| 6 | Wait for `/tmp/agent/runner.pid` to appear; record it |

### 2. Each phase — manager does two things

```python
mgr.send_prompt(handle, prompt_text, round_id=N, phase="DEFENSE")
# → emits AGENT_PROMPT_SENT
result = await mgr.wait_for_done(handle, round_id=N, phase="DEFENSE",
                                  timeout=phase_secs)
# → emits AGENT_COMPLETED on success
# → emits AGENT_TIMEOUT  if timer fires first (manager also drops `kill`)
# → emits AGENT_RUNNER_DEAD if pre-send liveness check failed
```

`send_prompt` steps:

1. **Liveness check**: `kill -0 $(cat runner.pid)`. If dead → emit
   `AGENT_RUNNER_DEAD`, return without dropping cue.
2. Write `/tmp/agent/prompt.txt` (the prompt body).
3. Write `/tmp/agent/phase_context.json` = `{round_id, phase}`.
4. Touch `/tmp/agent/cue`.

`wait_for_done` polls for `done.round{N}.{phase}.json`. On timeout:
drop `kill`, wait a short grace window, surface the resulting
forced-exit done.

### 3. Game end

Drop `/tmp/agent/shutdown` → runner exits cleanly.
Then `kill -TERM <runner_pid>` as belt-and-suspenders.

---

## `ClaudeAgentRunner` (`agents/runner.py` → copied to `/tmp/agent/runner.py`)

Class-based so each helper is independently unit-testable. The
orchestrator never imports it; `install()` copies the file into the
container and starts it with `python3 runner.py`.

```python
#!/usr/bin/env python3
"""CyberArena persistent agent runner. Lives in the team's dev
container. Watches /tmp/agent/cue, runs `claude -p --continue` per
cue, writes done.round{N}.{phase}.json on exit. See
docs/persistent_agent_async_phases_plan_v1.md.
"""
from __future__ import annotations
import json, os, signal, subprocess, sys, time
from pathlib import Path


class ClaudeAgentRunner:
    """Persistent in-container daemon that executes Claude per cue."""

    def __init__(self, state_dir: Path = Path("/tmp/agent")) -> None:
        self.S = state_dir
        self.prompt   = self.S / "prompt.txt"
        self.context  = self.S / "phase_context.json"
        self.cue      = self.S / "cue"
        self.kill     = self.S / "kill"
        self.shutdown = self.S / "shutdown"
        self.workdir  = self.S / "workdir"
        self.env_file = self.S / "agent.env"
        self.mcp      = self.S / "mcp.json"
        self.runner_pid    = self.S / "runner.pid"
        self.claude_pid    = self.S / "current_claude.pid"
        self.transcript    = self.S / "transcript.log"

    # ── public ─────────────────────────────────────────────────────

    def run_forever(self) -> int:
        signal.signal(signal.SIGTERM, lambda *_: self._exit())
        signal.signal(signal.SIGINT,  lambda *_: self._exit())
        self.runner_pid.write_text(str(os.getpid()))
        while True:
            self._wait_for_cue()
            self._run_one_cycle()

    # ── internals (unit-testable) ──────────────────────────────────

    def _wait_for_cue(self) -> None:
        while not self.cue.exists():
            if self.shutdown.exists():
                self._exit()
            time.sleep(0.5)
        self.cue.unlink(missing_ok=True)

    def _exit(self, code: int = 0) -> None:
        for p in (self.cue, self.kill):
            p.unlink(missing_ok=True)
        sys.exit(code)

    def _load_env(self) -> None:
        """Parse `export K=V` lines from agent.env into os.environ."""
        if not self.env_file.exists():
            return
        for line in self.env_file.read_text().splitlines():
            s = line.strip()
            if s.startswith("export "):
                s = s[7:]
            if not s or "=" not in s:
                continue
            k, _, v = s.partition("=")
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            os.environ[k] = v

    def _atomic_json(self, path: Path, payload: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)

    def _run_one_cycle(self) -> None:
        if not self.context.exists():
            sys.stderr.write("runner: cue without phase_context.json\n")
            time.sleep(1)
            return
        ctx = json.loads(self.context.read_text())
        rid, phase = int(ctx["round_id"]), str(ctx["phase"]).lower()
        sfx = f"round{rid}.{phase}"
        stdout_p = self.S / f"stdout.{sfx}.log"
        stderr_p = self.S / f"stderr.{sfx}.log"
        done_p   = self.S / f"done.{sfx}.json"
        done_p.unlink(missing_ok=True)

        self._load_env()
        model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
        started = time.time()

        with open(stdout_p, "wb") as out, open(stderr_p, "wb") as err, \
             open(self.prompt, "rb") as inp:
            proc = subprocess.Popen(
                ["claude", "-p", "--continue",
                 "--output-format", "stream-json", "--verbose",
                 "--dangerously-skip-permissions",
                 "--model", model,
                 "--mcp-config", str(self.mcp)],
                stdin=inp, stdout=out, stderr=err, cwd=str(self.workdir),
            )
            self.claude_pid.write_text(str(proc.pid))
            while proc.poll() is None:
                if self.kill.exists():
                    self.kill.unlink(missing_ok=True)
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                time.sleep(0.5)

        ended = time.time()
        with open(self.transcript, "ab") as t, open(stdout_p, "rb") as src:
            t.write(src.read())

        self._atomic_json(done_p, {
            "round_id": rid, "phase": phase,
            "exit_code": proc.returncode,
            "started_at": started, "ended_at": ended,
            "claude_pid": proc.pid,
            "stdout_path": str(stdout_p),
            "stderr_path": str(stderr_p),
        })


if __name__ == "__main__":
    sys.exit(ClaudeAgentRunner().run_forever())
```

Python 3 is already in `cyberarena/dev-base` (notes service +
checker.sh use it). No new image dependencies.

**Test surface**: `_load_env`, `_atomic_json`, `_run_one_cycle` (mock
`subprocess.Popen` + drive paths under `tmp_path`) each unit-test in
isolation.

---

## File IPC contract

All inside the team's dev container, under `/tmp/agent/`.

| File | Direction | Lifecycle |
|---|---|---|
| `runner.py`, `agent.env`, `mcp.json` | static | install once; gone at teardown |
| `runner.pid` | runner → manager | written at runner boot; manager `kill -0`s for liveness |
| `workdir/` | static dir | claude's isolated cwd for `--continue` session selection |
| `prompt.txt` | manager → runner | overwritten each phase |
| `phase_context.json` | manager → runner | overwritten each phase; `{round_id, phase}` |
| `cue` | manager → runner | created per phase; consumed by runner |
| `done.round{N}.{phase}.json` | runner → manager | one per phase, atomic write; consumed by `wait_for_done` |
| `stdout.round{N}.{phase}.log`, `stderr...` | claude → fs | per-phase, named so the next phase can't clobber |
| `transcript.log` | runner appends | cumulative per-phase stdout for replay |
| `kill` | manager → runner | timeout abort signal |
| `shutdown` | manager → runner | game-end exit signal |
| `current_claude.pid` | runner → fs | debug only |

**Invariants**:
- Runner writes `done.json` atomically via `tmp → mv`.
- One cue at a time — `wait_for_done` blocks before next `send_prompt`.
- Runner refuses cycles without `phase_context.json`.

### Per-phase artifact handoff

After each `wait_for_done` returns, `ClaudeAgentManager` copies the
three per-phase outputs into the run's artifact dir
(`runs/<ts>/artifacts/team{N}/round-{N}/`):

| File in container | Copied artifact name | What it is |
|---|---|---|
| `/tmp/agent/stdout.round{N}.{phase}.log` | `stream-{phase}.jsonl` | LLM stream-json transcript for the phase |
| `/tmp/agent/stderr.round{N}.{phase}.log` | `agent-{phase}.log` | stderr + supervisor diagnostics |
| (constructed at copy time) | `metadata-{phase}.json` | `{round_id, phase, exit_code, duration_ms, claude_pid, started_at, ended_at, prompt_first_line}` — enriched from `done.json` so post-mortem readers don't have to scan stream-json for basic facts |

Naming aligns with the artifact-store layout from
[`docs/DEFCON_challenge_framework_report_v1.md`](DEFCON_challenge_framework_report_v1.md);
`stream-{phase}.jsonl` and `agent-{phase}.log` exist there already and
just get sourced differently in the new model.

---

## Events

Reuse existing:
- `AGENT_INSTALLED` — runner bootstrap done (once per team).
- `AGENT_LAUNCHED` — `runner.pid` captured (once per team, stable).
- `AGENT_READY` — agent called MCP `report_ready` (unchanged).

New (per-phase):
- `AGENT_PROMPT_SENT` — `cue` dropped. Payload: `{phase, round_id, prompt_first_line}`.
- `AGENT_COMPLETED` — `done.json` consumed. Payload: `{exit_code, duration_ms, claude_pid, phase, round_id}`.
- `AGENT_TIMEOUT` — phase timer fired first. Payload: same shape.
- `AGENT_RUNNER_DEAD` — pre-send liveness check failed. Payload: `{team_id, runner_pid, phase, round_id}`.

---

## Code changes

| File | Change |
|---|---|
| `agents/runner.py` (new) | `ClaudeAgentRunner` class (the in-container daemon). Copied into the container by `install()`. |
| `agents/claude.py` | `ClaudeAgentManager.install()` rewritten as **one-shot per team**: writes runner.py + env + mcp.json + starts the daemon, captures `runner.pid`, fires `AGENT_INSTALLED` + `AGENT_LAUNCHED`. Existing `_launch` (one-shot CLI per call) retires. New methods: `send_prompt(handle, text, round_id, phase)`, `await wait_for_done(handle, round_id, phase, timeout) → DoneInfo`, `kill(handle)`. |
| `models/agent.py` | New `DoneInfo` dataclass mirroring the `done.json` payload. No `AgentConfig.mode` field needed — there is only one model. |
| `gamemgr/game_mgr.py` | New once-per-game pre-loop hook that installs every team's runner before round 1 begins (parallel via `asyncio.gather`). Kickoff handlers stop calling `install`; they do `send_prompt + wait_for_done`. Track first-prompt-per-team in `_agent_briefed: set[str]` so the long briefing only goes out once. |
| `gamemgr/events.py` | Add `AGENT_PROMPT_SENT`, `AGENT_COMPLETED`, `AGENT_TIMEOUT`, `AGENT_RUNNER_DEAD`. |
| `agents/prompts/*` | First-phase prompt = full briefing + first task. Subsequent prompts = 2–3 lines. |
| `panel/panel.html` | Render new event chips. |
| `main.py` | `_wire_defense_kickoff` → `_wire_phase_kickoffs` (no longer calls `install`; that runs once in the new pre-loop hook). |
| `tests/test_dcf_persistent_runner.py` (new) | Unit-test `ClaudeAgentRunner` helpers with mocked `subprocess.Popen` + paths under `tmp_path`. One docker-gated live test running install → send_prompt → wait_for_done end-to-end. |

**Existing tests** (`test_m4_agent.py`, `test_m4_5_install_emits.py`, `test_m5_6_agent_attack.py`) each call `install()` once in isolation, so they don't need changes — `install()`'s shape (params, callbacks, side-effects) is preserved; only its *internals* (start the runner instead of launching one-shot Claude) change. We re-run the suite to confirm; any test that turns out to assume one-shot-CLI semantics gets updated as part of the implementation PR.

**Estimate**: ~150 LOC + 5 tests.

---

## Risks

| Risk | Mitigation |
|---|---|
| Runner crashes between phases | Pre-`send_prompt` liveness check via `kill -0 $(cat runner.pid)`. On failure, emit `AGENT_RUNNER_DEAD` and skip the team for the phase. |
| Runner crashes during a phase | `AGENT_TIMEOUT` fires from phase timer; next phase's liveness check catches the dead runner. |
| `--continue` resumes wrong session | Dedicated `workdir/` cwd isolates sessions to this arena's run. |
| `done.json` partial-read race | Atomic `tmp → mv` write inside runner. |
| `cue` arrives mid-phase | Cannot — `wait_for_done` blocks before next `send_prompt`. |
| Conversation tokens grow unbounded | Deferred. Options: `--fork-session` at round boundary, compress-history MCP tool. |
| Mid-game runner recovery | Deferred. v1 lets a dead runner sit out the rest of the game. |

---

## Async mode (deferred, NOT in scope)

Future: send ONE setup prompt at game start; add MCP tools
(`get_round_state`, `list_opposing_prods`, `list_active_flags`); let
the agent pace itself across defense + attack.

Constraints v1 honors so async stays reachable:
- ❌ Don't bake phase-time-budgets into per-challenge prompts.
- ✅ Keep new MCP tools additive — never change existing tool shapes.

---

## Decisions (resolved)

| # | Decision | Choice |
|---|---|---|
| D1 | Runner language? | **Python** — testable; `subprocess` is first-class for process management, `json` module for safe serialization, no quoting hazards. Python 3 already in dev-base. |
| D2 | Coexist vs replace? | **Fully replace.** `claude -p --continue` IS headless mode; "persistent" just means we install once per team and drive per-phase work via the runner's cue/done protocol instead of re-running install per phase. No mode flag. Existing M4/M4.5/M5.6 tests stay valid since they each call `install()` once in isolation. |
| D3 | Per-phase artifacts copied to `runs/<ts>/artifacts/team{N}/round-{N}/`? | **stdout + stderr + metadata.json.** Both log files are already named per round+phase by the runner; just copy them. `metadata.json` carries `{round_id, phase, exit_code, duration_ms, claude_pid, started_at, ended_at, prompt_first_line}` — a post-processed enrichment of `done.json` (which only the wait_for_done code reads) so post-mortem readers don't need to load the streaming logs. |
| D4 | One session per team, or separate defense/attack sessions? | **One session.** Operator-role model — defense and attack share memory. Defense knowledge informs attack and vice versa. |
| D5 | First-phase prompt = briefing + first task, or briefing only at install + task only per phase? | **Combined first prompt:** the first cue carries the long briefing + the first phase's task. Subsequent cues are short 2–3 line task cues only. Avoids an empty-task install call. |
