# CLAUDE.md

## Design Sources

All design lives in [docs/](docs/). Read the relevant doc before changing related code.

- [docs/design_v1.md](docs/design_v1.md) — overall system design
- [docs/m1_topology_plan.md](docs/m1_topology_plan.md) / [docs/m1_topology_report.md](docs/m1_topology_report.md) — M1: topology
- [docs/m2_gameloop_plan.md](docs/m2_gameloop_plan.md) / [docs/m2_gameloop_report.md](docs/m2_gameloop_report.md) — M2: game loop
- [docs/m3_network_plan.md](docs/m3_network_plan.md) / [docs/m3_network_report.md](docs/m3_network_report.md) — M3: network switching
- [docs/m4_agent_plan.md](docs/m4_agent_plan.md) / [docs/m4_agent_report.md](docs/m4_agent_report.md) — M4: agent install & launch

## Update Rule

**Any major design change MUST update the corresponding doc in [docs/](docs/) in the same change.**

Major = anything that alters: module boundaries, data models, network/topology behavior, game loop semantics, agent lifecycle, or external interfaces.

- If an existing doc covers the area, edit it.
- If no doc covers the area, add a new one under `docs/` and link it from this file.
- Update this index when adding or removing a doc.
