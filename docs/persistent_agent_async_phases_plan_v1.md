# Persistent TUI Agent + Sync/Async Phases — Plan v1

**Goal**: One Claude Code TUI process per team that stays alive for the
whole game. Two execution modes for how the agent's work is driven:

- **Sync mode** *(this milestone)* — explicit DEFENSE and ATTACK phases
  driven by the round loop. The orchestrator types the phase-specific
  prompt into the TUI before each phase and waits for the agent's
  completion signal (or phase timer) before advancing. This is the
  M3-compatible model: phases are real, the network is switched per
  phase, the agent does what the current phase says.
- **Async mode** *(deferred to v2)* — no explicit phases. The agent
  decides for itself when to defend and when to attack — and can do
  both simultaneously. The orchestrator stops pushing prompts; the
  agent pulls round state from MCP (`get_round_state`, etc.) and
  paces itself. The network is still switched per phase (orchestrator
  side), but the agent doesn't have phase-shaped tasks.

**Status**: spec only. Implementation of sync mode pending sign-off
on D1–D6 below. Async mode deferred — design sketched in §Async mode
(deferred) so the sync-mode plumbing doesn't paint us into a corner.

---

## Out of scope (deferred to follow-ups)

- Multi-challenge per team (still one challenge per game).
- Token-cost optimization (conversation grows monotonically; revisit
  if it bites).
- Per-agent budget enforcement (LLM cost guardrails).
- Tool-call streaming through tmux (we still ship MCP tools via
  `--mcp-config`; tmux is only for the prompt + completion-sentinel I/O).
- Persistent agent for the existing `-p` headless path (this plan
  REPLACES that path for game_ico.yaml; `-p` stays for tests + smoke).

---

## Architecture: one tmux session per team

The persistent agent runs in `claude` interactive (TUI) mode inside a
detached tmux session living in the team's dev container. Orchestrator
operates the session via two tmux commands:

| What the orchestrator wants to do | tmux call |
|---|---|
| Type a prompt + press Enter | `tmux send-keys -t <session> "<prompt>" Enter` |
| Read the current visible pane (for sentinel polling) | `tmux capture-pane -t <session> -p -S -<lines>` |
| Append-only history (for the api-logs artifact) | `tmux pipe-pane -t <session> -O 'cat >> /tmp/agent/transcript.log'` |

tmux is chosen because:
- It's already a battle-tested PTY supervisor — readline / cursor
  control / ANSI all just work.
- The session persists across orchestrator → container `docker exec`
  invocations (the tmux server, not docker, owns the PTY).
- `capture-pane` + `pipe-pane` give us both snapshots (for the
  sentinel polling loop) and a full transcript (for `stream-jsonl`'s
  replacement, `transcript.log`).

```
┌─ team1_dev container ─────────────────────────────────────────────┐
│  tmux server                                                      │
│   └── session "agent"                                             │
│         └── pane: claude (interactive, --mcp-config /tmp/agent/mcp.json)│
│              └── PTY → /tmp/agent/transcript.log (via pipe-pane)  │
│                                                                   │
│  /tmp/agent/                                                      │
│    prompt.txt              ← latest phase prompt (sentinel-tagged)│
│    transcript.log          ← full TUI output stream               │
│    mcp.json                ← MCP tools (unchanged from M4.5)      │
│    agent.env               ← API key, base url, IS_SANDBOX        │
└───────────────────────────────────────────────────────────────────┘
```

---

## Sync mode (default; what we replace the current --continue model with)

### Kickoff per phase

```
1. Orchestrator builds the phase prompt (defense or attack, possibly short-
   form since the agent has prior-round memory in the TUI conversation).
2. Append a deterministic sentinel: "When you finish this phase, emit
   exactly the line [[TURN_DONE]] and nothing else after it."
3. tmux send-keys -t agent "<prompt>" Enter
4. Poll tmux capture-pane in a loop:
     - if "[[TURN_DONE]]" appears → emit PHASE_TURN_DONE event, stop polling
     - if phase timer elapses → emit PHASE_TIMEOUT, send Esc/Ctrl-C to abort, stop polling
     - else sleep 1s and re-poll
5. Round loop advances to next phase.
```

### New events

| Event | When |
|---|---|
| `AGENT_PROMPT_SENT` | After `send-keys`. payload: `{phase, round, prompt_first_line}` |
| `AGENT_TURN_DONE` | When sentinel sighted in capture-pane |
| `AGENT_TURN_TIMEOUT` | When phase timer expires before sentinel |

(Existing `AGENT_LAUNCHED` fires once per team at game start, not per phase.)

### Phase completion semantics in sync mode

- DEFENSE phase: ends when `AGENT_TURN_DONE` from both teams' agents,
  OR `defense_secs` elapses. Whichever first. (Already what
  `_on_deploy_complete` does today via the early-end signal — sync
  mode is just rewiring it from MCP report to TUI sentinel.)
- ATTACK phase: same — ends on both `AGENT_TURN_DONE` or `attack_secs`.

### Lifecycle

| Stage | Action |
|---|---|
| Game start (first phase entry per team) | `install()` runs ONCE: npm + SSH pivot + mcp.json + start tmux session running `claude --mcp-config ...` (interactive). PID = the tmux server's PID; tracked in `/tmp/agent/tmux.pid`. |
| Each subsequent phase | NO install. orchestrator: write prompt file + `tmux send-keys` + poll for sentinel. |
| Game end (GAME_END event) | `tmux kill-session -t agent` for every team. |

### What changes in code

| File | Change |
|---|---|
| `agents/claude.py` | New mode: `tui` (vs the current `headless`). `install()` branches: tui → start tmux session running interactive `claude` with `--mcp-config`. New methods: `send_prompt(handle, prompt) → None`, `await_turn_done(handle, team_id, round_id, phase, timeout_secs) → bool`. The await races MCP-tool resolution against capture-pane sentinel polling. |
| `gamemgr/mcp_server.py` | New tool `turn_done(reason)` resolves a per-`(team, round, phase)` future tracked on GameMgr. Idempotent — second call is a no-op. |
| `models/agent.py` | `AgentConfig.mode: Literal["headless", "tui"] = "headless"` |
| `config/game_ico.yaml` | `agent.mode: tui` per team |
| `gamemgr/game_mgr.py` | Kickoff handlers call `agent_mgr.send_prompt(...)` + `await agent_mgr.await_turn_done(...)` instead of `install()`. Game start gets a new pre-loop step: install every team's tmux+claude once. |
| `gamemgr/events.py` | Add `AGENT_PROMPT_SENT`, `AGENT_TURN_DONE`, `AGENT_TURN_TIMEOUT` |
| `agents/prompts/*` | Add sentinel boilerplate to every phase prompt. The challenge-specific prompts can be much shorter (`--continue`-style brevity) since the conversation persists. |
| `panel/panel.html` | Render the new events as chips. |
| `tests/test_dcf_tui_agent.py` | New: stub tmux via shutil.which check + mocked subprocess; verify send_prompt + await_turn_done; one live test gated on docker. |

### Turn completion: MCP tool (primary), sentinel (fallback)

#### Primary: `mcp__cyberarena__turn_done()` MCP tool

New tool in `gamemgr/mcp_server.py`, parallel to the existing
`report_deploy_complete`. The agent calls it once when it finishes
the current phase's work.

```
Tool: mcp__cyberarena__turn_done
Args: { reason: "completed" | "stuck" | "no_progress" }
Returns: { ok: true }
```

Emits a `AGENT_TURN_DONE` event on the bus with `{team_id, round_id,
phase, reason}`. The kickoff handler awaits a future that this event
resolves.

Why MCP-primary:
- **Deterministic**: explicit tool call vs. screen scraping. No
  false-positive from the agent quoting a sentinel in its reasoning,
  no false-negative from ANSI / pagination corrupting the screen.
- **Already on the wire**: agents have the MCP transport open; one
  more tool is trivial. Matches how `report_deploy_complete` already
  signals "agent thinks defense is done".
- **Asymmetric to its async-mode cousin**: async mode's
  `get_round_state` is a *query* the agent pulls; `turn_done` is a
  *push* the agent emits. Both stay additive.

The phase prompt's boilerplate footer becomes:

> When you have completed the work for this phase — success, gave up,
> or out of ideas — call the MCP tool `mcp__cyberarena__turn_done`
> with `reason: "completed"` (or "stuck" / "no_progress"). DO NOT
> proceed past that tool call.

#### Fallback: screen sentinel

If the agent never calls `turn_done` (forgot, errored mid-thought,
MCP transport hiccuped), we still need a way to declare the turn
done — otherwise the round loop blocks until phase timeout. So the
boilerplate footer ALSO includes:

> If the MCP tool is unavailable, print the line
> `[[CYBERARENA_TURN_DONE]]` on its own line to signal completion.

The orchestrator polls capture-pane for this sentinel in parallel
with the MCP wait. Whichever fires first wins. Use a long unique
sentinel string per session (`CYBERARENA_TURN_DONE_<6-hex>`) to
shrink false-positive risk further.

Two-signal handling: both events end up emitting `AGENT_TURN_DONE`;
the second one is dropped via the same idempotency pattern
`_defense_kickoff_done` already uses (keyed by `(round_id, team_id,
phase)`).

#### Turn detection alternatives considered

- **Quiescence** (no output for N seconds) — vulnerable to slow tool
  calls (e.g. `fpc` recompile blocks the agent). Rejected.
- **Claude-CLI built-in markers** — no documented machine-readable
  "turn done" signal in TUI mode. Rejected.
- **Sentinel-only** — was my original v1; user flagged the false-
  positive risk, rightly. Demoted to fallback.

---

## Async mode (deferred to v2)

### Definition

Async mode flips the driving relationship. The **agent decides what to
do and when** — it can defend and attack in parallel, or focus on one,
or do nothing. The orchestrator stops sending per-phase prompts.
Instead:

- A **single long-running prompt** is sent ONCE at game start telling
  the agent the game is on, what tools it has, and that it will need
  to **query round state itself** via MCP.
- The agent uses new MCP tools (`get_round_state`, etc.) to learn what
  round it's in, whether ATTACK is currently open (cross-team network
  attached), and how much time is left.
- The agent paces itself: maybe it spends round 1 reading source for
  defense, round 2 fuzzing the opponent's prod, round 3 doing both at
  once because it has spare capacity.

The orchestrator still owns:
- Round counter + phase timers (so flag rotation + network switching
  still happen on schedule — those are infrastructure, not agent work)
- Network switching (DEFENSE detaches cross-team net; ATTACK reattaches)
- Flag rotation per round
- `GAME_END` at total_rounds

The orchestrator does NOT own:
- Per-phase agent prompts (no "now do defense" / "now do attack")
- Waiting for the agent to be done with anything

### Required MCP additions for async mode

New tools the agent will call (deferred — sketched here so sync-mode
MCP isn't named in a way that conflicts):

| Tool | Returns |
|---|---|
| `mcp__cyberarena__get_round_state()` | `{round, phase, secs_into_phase, secs_left_in_phase, total_rounds}` |
| `mcp__cyberarena__list_opposing_prods()` | `[{team_id, host, port}, ...]` |
| `mcp__cyberarena__list_active_flags()` | `[{round_id, planted_at}, ...]` — what flags are currently live and submittable |

These already partially exist as game state in `GameMgr`; exposing
them via MCP is a thin shim.

### Why this is deferred

- Sync mode is what M5.6 + M7 wire-up already supports. Lowest-risk
  evolution.
- Async mode needs the new MCP tools above, fundamentally different
  prompt design ("autonomous operator" vs "phase-task executor"), and
  a different observability story (agent's actions aren't grouped by
  phase).
- Behavior we'd want to compare empirically: does an autonomous agent
  outperform a phase-prompted one at A/D? Unknown until we have both.

### What sync mode must NOT do, to keep async mode reachable later

- ❌ Don't bake "agent has a fixed time budget per phase" into the
  per-challenge prompts at the module level. Time budgets should be
  in the **per-phase boilerplate** (so async-mode prompts can omit
  them).
- ❌ Don't tear down the tmux session at end-of-phase. (Trivially
  satisfied — sync mode also wants it alive the whole game.)
- ❌ Don't assume the agent only does work in response to an
  orchestrator prompt. Sync mode's polling loop should be willing to
  see arbitrary agent activity (e.g. an agent calling `restart_service`
  before the prompt arrives, if it has prior memory pointing it that
  way) and not consider it an error.
- ✅ DO version the MCP tools as a stable surface — new tools are
  additive; existing tools shouldn't change shape between sync and
  async.

---

## What gets simpler

- **Per-phase install is gone.** One install per team at game start.
  Saves ~3s × (2 phases × N rounds) per team. For a 3-round game with
  2 teams: ~36s less overhead.
- **Phase prompts shrink dramatically.** First prompt has full
  briefing; subsequent prompts are 2–3 lines ("ROUND 2 DEFENSE.
  patch the round-1 miss. [[CYBERARENA_TURN_DONE_a3f7e1]]"). Less
  token cost per phase.
- **One PID per team to manage** for the whole game. Cleaner kill /
  teardown semantics.

## What gets more complex

- **tmux as a dependency.** Has to be installed in `cyberarena/dev-base`.
  Trivial (`apt-get install tmux`), but a new line in the Dockerfile.
- **Output parsing for the sentinel** is screen-scraping. Robust as
  long as the sentinel is unique enough.
- **`api-logs` format changes.** No more stream-json; instead we
  capture `tmux pipe-pane` output (raw TUI bytes including ANSI
  codes). Either strip ANSI in post-processing or store raw.
- **Token cost grows monotonically** as conversation length grows.
  Mitigations later: per-round session-fork via `--fork-session`, or
  periodic conversation summarization through a custom MCP tool.

---

## Plan of attack (if approved)

1. Doc this plan (← you're reading it).
2. Add `tmux` to `cyberarena/dev-base` Dockerfile + rebuild.
3. Land sync mode end-to-end:
   - `agents/claude.py` — TUI mode + send_prompt + await_turn_done
   - `AgentConfig.mode`
   - `gamemgr/game_mgr.py` — kickoff via send/await
   - 3 new event types + panel renderers
   - tests
4. Verify with one live ico game in sync mode.
5. *(Deferred to v2)* Async mode + new MCP tools — design preserved
   above so sync-mode plumbing doesn't paint us into a corner.
6. Update `docs/design_v1.7.md` → `v1.8.md` capturing the new agent
   lifecycle.

Estimate: sync mode ~150 LOC + 3 tests.

---

## Decision points (need user sign-off before sync-mode implementation)

| # | Decision | Default proposed |
|---|---|---|
| D1 | tmux vs raw PTY vs Claude-Agent-SDK to drive the TUI? | **tmux** — robust, inspectable, no new Python deps in the orchestrator. |
| D2 | Turn-done detection? | **MCP tool primary + sentinel fallback** — `mcp__cyberarena__turn_done(reason)` is the deterministic signal; sentinel `[[CYBERARENA_TURN_DONE_<hex>]]` covers the case the agent forgets the tool call. Whichever fires first wins, second is deduped. (Sentinel-only was the original v1; demoted after user flagged false-positive risk.) |
| D3 | Replace headless `-p` mode entirely, or coexist? | **Coexist** — `mode: headless` stays for tests + smoke; `mode: tui` is the new game default. |
| D4 | What happens to per-phase `stream-{phase}.jsonl`? | **Replace** with `transcript-{phase}.log` (raw ANSI bytes captured via `tmux pipe-pane`). Optional ANSI-strip in post-processing. |
| D5 | Should the persistent agent see BOTH defense and attack prompts in one conversation, or two separate sessions per team? | **One session** — matches the "single operator" model from the `cyber-arena-orchestration/src/prompts/team.py` reference; defense knowledge informs attack and vice versa. |
| D6 | Per-phase prompts: long-briefing first / short-task after, or always-short with a one-time setup-message at game start? | **Always-short with one-time setup at game start** — sync mode literally sends ONE setup message at agent install (operator-role briefing, topology, MCP tools, ico protocol, discipline rules), then every phase sends a 2–3 line task. Avoids special-casing "is this team's first prompt". |
