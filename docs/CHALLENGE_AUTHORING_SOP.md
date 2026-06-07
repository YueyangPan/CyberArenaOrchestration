# Challenge Authoring SOP

Package a vulnerable application into a CyberArena challenge as one
self-contained `challenges/<snake_name>/` directory. Reference:
[`challenges/nautilus_ico/`](../challenges/nautilus_ico/) (DEF CON
Finals 2025 `ico`). Interface ABCs:
[`challenges/interface/`](../challenges/interface/).

## 1. Files

```
challenges/<snake_name>/
├── __init__.py                # must — exports the Challenge class
├── challenge.py               # must — composition root
├── prompts.py                 # must — attack + defense prompt builders
├── build.py                   # must — docker build of the image
├── functionality_test.py      # recommended — FunctionalityTest impl
├── flag_handler.py            # recommended — FlagHandler impl
├── restart_handler.py         # recommended — RestartHandler impl
└── image/                     # docker build context
    ├── Dockerfile             # must
    ├── entrypoint.sh          # must
    ├── restart.sh             # must — framework-invoked rebuild + respawn
    ├── checker.sh             # recommended — backs the CHECKER probe
    └── service/               # recommended — baked source / binaries
```

Recommended Python files can be inlined into `challenge.py` if your
challenge is small. The surfaces they implement are still required —
see §3.

## 2. Naming

| What | Style | Example | Used as |
|---|---|---|---|
| Directory | `snake_case` | `nautilus_ico` | Python import: `from challenges.nautilus_ico import IcoChallenge` |
| `Challenge.name` | `kebab-case` | `"nautilus-ico"` | Registry key, `game.yaml` `name:`, prompt-dispatcher key, event `service` field |
| `VulboxImage.reference` | free-form OCI ref | `cyberarena/chal-nautilus-ico:v1` | Value `Challenge.vulbox.reference` returns; matches `image:` on team prod nodes in `game.yaml` |

Python imports use snake; YAML, panel, and wire identifiers use
kebab. The image reference is your call — `ghcr.io/...`,
`localhost:5000/...`, anything Docker pulls. Don't bake registry
policy into the challenge code.

## 3. Interface contracts

`Challenge` is the composition root. A single instance carries
everything the framework needs to deploy and run your challenge; it
exposes the five components below as `@property` /
`@cached_property` attributes and the framework reads them. You don't
construct the components on the framework's behalf.

All five are mandatory. `restart_handler = None` is not acceptable —
defense is part of every round.

| Component | Lives in | Surface |
|---|---|---|
| `Challenge` | `challenge.py` | `name`, `vulbox`, `functionality_test`, `flag_handler`, `restart_handler`, `docs` |
| `VulboxImage` | inline in `challenge.py` | `reference: str`, `exposed_ports`, `build_context` |
| `FunctionalityTest.run(target)` | `functionality_test.py` *or* inline | Returns a `CheckResult` tree. At minimum one `checker` leaf that fails when the service is actually broken, not just when the port is closed. Transport-level leaves (`tcp`, `http`, `sql`) alongside it are helpful for the panel and the agent. `nautilus_ico` uses `process` + `tcp` + `protocol` + `checker`. The framework emits one event per leaf and uses `leaf.name` verbatim. |
| `FlagHandler` | `flag_handler.py` *or* inline | `plant(target, flag) → handle` writes the flag and returns an opaque handle. `retrieve(target, handle) → str \| None` reads it back. Returns `None` on miss; never raises. |
| `RestartHandler.run(target)` | `restart_handler.py` *or* inline | Rebuilds and restarts the service. Run things inside the container via `target.meta["exec_in_container"]`. Return a `CheckResult` tree; at least one leaf should fail when the rebuild produces a broken service. `nautilus_ico` uses `compile` + `spawn` + `smoke`. |
| `build_attack_prompt` + `build_defense_prompt` | `prompts.py` | Both required. The defense prompt must tell the agent that flag submissions during DEFENSE are rejected with `reason=WRONG_PHASE`. Name the vulnerable file and function if you know them. If you don't, give real attack-surface guidance — entry points, untrusted-input paths — instead of guessing. |

## 4. Image requirements

Four things the framework needs from your image:

- SSH for `arena_agent`. The defender uses it during DEFENSE to edit
  source. Root login disabled.
- A writable copy of your source. The agent edits the copy;
  `restart.sh` rebuilds from it. Set this up in the entrypoint before
  launching the service.
- A rebuild script your `RestartHandler` invokes. The reference puts
  it at `/arena/restart.sh`, root-owned.
- Self-contained startup. Nothing fetched after build — bake every
  runtime dep into the image. Compiled services should pre-compile
  the initial binary at build time but keep the toolchain so rebuilds
  work later.

Build for `linux/amd64`. Pick any in-image service slug you want
(`nautilus_ico` uses `ico`); it doesn't need to match your directory
name or `Challenge.name`.

The cleanest way to start: copy
[`challenges/nautilus_ico/image/`](../challenges/nautilus_ico/image/)
and adjust.

## 5. Defense flow contract

Agent calls `mcp__cyberarena__restart_service`. Framework runs your
`restart_handler.run(target)`:

```
restart_handler.run(target)
  → rebuild from /srv/challenge/<svc>/
  → kill old, spawn new
  → return CheckResult{ ... }
```

The agent should always go through MCP, never run `restart.sh`
directly. The script is reachable from the SSH session, but only the
MCP path emits restart events and re-probes health. A direct
invocation produces a round that records nothing.

After the restart, the framework re-runs `functionality_test`. CHECKER
has to pass for the round to count.

## 6. Pipeline integration

`<snake>` = directory name; `<kebab>` = `Challenge.name`.

1. Submit `challenges/<snake>/` as one PR. **Don't modify any other framework file.**
2. Maintainer adds to [`challenges/registry.py`](../challenges/registry.py):
   ```python
   _CHALLENGE_FACTORIES["<kebab>"] = YourChallenge       # required
   _IMAGE_BUILDERS["<kebab>"]      = _your_build_fn      # required: runs your build.py
   _INITIAL_START["<kebab>"]       = <post-deploy hook>  # optional: extra setup after start (e.g. wait for sshd)
   _HEALTH_INTERVAL_SECS["<kebab>"] = <seconds>          # optional: override the 10s poll default
   ```
3. In `game.yaml` (or `game_<kebab>.yaml`): set `image:` on team prod nodes to whatever `VulboxImage.reference` returns; add a `challenges:` entry with `name: <kebab>`.
4. `python -m main setup -c config/game_<kebab>.yaml`, then `start`.

## 7. Acceptance checks

**Image**

- [ ] `docker build` succeeds; tag matches `VulboxImage.reference`
- [ ] Container starts; service reachable within 5s; no network needed at startup
- [ ] `id arena_agent` ok; `sshd -t` exits 0

**Flags** — `plant` then `retrieve` returns the exact string.

**Defense**

- [ ] Baseline `restart_handler.run(target)` succeeds; CHECKER passes
- [ ] Inject a syntax error → rebuild fails, errors surface in `CheckResult`
- [ ] Break the service's real logic → CHECKER fails (not just port closure)

**Wiring**

- [ ] `from challenges.<snake> import <Name>Challenge` imports clean
- [ ] `<Name>Challenge().name` is the kebab string used in `game.yaml`
- [ ] `<Name>Challenge().restart_handler` is a `RestartHandler` (not `None`)

## 8. Reference

- Interface ABCs: [`challenges/interface/challenge.py`](../challenges/interface/challenge.py), [`challenges/interface/probe.py`](../challenges/interface/probe.py)
- Canonical example: [`challenges/nautilus_ico/`](../challenges/nautilus_ico/)
- Architecture: [`docs/UML/challenges.md`](UML/challenges.md)

> `challenges/notes/` is a framework smoke test, not a reference (no defense flow).
