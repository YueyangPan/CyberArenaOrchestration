# Cyber Arena Orchestration

Orchestrator for a 2-team Attack/Defense CTF arena. Spins up Docker
networks + per-team containers, plants flags, runs an ATTACK/DEFENSE
round loop, and drives LLM agents (Claude Code today) for both sides
through an MCP channel.

System design: [docs/design_v1.7.md](docs/design_v1.7.md). Writing a
new challenge: [docs/CHALLENGE_AUTHORING_SOP.md](docs/CHALLENGE_AUTHORING_SOP.md).

## Prerequisites

| Requirement | macOS | Ubuntu |
|---|---|---|
| Docker | Docker Desktop / OrbStack (running) | Docker Engine 20.10+ |
| Docker permissions | — | `sudo usermod -aG docker $USER`, then re-login |
| Python | conda / miniconda (any recent) | conda / miniconda (any recent) |

```bash
docker version --format '{{.Server.Version}}'
conda --version
```

## Setup

```bash
conda create -n cyberarena python=3.11 -y
conda activate cyberarena
pip install -r requirements.txt

./scripts/build_images.sh    # cyberarena/dev-base (agent runtime)
# Per-challenge images (cyberarena/chal-notes, cyberarena/chal-nautilus-ico, …)
# are built on demand by `main.py setup` / `start`.
```

## Run

```bash
conda activate cyberarena

# Bring up networks + containers from the config:
python main.py setup -c config/game_ico.yaml

# Run a full match (round loop + agents + panel server on :8000):
python main.py start -c config/game_ico.yaml
# → live panel at http://localhost:8000/panel

# Tear everything down:
python main.py teardown -c config/game_ico.yaml
```

Two ready-made configs:

- [`config/game.yaml`](config/game.yaml) — `notes` challenge, framework smoke test
- [`config/game_ico.yaml`](config/game_ico.yaml) — `nautilus-ico` (DEF CON Finals 2025), full attack + defense flow

## Tests

```bash
pytest                       # full suite (~90s; auto-skips live tests when docker is down)
pytest tests/test_m1_topology.py -v   # one of the live integration tests
```

## Common gotchas

- **Subnet conflict (Ubuntu more than macOS).** `config/*.yaml` hard-codes `10.0.0.0/24`, `10.1.0.0/24`, `10.2.0.0/24`. If a corporate VPN, tailscale, or k8s already claims one, edit `networks:` to free ranges.
- **`permission denied` on `/var/run/docker.sock`.** You are not in the `docker` group, or you joined it but have not logged out.
- **Stale containers after a crash.** `setup` is idempotent on container names, but if it died mid-way: `docker rm -f team1_dev team1_prod team2_dev team2_prod && docker network rm team1_net team2_net game_net`.
- **API keys.** `start` needs `ANTHROPIC_API_KEY_TEAM1` / `_TEAM2` (and matching `_BASE_URL` if you proxy) in `.env`. Without them the round loop runs but the LLM phases are no-ops.

## Repo layout

```
CyberArenaOrchestration/
├── main.py                    # CLI: setup / start / teardown
├── config/                    # game.yaml + per-challenge match configs
├── challenges/                # pluggable challenges; see CHALLENGE_AUTHORING_SOP
│   ├── interface/             # ABCs: Challenge / VulboxImage / FunctionalityTest / FlagHandler / RestartHandler
│   ├── notes/                 # framework smoke-test challenge
│   ├── nautilus_ico/          # DEF CON Finals 2025 `ico` — reference implementation
│   └── registry.py            # slug → Challenge factory (maintainers add 2-4 lines per new challenge)
├── orchestrator/              # docker networks + containers + topology
├── gamemgr/                   # round loop, timeline (events), MCP server, REST API, panel
├── agents/                    # pluggable agent CLIs (Claude Code today)
├── flag/                      # FlagServer (plant + rotate + submit)
├── health/                    # per-round functionality probes
├── panel/                     # live event-stream UI (swim-lane SVG)
├── models/                    # dataclasses (topology, agent config, …)
├── scripts/                   # build_images.sh, screenshot demo, …
├── tests/                     # ~210 tests, mix of unit + live integration
└── docs/                      # design + UML + per-milestone reports + SOP
```
