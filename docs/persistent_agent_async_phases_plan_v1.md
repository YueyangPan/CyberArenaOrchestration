# Wrapper-Based Persistent-Memory Agent — Plan v1

## Goals

- Claude Code is `npm install`-ed exactly **once** per team.
- MCP config, SSH key, env vars are written exactly **once**.
- Agent **retains memory across rounds and phases** (defense + attack
  share the same conversation, via Claude CLI's `--continue`).
- Orchestrator **drives DEFENSE / ATTACK explicitly per round** —
  same loop semantics as today's M5.6 + M7 wire-up.
- **No tmux, no PTY, no screen scraping, no sentinel parsing.**

## Non-goals

- Single long-lived Claude process. Each phase is a fresh
  `claude -p --continue` invocation; the *wrapper* (a small bash
  daemon in the dev container) is the persistent process.
- One stable PID for "the agent" across all phases. The **wrapper's
  PID** is stable; Claude's PID changes each round, and we record both.
- Async mode (deferred — see "Async mode" below for the parking lot).

---

## Architecture

```
┌─ Host orchestrator ────────────────────────────────────────────┐
│  GameMgr / kickoff handlers                                    │
│    │   (file IPC inside the dev container)                     │
│    ▼                                                           │
└─────────┬──────────────────────────────────────────────────────┘
          │
┌─ team{N}_dev container ───────────────────────────────────────┐
│  /tmp/agent/wrapper.sh   (long-lived; PID in wrapper.pid)     │
│    │                                                          │
│    │ spawns one short-lived child per phase                   │
│    ▼                                                          │
│  claude -p --continue --mcp-config mcp.json --model …         │
│    < prompt.txt > stdout.log 2> stderr.log                    │
│    │                                                          │
│    │ uses Claude CLI's built-in session memory                │
│    │ under /root/.claude/projects/<cwd>/<session>.jsonl       │
│    ▼                                                          │
│   exits → wrapper writes done.json, loops back to wait        │
└───────────────────────────────────────────────────────────────┘
```

Round-by-round, sync mode:

```
Round 1 DEFENSE:  orchestrator drops prompt.txt + cue
                  → wrapper sees cue, runs claude (PID 101)
                  → claude exits, wrapper writes done.json
                  → orchestrator reads done.json, moves to ATTACK
Round 1 ATTACK :  PID 102
Round 2 DEFENSE:  PID 103
Round 2 ATTACK :  PID 104
Round 3 DEFENSE:  PID 105
Round 3 ATTACK :  PID 106
```

Every `claude` call passes `--continue`, so each invocation resumes
the prior session. From the agent's view: one continuous conversation
across all six phases. From the system's view: six short-lived child
processes managed by one persistent bash supervisor.

---

## Lifecycle

### Game start — `install()` runs ONCE per team

1. `npm install -g @anthropic-ai/claude-code`.
2. `_provision_ssh_pivot` (generate + push pubkey to own_prod).
3. Write `/tmp/agent/mcp.json` (per M4.5).
4. Write `/tmp/agent/agent.env` (`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLAUDE_MODEL`, etc.).
5. Write `/tmp/agent/wrapper.sh`.
6. `nohup setsid /tmp/agent/wrapper.sh </dev/null >wrapper.stdout 2>wrapper.stderr &`.
7. Wait for `/tmp/agent/wrapper.pid` to appear; record it.

Subsequent phases never re-run install.

### Each phase — `send_prompt` + `wait_for_done`

Kickoff handler replaces the current "install each phase" pattern:

```python
mgr.send_prompt(handle, phase_prompt_text, round_id=N, phase="DEFENSE")
# → emits AGENT_PROMPT_SENT
result = await mgr.wait_for_done(handle, round_id=N, phase="DEFENSE",
                                  timeout=phase_secs)
# → emits AGENT_COMPLETED   (on done.round{N}.{phase}.json arrival)
# → emits AGENT_TIMEOUT     (if timer fires first; orchestrator
#                            drops `kill` and waits a grace period)
```

`send_prompt`:
1. write `/tmp/agent/prompt.txt`
2. write `/tmp/agent/phase_context.json` with `{round_id, phase}`
3. create `/tmp/agent/cue`

The wrapper consumes the cue, reads `phase_context.json` to compute
its output-file suffix (`round{N}.{phase}`), and runs Claude. Output
files (`stdout.round{N}.{phase}.log`, `stderr...`, `done...`) are
named so a missed orchestrator copy can't be overwritten by the next
phase.

`wait_for_done`: poll for the EXPECTED `done.round{N}.{phase}.json`
file up to `timeout`. On timeout, drop `/tmp/agent/kill` and wait a
short grace window for the wrapper to surface a forced-exit done file.

### Game end

Orchestrator drops `/tmp/agent/shutdown`; wrapper exits cleanly.
Belt-and-suspenders: `kill -TERM <wrapper_pid>` afterward.

---

## Wrapper script (`/tmp/agent/wrapper.sh`)

Bash, ~60 lines. Written by `install()`, not baked into the image.

```bash
#!/usr/bin/env bash
set -euo pipefail

S=/tmp/agent
PROMPT=$S/prompt.txt
CONTEXT=$S/phase_context.json   # orchestrator writes {round_id, phase} before cue
CUE=$S/cue
KILL=$S/kill
SHUTDOWN=$S/shutdown
TRANSCRIPT=$S/transcript.log
CLAUDE_PID_FILE=$S/current_claude.pid

echo $$ > "$S/wrapper.pid"

cleanup() { rm -f "$CUE" "$KILL"; exit 0; }
trap cleanup TERM INT

while true; do
  # Wait for a cue (or shutdown signal)
  while [ ! -f "$CUE" ]; do
    [ -f "$SHUTDOWN" ] && cleanup
    sleep 0.5
  done
  rm -f "$CUE"

  # Read round_id + phase from phase_context.json so per-phase artifacts
  # don't overwrite each other. Refuse to run if missing — the
  # orchestrator must always pair a cue with a context.
  if [ ! -f "$CONTEXT" ]; then
    echo "wrapper: cue received without phase_context.json; aborting cycle" >&2
    sleep 1
    continue
  fi
  ROUND_ID=$(jq -r .round_id "$CONTEXT")
  PHASE=$(jq -r .phase    "$CONTEXT" | tr '[:upper:]' '[:lower:]')
  SUFFIX="round${ROUND_ID}.${PHASE}"

  STDOUT="$S/stdout.${SUFFIX}.log"
  STDERR="$S/stderr.${SUFFIX}.log"
  DONE="$S/done.${SUFFIX}.json"
  rm -f "$DONE"

  STARTED_AT=$(date +%s.%N)
  set -a; . "$S/agent.env"; set +a

  claude -p --continue \
    --output-format stream-json --verbose \
    --dangerously-skip-permissions \
    --model "$CLAUDE_MODEL" \
    --mcp-config "$S/mcp.json" \
    < "$PROMPT" > "$STDOUT" 2> "$STDERR" &
  CLAUDE_PID=$!
  echo "$CLAUDE_PID" > "$CLAUDE_PID_FILE"

  while kill -0 "$CLAUDE_PID" 2>/dev/null; do
    if [ -f "$KILL" ]; then
      rm -f "$KILL"
      kill -TERM "$CLAUDE_PID" 2>/dev/null || true
      sleep 1
      kill -KILL "$CLAUDE_PID" 2>/dev/null || true
    fi
    sleep 0.5
  done

  # Capture wait's exit code without `|| true` swallowing it (the
  # set -e at top of the script means a raw `wait` on a non-zero exit
  # aborts the loop — use set +e around it instead).
  set +e
  wait "$CLAUDE_PID" 2>/dev/null
  EXIT_CODE=$?
  set -e
  ENDED_AT=$(date +%s.%N)

  # Append per-phase stdout to the cumulative transcript for replay
  cat "$STDOUT" >> "$TRANSCRIPT"

  # Atomic write via tmp + rename so the orchestrator never reads a
  # partial done JSON.
  cat > "$DONE.tmp" <<EOF
{
  "round_id":     $ROUND_ID,
  "phase":        "$PHASE",
  "exit_code":    $EXIT_CODE,
  "started_at":   $STARTED_AT,
  "ended_at":     $ENDED_AT,
  "claude_pid":   $CLAUDE_PID,
  "stdout_bytes": $(wc -c < "$STDOUT"),
  "stdout_path":  "$STDOUT",
  "stderr_path":  "$STDERR"
}
EOF
  mv "$DONE.tmp" "$DONE"
done
```

Two robustness details worth noting:

- **`wait` exit-code capture** uses `set +e / set -e` to bracket the
  call. `wait "$PID" 2>/dev/null || true` would silently mask any
  non-zero exit because `|| true` resets `$?` to 0 before we can read
  it; `set +e` keeps the exit code accessible.
- **Per-phase output filenames** carry `roundN.phaseX` so a missed
  artifact copy on the orchestrator side doesn't get clobbered by the
  next phase. The orchestrator must write `phase_context.json` before
  every `cue` — the wrapper refuses to run a cycle without it.

Why bash and not python: already in the base image; no dependency
added; ~60 lines is tractable. `jq` is also already in dev-base via
the existing toolchain.

---

## File IPC contract

All paths inside the team's dev container.

| File | Direction | Created by | Removed by | Purpose |
|---|---|---|---|---|
| `wrapper.sh` | static | `install()` | game-end teardown | the daemon |
| `wrapper.pid` | wrapper → orch | wrapper at boot | (stays for the game) | track persistent PID |
| `agent.env`, `mcp.json` | static | `install()` | teardown | unchanged from M4.5 |
| `prompt.txt` | orch → wrapper | `send_prompt` (overwrite each phase) | (stays) | phase prompt body |
| `phase_context.json` | orch → wrapper | `send_prompt` (overwrite each phase) | (stays) | `{round_id, phase}` — wrapper reads this to suffix output files |
| `cue` | orch → wrapper | `send_prompt` | wrapper at consume | "new prompt ready" sentinel |
| `done.round{N}.{phase}.json` | wrapper → orch | wrapper at claude-exit (atomic) | `wait_for_done` after read | `{round_id, phase, exit_code, started_at, ended_at, claude_pid, stdout_bytes, stdout_path, stderr_path}` — one per phase, named so a missed copy doesn't get clobbered |
| `stdout.round{N}.{phase}.log` | claude → fs | wrapper per invocation | game-end teardown | per-phase stream-json. **Named** per round+phase so the next phase doesn't overwrite — orchestrator copies into the per-round artifact dir at leisure. |
| `stderr.round{N}.{phase}.log` | claude → fs | wrapper per invocation | game-end teardown | per-phase stderr, same naming |
| `kill` | orch → wrapper | `wait_for_done` on timeout | wrapper at consume | abort current claude |
| `shutdown` | orch → wrapper | game-end teardown | (wrapper exits) | exit daemon loop |
| `transcript.log` | wrapper appends | wrapper per phase | teardown | cumulative stream-json across the game (artifact source) |
| `current_claude.pid` | wrapper → fs | wrapper per invocation | (overwritten) | debug — which Claude is running now |

Why files over FIFOs / sockets: idempotent, inspectable
(`docker exec dev ls /tmp/agent/` shows the entire IPC surface), no
connection state, no timeout coupling. Polling cost is negligible.

Race-safe writes: the wrapper always writes `done.json` via
`tmp → mv` so a reader can't catch a partial JSON.

One-cue-at-a-time invariant: the wrapper only consumes the next `cue`
after `done.json` exists. Orchestrator's `wait_for_done` blocks before
sending the next prompt, so the protocol is never pipelined.

---

## Events

Existing (semantics preserved):

- `AGENT_INSTALLED` — once per team after `install()` completes.
- `AGENT_LAUNCHED` — once per team after `wrapper.pid` is captured.
  Payload includes `wrapper_pid` (now stable for the whole game).
- `AGENT_READY` — when the agent calls MCP `report_ready` (unchanged).

New (per-phase):

- `AGENT_PROMPT_SENT` — orchestrator emits after dropping `cue`.
  Payload: `{phase, round_id, prompt_first_line}`.
- `AGENT_COMPLETED` — orchestrator emits after consuming `done.json`.
  Payload: `{exit_code, duration_ms, claude_pid, phase, round_id}`.
- `AGENT_TIMEOUT` — phase timer fired before `done.json`; `kill` was
  dropped; the work didn't finish in time.

Phase early-end (existing) stays: `DEPLOY_COMPLETE` from MCP drains
`_defense_pending` and can short-circuit DEFENSE. `AGENT_COMPLETED`
confirms the wrapper actually saw the process exit.

---

## Memory: how `--continue` makes it work

Claude CLI writes its conversation to
`/root/.claude/projects/<cwd-encoded>/<session-id>.jsonl` after every
turn. Subsequent invocations with `--continue` pick the most-recent
session for that cwd and replay it as conversation history.

Verified live:

```
docker exec dev claude -p ...           < "Remember 8472, say OK"  → "OK"
docker exec dev claude -p --continue ... < "What number?"          → "**8472**"
```

This survives the wrapper restarting between phases — the session
file lives on disk. Conversation grows monotonically; token-cost
mitigation (per-round `--fork-session`, or a custom
`compress_history` MCP tool) is deferred.

---

## Turn completion: process exit

The wrapper's `wait $CLAUDE_PID` is the entire signal:

- **Process-exit is unambiguous.** No sentinel parsing, no screen
  scraping, no false positives from the agent quoting a string.
- **Exit code is the truth.** 0 = clean turn end; non-zero = either
  Claude errored or our `kill` killed it.
- **Timeout handling** lives in the orchestrator: phase timer fires →
  drop `kill` → wrapper SIGTERMs Claude → wrapper writes `done.json`
  with non-zero exit_code → orchestrator emits `AGENT_TIMEOUT`.

---

## What changes in code

| File | Change |
|---|---|
| `agents/claude.py` | `install()` becomes one-shot per team: writes wrapper.sh + env + mcp.json + starts the wrapper via setsid. New methods `send_prompt(handle, text)` and `await wait_for_done(handle, timeout) → DoneInfo`. The current `_launch` retires for this mode. |
| `models/agent.py` | `AgentConfig.mode: Literal["headless", "persistent"] = "headless"`. New `DoneInfo` dataclass: `{exit_code, started_at, ended_at, duration_ms, claude_pid}`. |
| `config/game_ico.yaml` | `agent.mode: persistent` per team. |
| `gamemgr/game_mgr.py` | Add a once-per-game install hook (every team's wrapper started before the round loop begins). Kickoff handlers become `send_prompt + wait_for_done`; no install per phase. |
| `gamemgr/events.py` | Add `AGENT_PROMPT_SENT`, `AGENT_COMPLETED`, `AGENT_TIMEOUT`. |
| `agents/prompts/*` | First-prompt-per-team gets the full briefing (operator role, topology, MCP tools, ico protocol, discipline rules). Subsequent prompts are 2–3 lines — Claude has full context from prior turns via `--continue`. Track first-vs-subsequent via `_agent_briefed: set[team_id]` on GameMgr. |
| `panel/panel.html` | Render the new events as chips. |
| `main.py` | `_wire_defense_kickoff` → `_wire_phase_kickoffs` to reflect the new flow. |
| `tests/test_dcf_persistent_wrapper.py` | New: stub the wrapper with a Python `sleep`-based double for unit tests; one docker-gated live test running install → send_prompt → wait_for_done end-to-end. |

Estimate: ~150 LOC + ~5 tests.

---

## Why this is better than driving a long-lived TUI process

1. **Memory works the same way** (`--continue`) regardless of how the
   CLI is launched.
2. **No tmux dependency, no PTY**, no terminal control codes to parse.
3. **Turn-done is deterministic** — process exit, not screen
   scraping. No need for an MCP `turn_done` tool or sentinel fallback.
4. **Easier debug** — `stdout.log` / `stderr.log` / `exit_code` per
   phase are plain files. No ANSI to strip.
5. **Less code** — a 50-line bash wrapper + small orchestrator
   changes vs. a full TUI driver with capture-pane polling.

---

## Risks / open items

| Risk | Mitigation |
|---|---|
| Wrapper crashes mid-phase | `AGENT_LAUNCHED` carries `wrapper_pid`; a watchdog subscriber can `kill -0` to detect. Recovery (restart wrapper) deferred — for v1 we just emit `AGENT_TIMEOUT` and move on. |
| `done.json` partial-read race | Atomic `tmp → mv` write inside wrapper. |
| `cue` arrives mid-phase | Cannot — `wait_for_done` blocks before next `send_prompt`. Protocol is strictly one-cue-at-a-time. |
| Conversation tokens grow | Deferred. Options: `--fork-session` at round boundary, compress-history MCP tool. |
| `/root/.claude/` lost | Wouldn't survive container restart anyway; arena dev container is persistent for the game's duration. Out of scope. |

---

## Async mode (parking lot, NOT in scope)

Future direction: drop the per-phase `send_prompt`. Send ONE setup
prompt at game start telling the agent the game is on. Add MCP tools
(`get_round_state`, `list_opposing_prods`, `list_active_flags`). The
agent paces itself — can attack while defending, etc. Orchestrator
still owns round counter + network switching + flag rotation.

Constraints on this v1 design that keep async reachable later:

- ❌ Don't bake "agent has a fixed phase time budget" into per-challenge
  prompts. Time budgets belong in the per-phase boilerplate that
  async mode will simply omit.
- ❌ Don't tear down the wrapper at end of phase. (Already not the plan.)
- ✅ Keep MCP tools additive — new ones don't change existing surface.

---

## Decision points (need user sign-off before code lands)

| # | Decision | Default proposed |
|---|---|---|
| D1 | Wrapper in **bash** vs **python**? | **bash** — no new dependency in dev-base; 50 lines tractable. |
| D2 | Replace headless `-p` mode entirely, or coexist? | **Coexist** — `mode: headless` stays for M4 / M5 / M5.6 / M4.5 tests + smoke; `mode: persistent` is the new game default for game_ico.yaml. |
| D3 | Per-phase artifact: what gets copied to `runs/<ts>/artifacts/team{N}/round-{N}/`? | Copy a byte-range slice of `transcript.log` (from cursor at phase start → cursor at phase end). `stdout.log` / `stderr.log` are per-phase already; copy those too if useful. |
| D4 | One session per team (both phases share memory) vs two (separate defense / attack sessions)? | **One session** — matches the "single operator" model from `cyber-arena-orchestration/src/prompts/team.py`. Defense knowledge informs attack and vice versa. |
| D5 | Long-briefing first / short-after, OR briefing once at install (no task) + short per-phase prompts? | **Long-briefing in the FIRST phase prompt** (i.e. first `cue` carries setup + first task together). Avoids a special "init prompt with no task" call. Subsequent prompts are 2–3 lines. |
