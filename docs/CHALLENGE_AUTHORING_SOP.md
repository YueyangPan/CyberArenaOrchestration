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
| `RestartHandler.run(target)` | `restart_handler.py` *or* inline | Rebuilds + restarts the service. Use `target.meta["exec_in_container"]` to run commands inside the container. Returns a `CheckResult` tree with at least one leaf that fails when the rebuild produced a broken service. `nautilus_ico` uses `compile` + `spawn` + `smoke`; web / db might prefer `build` + `start` + `smoke`. |
| `build_attack_prompt` + `build_defense_prompt` | `prompts.py` | Both required. Defense prompt **must** state that submitting a flag during DEFENSE rejects with `reason=WRONG_PHASE`. If you know where the bug is, name file / function. If you don't (legacy / unaudited / discovery-style challenge), give real attack-surface guidance (entry points, untrusted-input boundaries) — don't fabricate. |

## 4. Image requirements

The framework needs four things from your image. Everything else is up
to you.

- **SSH access for `arena_agent`** (non-root). The defender agent SSHes
  in during DEFENSE to edit your source; root login is disabled.
- **A writable copy of your source.** The agent edits a copy, not the
  baked original — so the entrypoint should materialize a working
  directory owned by `arena_agent` before launching the service.
  `restart.sh` rebuilds from there.
- **A rebuild script the framework can invoke** through your
  `RestartHandler`. The reference puts it at `/arena/restart.sh`
  (root-owned, not agent-writable).
- **Self-contained startup.** No `apt-get` / `pip install` after build
  — everything must already be in the image. Compiled services
  should pre-compile their initial binary at build time but keep the
  toolchain so `restart.sh` can rebuild later.

Build for `linux/amd64` unless you have a reason otherwise (some
toolchains have no arm backend). Pick any service slug `<svc>` for the
in-image paths — `nautilus_ico` uses `ico`, and it's fine for it to
differ from your directory name and `Challenge.name`.

For the exact Dockerfile + entrypoint pattern that satisfies all of
the above, copy
[`challenges/nautilus_ico/image/`](../challenges/nautilus_ico/image/)
as your starting point.

## 5. Defense flow contract

When the agent calls `mcp__cyberarena__restart_service`, the framework
runs `restart_handler.run(target)`:

```
restart_handler.run(target)
  → rebuild from /srv/challenge/<svc>/
  → kill old, spawn new
  → return CheckResult{ ... }
```

**Defense prompts should call MCP `restart_service` and wait for the
event chain**, not run `restart.sh` directly. The script's permissions
don't physically block a direct call, but only the MCP path emits
restart events and re-probes health — bypass it and the round
records nothing.

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
