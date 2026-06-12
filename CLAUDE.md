# CLAUDE.md

## Design Sources

All design lives in [docs/](docs/). Read the relevant doc before changing related code.

**Current (load-bearing):**

- [docs/CHALLENGE_AUTHORING_SOP.md](docs/CHALLENGE_AUTHORING_SOP.md) — SOP for collaborators packaging new challenges. Hand to outside contributors before they start.
- [docs/RUN_SAMPLE_CHALLENGE.md](docs/RUN_SAMPLE_CHALLENGE.md) — End-to-end recipe for running the bundled DEF CON ico sample challenge from a blank Linux host. Tested on Amazon Linux 2023; works on Debian / Ubuntu with the same steps.
- [docs/RUN_ARTIFACT_SPEC.md](docs/RUN_ARTIFACT_SPEC.md) — Consumer-facing spec for a run's output: `runs/<ts>/` layout, per-file meaning, the `stream-*.jsonl` event schema, and `events.jsonl`. Hand to anyone displaying/consuming run artifacts (e.g. the website).
- [docs/UML/overview.md](docs/UML/overview.md) — system-level UML, **the live design**; drills into [agent_manager.md](docs/UML/agent_manager.md) · [game_manager.md](docs/UML/game_manager.md) · [timeline.md](docs/UML/timeline.md) · [flag.md](docs/UML/flag.md) · [health.md](docs/UML/health.md) · [facility.md](docs/UML/facility.md) · [orchestrator.md](docs/UML/orchestrator.md) · [challenges.md](docs/UML/challenges.md)

**Archived** (superseded designs + per-milestone plans/reports, kept for lineage — the live design is the UML set above):

- [docs/archived/design_v1.7.md](docs/archived/design_v1.7.md) — overall system design (plan snapshot; superseded by the UML set above).
- [docs/archived/challenge_interface_v2_plan.md](docs/archived/challenge_interface_v2_plan.md) — Challenge interface v2 (shipped 2026-06-03): per-challenge `Challenge` ABC (VulboxImage + FunctionalityTest + FlagHandler + RestartHandler + ChallengeDocs) under `challenges/<name>/`. Live design in [docs/UML/challenges.md](docs/UML/challenges.md).
- [docs/archived/persistent_memory_agent_plan_v1.md](docs/archived/persistent_memory_agent_plan_v1.md) — install once, `claude -p --continue` per phase via `AgentTransport`. Live design in [docs/UML/agent_manager.md](docs/UML/agent_manager.md).
- [docs/archived/facility_abstraction_plan_v1.md](docs/archived/facility_abstraction_plan_v1.md) — Facility boundary (Phase 1 shipped 2026-06-09): FlagServer/HealthPoller talk to GameMgr through three ports (`StatusPort`/`EventSink`/`ExecPort`) bundled in a `FacilityContext`; Phases 2–3 designed, not built. Live design in [docs/UML/facility.md](docs/UML/facility.md).
- [docs/archived/async_mode_plan_v1.md](docs/archived/async_mode_plan_v1.md) — Async mode (shipped 2026-06-12): a second game mode (`round_config.mode: sync|async`) where each team runs one long-lived, supervised agent (prompted once, polls `get_game_state` over MCP), networks stay always-on so attack+defense run simultaneously at `Phase.LIVE`, and a round tick only rotates flags. Live design once shipped in [docs/UML/game_manager.md](docs/UML/game_manager.md).
- [docs/archived/](docs/archived/) — `design_v1.md`, `design_v1.6.md`, per-milestone `m{1..6}_*_plan.md` / `_report.md` (M1 topology → M6 panel, incl. M4.5 MCP and M5.5 / M5.6), and the original `DEFCON_challenge_framework_plan_v1.md` / `_report_v1.md` (superseded by `challenge_interface_v2_plan.md`).

## Update Rule

**Any major design change MUST update the corresponding doc in [docs/](docs/) in the same change.**

Major = anything that alters: module boundaries, data models, network/topology behavior, game loop semantics, agent lifecycle, or external interfaces.

- If an existing doc covers the area, edit it.
- If no doc covers the area, add a new one under `docs/` and link it from this file.
- Update this index when adding or removing a doc.
- Do not edit anything under `docs/archived/` — those are historical artifacts. Supersede instead.
