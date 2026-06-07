# CLAUDE.md

## Design Sources

All design lives in [docs/](docs/). Read the relevant doc before changing related code.

**Current (load-bearing):**

- [docs/design_v1.7.md](docs/design_v1.7.md) — overall system design
- [docs/challenge_interface_v2_plan.md](docs/challenge_interface_v2_plan.md) — Challenge interface v2 (shipped 2026-06-03): per-challenge `Challenge` ABC (VulboxImage + FunctionalityTest + FlagHandler + RestartHandler + ChallengeDocs) under `challenges/<name>/`. Live design lives in [docs/UML/challenges.md](docs/UML/challenges.md).
- [docs/persistent_memory_agent_plan_v1.md](docs/persistent_memory_agent_plan_v1.md) — install once, `claude -p --continue` per phase via `AgentTransport`
- [docs/CHALLENGE_AUTHORING_SOP.md](docs/CHALLENGE_AUTHORING_SOP.md) — SOP for collaborators packaging new challenges. Hand to outside contributors before they start.
- [docs/RUN_ON_EC2.md](docs/RUN_ON_EC2.md) — Reproducibility recipe: from a blank Linux host (any distro) to a finished DEF CON ico match. Tested on Amazon Linux 2023; works on Debian / Ubuntu with the same steps.
- [docs/UML/overview.md](docs/UML/overview.md) — system-level UML; drills into [agent_manager.md](docs/UML/agent_manager.md) · [game_manager.md](docs/UML/game_manager.md) · [timeline.md](docs/UML/timeline.md) · [flag.md](docs/UML/flag.md) · [health.md](docs/UML/health.md) · [orchestrator.md](docs/UML/orchestrator.md) · [challenges.md](docs/UML/challenges.md)

**Archived** (superseded designs + per-milestone plans/reports, kept for lineage):

- [docs/archived/](docs/archived/) — `design_v1.md`, `design_v1.6.md`, per-milestone `m{1..6}_*_plan.md` / `_report.md` (M1 topology → M6 panel, incl. M4.5 MCP and M5.5 / M5.6), and the original `DEFCON_challenge_framework_plan_v1.md` / `_report_v1.md` (superseded by `challenge_interface_v2_plan.md`).

## Update Rule

**Any major design change MUST update the corresponding doc in [docs/](docs/) in the same change.**

Major = anything that alters: module boundaries, data models, network/topology behavior, game loop semantics, agent lifecycle, or external interfaces.

- If an existing doc covers the area, edit it.
- If no doc covers the area, add a new one under `docs/` and link it from this file.
- Update this index when adding or removing a doc.
- Do not edit anything under `docs/archived/` — those are historical artifacts. Supersede instead.
