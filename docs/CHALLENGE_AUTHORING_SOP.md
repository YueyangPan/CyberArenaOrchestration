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

The "recommended" Python files can be inlined into `challenge.py` for
small challenges; the surfaces they implement are still required
(§3).

## 2. Naming

| What | Style | Example | Used as |
|---|---|---|---|
| Directory | `snake_case` | `nautilus_ico` | Python import: `from challenges.nautilus_ico import IcoChallenge` |
| `Challenge.name` | `kebab-case` | `"nautilus-ico"` | Registry key, `game.yaml` `name:`, prompt-dispatcher key, event `service` field |
| `VulboxImage.reference` | free-form OCI ref | `cyberarena/chal-nautilus-ico:v1` | Value `Challenge.vulbox.reference` returns; matches `image:` on team prod nodes in `game.yaml` |

Python imports → snake. YAML / panel / wire → kebab. Image reference
is your call (`ghcr.io/...`, `localhost:5000/...`, etc.) — don't bake
registry policy into the challenge code.

## 3. Interface contracts

`Challenge` is the **composition root**: one instance is the only
handle the framework needs. The framework reads its `@property` /
`@cached_property` attributes; it never constructs the components.

All five components are mandatory — `restart_handler = None` is
**not** acceptable. Defense is part of the round.

| Component | Lives in | Surface |
|---|---|---|
| `Challenge` | `challenge.py` | `name`, `vulbox`, `functionality_test`, `flag_handler`, `restart_handler`, `docs` |
| `VulboxImage` | inline in `challenge.py` | `reference: str`, `exposed_ports`, `build_context` |
| `FunctionalityTest.run(target)` | `functionality_test.py` *or* inline | Returns a `CheckResult` tree. **Required:** one `checker` leaf that fails when the service's real function is broken (not merely when the port is closed). **Recommended:** at least one transport-level leaf alongside (`tcp` / `http` / `sql` / …). `nautilus_ico` uses `process` + `tcp` + `protocol` + `checker`; framework emits one event per leaf, `leaf.name` verbatim. |
| `FlagHandler` | `flag_handler.py` *or* inline | `plant(target, flag) → handle` writes the flag and returns an opaque handle. `retrieve(target, handle) → str \| None` reads it back (`None` on miss, never raises). |
| `RestartHandler.run(target)` | `restart_handler.py` *or* inline | Rebuilds + restarts the service via whatever mechanism `target` provides (`target.meta["exec_in_container"]` today; SSH / k8s exec / VM agent tomorrow). Returns a `CheckResult` tree with at least one leaf that fails when the rebuild produced a broken service. `nautilus_ico` uses `compile` + `spawn` + `smoke`; web / db might prefer `build` + `start` + `smoke`. |
| `build_attack_prompt` + `build_defense_prompt` | `prompts.py` | Both required. Defense prompt **must** state that submitting a flag during DEFENSE rejects with `reason=WRONG_PHASE`. If you know where the bug is, name file / function. If you don't (legacy / unaudited / discovery-style challenge), give real attack-surface guidance (entry points, untrusted-input boundaries) — don't fabricate. |

## 4. Image requirements

`<svc>` is the in-image service slug (`nautilus_ico` uses `ico`); free
to differ from `<snake>` and `<kebab>`.

**`image/Dockerfile`:**

1. `FROM --platform=linux/amd64 ubuntu:24.04` (or another amd64 base).
2. Install all runtime deps — **no network at container startup**.
3. Create `arena_agent` user; `PermitRootLogin no` in sshd_config.
4. Bake source to `/opt/challenge_src/<svc>/` (read-only template).
5. Place `/arena/restart.sh` (and optional `/arena/checker.sh`), root-owned mode 555.
6. Compiled services: pre-compile the initial binary at build time, but keep the toolchain in the runtime image — `restart.sh` rebuilds in-container.

Recommended: `EXPOSE 22` plus the service port(s).

**`image/entrypoint.sh`:**

1. `ssh-keygen -A` (sshd host keys on first start).
2. `cp -r /opt/challenge_src/<svc>/. /srv/challenge/<svc>/`, chown to `arena_agent`.
3. Launch the service (or leave it to `restart.sh` for compiled services).
4. `exec /usr/sbin/sshd -D -e` as PID 1.

## 5. Defense flow contract

When the agent calls `mcp__cyberarena__restart_service`, the framework
runs `restart_handler.run(target)`:

```
restart_handler.run(target)
  → rebuild from /srv/challenge/<svc>/
  → kill old, spawn new
  → return CheckResult{ ... }
```

**The supported restart path is MCP `restart_service`** — not bashing
`restart.sh` directly. The script is root-owned mode 555 (executable,
not modifiable), so the filesystem doesn't block a direct invocation
— but the `STARTED → COMPLETED|FAILED` event chain and the
post-restart health probe only fire on the MCP path. Defense prompts
should call MCP and wait for the chain.

After the restart, the framework re-runs `functionality_test`.
CHECKER must pass for the round to count.

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
