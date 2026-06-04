# Orchestrator layer — UML

Container + network lifecycle for the arena. Owns Docker. Knows
nothing about agents or game state — those build on top.

## Class diagram

```mermaid
classDiagram
  class Orchestrator {
    +graph: TopologyGraph
    +containers: ContainerManager
    +network: NetworkManager
    +setup() None
    +teardown() None
    -_run_install_list(node) None
    -_preflight() None
  }

  class ContainerManager {
    -_client: docker.DockerClient
    +create_and_start(node) str
    +remove(name) None
    +exec(name, cmd) tuple~int, str~
    +is_running(name) bool
  }

  class NetworkManager {
    -_client: docker.DockerClient
    -_graph: TopologyGraph
    +create(network) str
    +remove(name) None
    +set_cross_team_network(allow) None
  }

  class ContainerHandle {
    +container_id: str
    +name: str
    +networks: list~str~
  }

  class TopologyGraph {
    +teams: list~Team~
    +nodes: list~Node~
    +networks: list~Network~
    +node(id) Node
  }

  class Team {
    +id: str
    +dev: str
    +prod: str
    +token: str
    +agent: AgentConfig
  }

  class Node {
    +id: str
    +image: str
    +networks: list~str~
    +ports: list~str~
    +install_list: list~str~
    +env: dict
    +extra_hosts: dict
  }

  class Network {
    +name: str
    +subnet: str
  }

  Orchestrator "1" *-- "1" ContainerManager
  Orchestrator "1" *-- "1" NetworkManager
  Orchestrator "1" o-- "1" TopologyGraph
  TopologyGraph "1" *-- "*" Team
  TopologyGraph "1" *-- "*" Node
  TopologyGraph "1" *-- "*" Network
  ContainerManager ..> Node : creates from
  NetworkManager ..> Network : creates from
  ContainerHandle ..> Node : refers to by name
```

## Network state per phase

The arena has three classes of network:

| Network | Always connected | Phase gate |
|---|---|---|
| `team{N}_net` | yes (intra-team) | always — `team{N}_dev` ↔ `team{N}_prod` |
| `game_net` | only in ATTACK | `set_cross_team_network(allow=True)` attaches all prods |
| `attack_net_team{N}` | only in ATTACK | per-team attacker net (opponent's dev ↔ this team's prod) |

`set_cross_team_network(allow)` flips both `game_net` and the
`attack_net_*` family in lockstep — DEFENSE = all detached, ATTACK
= all attached. Detach is implemented by `docker network disconnect`
and re-attach by `docker network connect`.

## Setup / teardown sequence

```mermaid
sequenceDiagram
  autonumber
  participant Main as main.py
  participant Orch as Orchestrator
  participant Nm as NetworkManager
  participant Cm as ContainerManager
  participant D as Docker daemon

  Note over Main: setup()
  Main->>Orch: setup()
  loop per network
    Orch->>Nm: create(net)
    Nm->>D: docker network create
  end
  loop per node
    Orch->>Cm: create_and_start(node)
    Cm->>D: pull · create · start
    Cm->>D: connect extra networks (beyond default)
  end
  loop per node with install_list
    Orch->>Cm: exec(node.id, "apt-get install …")
  end
  Orch->>Orch: _preflight() (stub)

  Note over Main: teardown()
  Main->>Orch: teardown()
  loop per node
    Orch->>Cm: remove(node.id)
    Cm->>D: stop · rm
  end
  loop per network
    Orch->>Nm: remove(net.name)
    Nm->>D: docker network rm
  end
```

## ContainerHandle — opaque pointer to a running container

```mermaid
classDiagram
  class ContainerHandle {
    <<frozen>>
    +container_id: str
    +name: str
    +networks: list~str~
  }
```

The agents layer, transport backends, and the flag/health facilities
pass `ContainerHandle` to identify which container a command targets.
Decoupled from the legacy `Service` ABC (deleted with the challenge
interface v2 migration); per-challenge install/start/restart lifecycle
now lives in the `Challenge` ABC's `flag_handler` / `restart_handler`
and the `challenges.registry.initial_start` hook. See
[challenges.md](challenges.md).

## File map

| Path | Role |
|---|---|
| `orchestrator/orchestrator.py` | `Orchestrator` (setup / teardown) |
| `orchestrator/infra/docker.py` | `ContainerManager` |
| `orchestrator/infra/network.py` | `NetworkManager` |
| `orchestrator/handle.py` | `ContainerHandle` dataclass |
| `orchestrator/topology.py` | YAML → `TopologyGraph` |
| `models/topology.py` | `Team` / `Node` / `Network` / `TopologyGraph` dataclasses |
