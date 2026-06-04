# CLAUDE.md

## Design Sources

All design lives in [docs/](docs/). Read the relevant doc before changing related code.

- [docs/design_v1.7.md](docs/design_v1.7.md) — overall system design (current; supersedes v1.6)
- [docs/design_v1.6.md](docs/design_v1.6.md) — prior baseline (kept for lineage)
- [docs/design_v1.md](docs/design_v1.md) — historical v1 baseline
- [docs/m1_topology_plan.md](docs/m1_topology_plan.md) / [docs/m1_topology_report.md](docs/m1_topology_report.md) — M1: topology
- [docs/m2_gameloop_plan.md](docs/m2_gameloop_plan.md) / [docs/m2_gameloop_report.md](docs/m2_gameloop_report.md) — M2: game loop
- [docs/m3_network_plan.md](docs/m3_network_plan.md) / [docs/m3_network_report.md](docs/m3_network_report.md) — M3: network switching
- [docs/m4_agent_plan.md](docs/m4_agent_plan.md) / [docs/m4_agent_report.md](docs/m4_agent_report.md) — M4: agent install & launch
- [docs/m5_flag_plan.md](docs/m5_flag_plan.md) / [docs/m5_flag_report.md](docs/m5_flag_report.md) — M5: flag plant + submit works
- [docs/m5_5_rotation_plan.md](docs/m5_5_rotation_plan.md) / [docs/m5_5_rotation_report.md](docs/m5_5_rotation_report.md) — M5.5: per-round rotation + per-team token auth
- [docs/m5_6_agent_attack_plan.md](docs/m5_6_agent_attack_plan.md) / [docs/m5_6_agent_attack_report.md](docs/m5_6_agent_attack_report.md) — M5.6: agent-driven attack loop (SSH pivot)
- [docs/m6_panel_plan.md](docs/m6_panel_plan.md) / [docs/m6_panel_report.md](docs/m6_panel_report.md) — M6: observation panel (live event stream)
- [docs/m4_5_mcp_plan.md](docs/m4_5_mcp_plan.md) / [docs/m4_5_mcp_report.md](docs/m4_5_mcp_report.md) — M4.5: MCP agent channel (replaces deferred WS reverse channel)
- [docs/DEFCON_challenge_framework_plan_v1.md](docs/DEFCON_challenge_framework_plan_v1.md) / [docs/DEFCON_challenge_framework_report_v1.md](docs/DEFCON_challenge_framework_report_v1.md) — DEFCON: pluggable ChallengeSource + nautilus-ico defense workflow
- [docs/persistent_memory_agent_plan_v1.md](docs/persistent_memory_agent_plan_v1.md) — Persistent-memory agent v1: install once, `claude -p --continue` per phase via `AgentTransport` (replaces the M4-era per-phase install + supervisor model)
- [docs/challenge_interface_v2_plan.md](docs/challenge_interface_v2_plan.md) — Challenge interface v2 (shipped 2026-06-03): pluggable per-challenge `Challenge` ABC (VulboxImage + FunctionalityTest + FlagHandler + RestartHandler + ChallengeDocs) under `challenges/<name>/`; retired ChallengeSource / FlagMechanism / Service ABC. Live design lives in [docs/UML/challenges.md](docs/UML/challenges.md).
- [docs/UML/overview.md](docs/UML/overview.md) — system-level UML; drills into [agent_manager.md](docs/UML/agent_manager.md) · [game_manager.md](docs/UML/game_manager.md) · [timeline.md](docs/UML/timeline.md) · [flag.md](docs/UML/flag.md) · [health.md](docs/UML/health.md) · [orchestrator.md](docs/UML/orchestrator.md) · [challenges.md](docs/UML/challenges.md)

## Update Rule

**Any major design change MUST update the corresponding doc in [docs/](docs/) in the same change.**

Major = anything that alters: module boundaries, data models, network/topology behavior, game loop semantics, agent lifecycle, or external interfaces.

- If an existing doc covers the area, edit it.
- If no doc covers the area, add a new one under `docs/` and link it from this file.
- Update this index when adding or removing a doc.
