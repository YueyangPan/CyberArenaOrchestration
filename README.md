# Cyber Arena Orchestration

Orchestrator for a 2-team Attack/Defense CTF arena. See [docs/design_v1.md](docs/design_v1.md) for the system design and [docs/m1_plan.md](docs/m1_plan.md) for the current milestone scope.

## Prerequisites

| Requirement | macOS | Ubuntu |
|-------------|-------|--------|
| Docker | Docker Desktop or OrbStack (running) | Docker Engine 20.10+ (`sudo apt install docker.io`) |
| Docker permission | — | Add your user to the `docker` group: `sudo usermod -aG docker $USER`, then log out / back in |
| Python | 3.11+ (`brew install python@3.12`) | 3.11+ (Ubuntu 24.04 ships 3.12; on 22.04 use `apt install python3.11` from deadsnakes PPA) |

Verify both backends:

```bash
docker version --format '{{.Server.Version}}'
python3 --version
```

## Setup

```bash
# from repo root
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

./scripts/build_images.sh        # builds cyberarena/dev-base + cyberarena/prod-base (~5 min first time)
```

## Run

```bash
source .venv/bin/activate

python3 main.py setup            # creates 3 networks + 4 containers
docker ps                        # should show team1_dev / team1_prod / team2_dev / team2_prod, all Up

python3 main.py teardown         # full cleanup
```

## Tests

```bash
pytest tests/test_m1.py -v
```

The integration test auto-skips if the docker daemon is unreachable.

## Common gotchas

- **Subnet conflict (Ubuntu more than macOS).** `config/game.yaml` hard-codes `10.0.0.0/24`, `10.1.0.0/24`, `10.2.0.0/24`. If a corporate VPN, tailscale, or k8s already owns one of these, `docker network create` fails. Edit `networks:` in `game.yaml` to free ranges.
- **"permission denied" on `/var/run/docker.sock` (Ubuntu).** You are not in the `docker` group, or you joined it but have not logged out / back in.
- **Stale containers from a previous run.** `main.py setup` is idempotent on the container name (removes existing then recreates), but if `setup` crashed mid-way you may need to manually `docker rm -f team1_dev team1_prod team2_dev team2_prod` and `docker network rm team1_net team2_net game_net`.

## Repo layout

```
CyberArenaOrchestration/
├── config/
│   ├── game.yaml                 # match config (teams + nodes + networks + install_list)
│   └── docker/
│       ├── Dockerfile.dev        # cyberarena/dev-base (agent runtime + attack/debug tools)
│       └── Dockerfile.prod       # cyberarena/prod-base (minimal ubuntu + python3 + sshd)
├── models/                       # dataclasses (TopologyGraph, Node, Network, Team)
├── orchestrator/
│   ├── orchestrator.py           # setup() / teardown() lifecycle
│   ├── topology.py               # YAML loader
│   ├── service.py                # Service ABC (concrete impls in M2)
│   └── infra/
│       ├── docker.py             # container lifecycle
│       └── network.py            # network lifecycle + set_cross_team_network stub (M3)
├── scripts/build_images.sh
├── tests/test_m1.py              # full setup -> verify -> teardown integration test
├── main.py                       # CLI entry point
├── requirements.txt
└── docs/
    ├── design_v1.md              # system design
    ├── m1_plan.md                # milestone 1 plan
    └── m1_report.md              # milestone 1 execution report
```