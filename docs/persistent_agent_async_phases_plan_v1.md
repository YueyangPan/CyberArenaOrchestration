# Wrapper-Based Persistent-Memory Agent — Plan v1

## What we're building

One **bash wrapper daemon** per team's dev container. It stays alive
the whole game. Each phase, the orchestrator drops a prompt + cue
file; the wrapper runs `claude -p --continue` once per cue, writes a
`done.json` when Claude exits, and loops back.

**Memory** carries across phases via Claude CLI's `--continue` flag
(it picks up the most-recent session in the cwd — empirically
verified). Defense and attack share one conversation.

**No tmux. No PTY. No screen scraping.** Turn completion = process exit.

---

## Architecture

```
Host orchestrator (GameMgr)
    │  file IPC via docker exec
    ▼
team{N}_dev container
    /tmp/agent/wrapper.sh        ← long-lived bash daemon (PID stable)
        │
        │ for each phase:
        ▼
    cd /tmp/agent/workdir
    claude -p --continue --mcp-config mcp.json --model … < prompt.txt
        │
        │ writes session to /root/.claude/projects/<workdir-cwd>/<id>.jsonl
        ▼
    exits → wrapper writes done.round{N}.{phase}.json → loop
```

Per round:

```
R1 DEFENSE  →  orch drops prompt+cue → wrapper runs claude → done →
R1 ATTACK   →  orch drops prompt+cue → wrapper runs claude → done →
R2 DEFENSE  →  …
```

Each `claude` invocation is a new process but resumes the prior
conversation. Wrapper PID is stable; Claude's PID changes per phase.
Both are tracked.

---

## Lifecycle

### 1. Game start — `install()` runs **ONCE** per team

| Step | Action |
|---|---|
| 1 | `npm install -g @anthropic-ai/claude-code` |
| 2 | `_provision_ssh_pivot` (existing M5.6 SSH key push) |
| 3 | Write `/tmp/agent/{mcp.json, agent.env, wrapper.sh}` |
| 4 | `mkdir -p /tmp/agent/workdir` (claude's isolated cwd — keeps `--continue` away from leftover sessions elsewhere) |
| 5 | `nohup setsid /tmp/agent/wrapper.sh </dev/null >wrapper.stdout 2>wrapper.stderr &` |
| 6 | Wait for `/tmp/agent/wrapper.pid` to appear; record it |

### 2. Each phase — orchestrator does two things

```python
mgr.send_prompt(handle, prompt_text, round_id=N, phase="DEFENSE")
# → emits AGENT_PROMPT_SENT
result = await mgr.wait_for_done(handle, round_id=N, phase="DEFENSE",
                                  timeout=phase_secs)
# → emits AGENT_COMPLETED on success
# → emits AGENT_TIMEOUT if timer fires first (orch also drops `kill`)
# → emits AGENT_WRAPPER_DEAD if pre-send liveness check failed
```

`send_prompt` steps:

1. **Liveness check**: `kill -0 $(cat wrapper.pid)`. If dead → emit
   `AGENT_WRAPPER_DEAD`, return without dropping cue.
2. Write `/tmp/agent/prompt.txt` (the prompt body).
3. Write `/tmp/agent/phase_context.json` = `{round_id, phase}`.
4. Touch `/tmp/agent/cue`.

`wait_for_done` polls for `done.round{N}.{phase}.json`. On timeout:
drop `kill`, wait a short grace window, surface the resulting
forced-exit done.

### 3. Game end

Drop `/tmp/agent/shutdown` → wrapper exits cleanly.
Then `kill -TERM <wrapper_pid>` as belt-and-suspenders.

---

## Wrapper script (`/tmp/agent/wrapper.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

S=/tmp/agent
echo $$ > "$S/wrapper.pid"
trap 'rm -f "$S/cue" "$S/kill"; exit 0' TERM INT

while true; do
  # Wait for a cue or a shutdown signal.
  while [ ! -f "$S/cue" ]; do
    [ -f "$S/shutdown" ] && { rm -f "$S/cue" "$S/kill"; exit 0; }
    sleep 0.5
  done
  rm -f "$S/cue"

  # Read round + phase. Wrapper refuses to run without a context.
  if [ ! -f "$S/phase_context.json" ]; then
    echo "wrapper: cue without phase_context.json; skipping cycle" >&2
    sleep 1; continue
  fi
  ROUND_ID=$(jq -r .round_id "$S/phase_context.json")
  PHASE=$(jq -r .phase "$S/phase_context.json" | tr '[:upper:]' '[:lower:]')
  SUFFIX="round${ROUND_ID}.${PHASE}"
  STDOUT="$S/stdout.${SUFFIX}.log"
  STDERR="$S/stderr.${SUFFIX}.log"
  DONE="$S/done.${SUFFIX}.json"
  rm -f "$DONE"

  STARTED_AT=$(date +%s.%N)
  set -a; . "$S/agent.env"; set +a

  # Isolated cwd so --continue resumes ONLY this arena's sessions.
  cd "$S/workdir"
  claude -p --continue \
    --output-format stream-json --verbose \
    --dangerously-skip-permissions \
    --model "$CLAUDE_MODEL" \
    --mcp-config "$S/mcp.json" \
    < "$S/prompt.txt" > "$STDOUT" 2> "$STDERR" &
  CLAUDE_PID=$!
  echo "$CLAUDE_PID" > "$S/current_claude.pid"

  while kill -0 "$CLAUDE_PID" 2>/dev/null; do
    if [ -f "$S/kill" ]; then
      rm -f "$S/kill"
      kill -TERM "$CLAUDE_PID" 2>/dev/null || true
      sleep 1
      kill -KILL "$CLAUDE_PID" 2>/dev/null || true
    fi
    sleep 0.5
  done

  # Capture wait's exit code WITHOUT `|| true` (which would mask it).
  set +e
  wait "$CLAUDE_PID" 2>/dev/null
  EXIT_CODE=$?
  set -e
  ENDED_AT=$(date +%s.%N)

  cat "$STDOUT" >> "$S/transcript.log"

  # Atomic write: tmp + mv, so the orch never reads a partial JSON.
  cat > "$DONE.tmp" <<EOF
{ "round_id": $ROUND_ID, "phase": "$PHASE",
  "exit_code": $EXIT_CODE,
  "started_at": $STARTED_AT, "ended_at": $ENDED_AT,
  "claude_pid": $CLAUDE_PID,
  "stdout_path": "$STDOUT", "stderr_path": "$STDERR" }
EOF
  mv "$DONE.tmp" "$DONE"
done
```

`jq` is already in `cyberarena/dev-base`. No new image dependencies.

---

## File IPC contract

All inside the team's dev container, under `/tmp/agent/`.

| File | Direction | Lifecycle |
|---|---|---|
| `wrapper.sh`, `agent.env`, `mcp.json` | static | install once; gone at teardown |
| `wrapper.pid` | wrapper → orch | written at wrapper boot; orch `kill -0`s for liveness |
| `workdir/` | static dir | claude's isolated cwd for `--continue` session selection |
| `prompt.txt` | orch → wrapper | overwritten each phase |
| `phase_context.json` | orch → wrapper | overwritten each phase; `{round_id, phase}` |
| `cue` | orch → wrapper | created per phase; consumed by wrapper |
| `done.round{N}.{phase}.json` | wrapper → orch | one per phase, atomically written; consumed by `wait_for_done` |
| `stdout.round{N}.{phase}.log`, `stderr...` | claude → fs | per-phase, named so the next phase can't clobber |
| `transcript.log` | wrapper appends | cumulative per-phase stdout for replay |
| `kill` | orch → wrapper | timeout abort signal |
| `shutdown` | orch → wrapper | game-end exit signal |
| `current_claude.pid` | wrapper → fs | debug only |

**Invariants**:
- Wrapper writes `done.json` atomically via `tmp → mv`.
- One cue at a time — `wait_for_done` blocks before next `send_prompt`.
- Wrapper refuses cycles without `phase_context.json`.

---

## Events

Reuse existing:
- `AGENT_INSTALLED` — wrapper bootstrap done (once per team).
- `AGENT_LAUNCHED` — `wrapper.pid` captured (once per team, stable).
- `AGENT_READY` — agent called MCP `report_ready` (unchanged).

New (per-phase):
- `AGENT_PROMPT_SENT` — `cue` dropped. Payload: `{phase, round_id, prompt_first_line}`.
- `AGENT_COMPLETED` — `done.json` consumed. Payload: `{exit_code, duration_ms, claude_pid, phase, round_id}`.
- `AGENT_TIMEOUT` — phase timer fired first. Payload: same shape.
- `AGENT_WRAPPER_DEAD` — pre-send liveness check failed. Payload: `{team_id, wrapper_pid, phase, round_id}`.

---

## Code changes

| File | Change |
|---|---|
| `agents/claude.py` | `install()` becomes one-shot per team (writes wrapper.sh + env + mcp.json + starts daemon). New methods: `send_prompt(handle, text, round_id, phase)`, `await wait_for_done(handle, round_id, phase, timeout) → DoneInfo`. Existing `_launch` retires for `mode=persistent`. |
| `models/agent.py` | `AgentConfig.mode: Literal["headless", "persistent"] = "headless"`. New `DoneInfo` dataclass mirroring the `done.json` payload. |
| `gamemgr/game_mgr.py` | Add once-per-game install hook before round loop. Kickoff handlers do `send_prompt + wait_for_done` (not `install`). Track first-prompt-per-team in `_agent_briefed: set[str]` so the long briefing only goes out once. |
| `gamemgr/events.py` | Add `AGENT_PROMPT_SENT`, `AGENT_COMPLETED`, `AGENT_TIMEOUT`, `AGENT_WRAPPER_DEAD`. |
| `agents/prompts/*` | First-phase prompt = full briefing + first task. Subsequent prompts = 2–3 lines. |
| `config/game_ico.yaml` | `agent.mode: persistent` per team. |
| `panel/panel.html` | Render new event chips. |
| `main.py` | `_wire_defense_kickoff` → `_wire_phase_kickoffs`. |
| `tests/test_dcf_persistent_wrapper.py` | New: unit tests with a python `sleep`-based wrapper double + one docker-gated live test. |

**Estimate**: ~150 LOC + 5 tests.

---

## Risks

| Risk | Mitigation |
|---|---|
| Wrapper crashes between phases | `kill -0` liveness in `send_prompt` → `AGENT_WRAPPER_DEAD`, skip team for the phase. |
| Wrapper crashes during a phase | `AGENT_TIMEOUT` fires from phase timer; next phase's liveness check catches the dead wrapper. |
| `--continue` resumes wrong session | Dedicated `workdir/` cwd isolates sessions to this arena's run. |
| Conversation tokens grow unbounded | Deferred. Options: `--fork-session` at round boundary, or a compress-history MCP tool. |
| Mid-game wrapper recovery | Deferred. v1 lets a dead wrapper sit out the rest of the game. |

---

## Async mode (deferred, NOT in scope)

Future: send ONE setup prompt at game start; add MCP tools
(`get_round_state`, `list_opposing_prods`, `list_active_flags`); let
the agent pace itself across defense + attack.

To keep that path open from v1:
- ❌ Don't bake phase-time-budgets into per-challenge prompts (they
  belong in per-phase boilerplate, which async mode omits).
- ✅ Keep new MCP tools additive — never change existing tool shapes.

---

## Open decisions

| # | Decision | Default |
|---|---|---|
| D1 | Wrapper in bash vs python? | **bash** — already in dev-base, ~50 lines tractable. |
| D2 | Headless `-p` mode coexists or removed? | **Coexist** — `mode: headless` stays for M4/M5/M5.6/M4.5 tests; `mode: persistent` is new game default. |
| D3 | What gets copied per phase to `runs/<ts>/artifacts/team{N}/round-{N}/`? | The per-phase `stdout.log` + `stderr.log` directly (already named per round+phase). |
| D4 | One session per team, or separate defense/attack sessions? | **One session** — operator-role model; defense knowledge informs attack. |
| D5 | First-phase prompt = briefing + first task, or briefing only at install + task only per phase? | **Combined in first phase** — avoids an extra empty-task call at install. |
