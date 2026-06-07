# Challenge Authoring SOP

Spec for packaging a vulnerable application into a CyberArena
challenge. Deliverable: one self-contained `challenges/<name>/`
directory. Reference implementation:
[`challenges/nautilus_ico/`](../challenges/nautilus_ico/) (DEF CON
Finals 2025 `ico`). All required interfaces are defined in
[`challenges/interface/`](../challenges/interface/).

> `challenges/notes/` is a framework smoke test, not a reference —
> it intentionally lacks a defense flow.

## 1. Required files

Every contributed challenge **must** provide all of the following:

```
challenges/<name>/
├── __init__.py                # exports the Challenge class
├── challenge.py               # composition root
├── functionality_test.py      # FunctionalityTest impl
├── flag_handler.py            # FlagHandler impl
├── restart_handler.py         # RestartHandler impl  (defense flow)
├── prompts.py                 # build_attack_prompt + build_defense_prompt
├── build.py                   # docker build of the image
└── image/                     # docker build context
    ├── Dockerfile
    ├── entrypoint.sh
    ├── restart.sh             # framework-invoked rebuild + respawn
    ├── checker.sh             # backs the CHECKER probe (recommended)
    └── service/               # source / binaries baked into the image
```

Naming:
- Directory name: snake_case (Python module), e.g. `nautilus_ico`
- `Challenge.name`: kebab-case, e.g. `"nautilus-ico"`
- Image tag: `cyberarena/chal-<name>:<version>`

## 2. Interface contracts

The challenge composes five components (`challenges/interface/`):

| Component | File | Required surface |
|---|---|---|
| `Challenge` | `challenge.py` | `name`, `vulbox`, `functionality_test`, `flag_handler`, `restart_handler`, `docs` (all required — `restart_handler` returning `None` is **not** acceptable for contributed challenges) |
| `VulboxImage` | inline in `challenge.py` | `reference` (`cyberarena/chal-<name>:<v>`), `exposed_ports`, `build_context` |
| `FunctionalityTest.run(target)` | `functionality_test.py` | Returns a `CheckResult` tree. Recommended 4 leaves: `process`, `tcp`, `protocol`, `checker`. CHECKER must fail when the service's real function is broken — not just when the port is closed. |
| `FlagHandler.plant/retrieve(target, ...)` | `flag_handler.py` | `plant` writes the flag and returns an opaque handle; `retrieve` reads it back via the same handle. Returns `None` on miss, never raises. |
| `RestartHandler.run(target)` | `restart_handler.py` | Invokes `/arena/restart.sh` via docker exec. Returns a `CheckResult` with three leaves: `compile`, `spawn`, `smoke`. |
| `build_attack_prompt` + `build_defense_prompt` | `prompts.py` | Both required. The defense prompt **must** explicitly state that submitting a flag during DEFENSE is rejected (`reason=WRONG_PHASE`). Vuln hotspots must be named to file / function level. |

## 3. Image requirements

`image/Dockerfile` must:

1. `FROM --platform=linux/amd64 ubuntu:24.04` (or compatible amd64 base).
2. Install all runtime dependencies — **no network access at container startup**.
3. Create the `arena_agent` user; disable `PermitRootLogin` in sshd_config.
4. Bake service source to `/opt/challenge_src/<name>/` (read-only template).
5. Place `/arena/restart.sh` (and optionally `/arena/checker.sh`),
   root-owned, mode 555.
6. For compiled services: pre-compile the initial binary at build time;
   leave the artifact under `/opt/challenge_src/<name>/`. Keep the
   build toolchain in the runtime image — `restart.sh` rebuilds in-container.
7. `EXPOSE 22` plus the service port(s).

`image/entrypoint.sh` must:

1. Generate sshd host keys on first start (`ssh-keygen -A`).
2. Materialize the writable workdir: `cp -r /opt/challenge_src/<name>/. /srv/challenge/<name>/`, chown to `arena_agent`.
3. Launch the service (or leave it to `restart.sh` for compiled services).
4. `exec /usr/sbin/sshd -D -e` as PID 1.

## 4. Defense flow contract

When the agent calls `mcp__cyberarena__restart_service`, the framework
runs your `restart_handler.run(target)`:

```
docker exec /arena/restart.sh
  → recompile from /srv/challenge/<name>/
  → kill old process, spawn new
  → return CheckResult{ compile, spawn, smoke }
```

The agent never invokes `restart.sh` directly. It can read and execute
`/arena/restart.sh` but cannot modify it; the entire "edit source →
restart service" pathway goes through this contract.

After restart, the framework re-runs `functionality_test`. CHECKER must
pass for the round to count.

## 5. Pipeline integration

1. Deliver your `challenges/<name>/` directory in a PR. **Do not modify
   any other framework file.**
2. The maintainers add 2–4 lines to
   [`challenges/registry.py`](../challenges/registry.py):
   ```python
   _CHALLENGE_FACTORIES["<name>"] = YourChallenge
   _IMAGE_BUILDERS["<name>"]      = _your_build_fn
   # Optional:
   _INITIAL_START["<name>"]       = <post-deploy hook>
   _HEALTH_INTERVAL_SECS["<name>"] = <seconds>
   ```
3. Reference it from `game.yaml` (or a new `game_<name>.yaml`) by setting
   `image:` on the team prod nodes to your tag, and adding a
   `challenges:` entry with `name: <kebab>`.
4. `python -m main setup -c config/game_<name>.yaml` builds the image
   via your `build.py`; `python -m main start -c config/game_<name>.yaml`
   runs a full match.

## 6. Acceptance checks (run before submitting)

**Build & startup**
- [ ] `docker build` succeeds and produces `cyberarena/chal-<name>:<version>`
- [ ] `docker run` brings the container up; the service is reachable
      within 5 seconds
- [ ] `id arena_agent` returns uid > 0; `sshd -t` exits 0

**Flag round-trip**
- [ ] `plant` followed by `retrieve` returns the exact string planted

**Defense flow**
- [ ] `/arena/restart.sh` exits 0; the new process is pgrep-able; the
      port accepts connections
- [ ] Deliberately commenting out a logic line: `restart.sh` still
      rebuilds (toolchain is complete)
- [ ] Deliberately injecting a syntax error: `restart.sh` exits non-zero
      (build errors propagate)
- [ ] CHECKER passes on a working service AND **fails on a broken
      service** (not merely on port closure)

**Framework hygiene**
- [ ] No internet required at container startup (no `apt-get` / `pip install` in the entrypoint)
- [ ] No absolute-path hardcodes outside the image's own fixed paths
- [ ] `from challenges.<name> import <Name>Challenge` imports without error
- [ ] `<Name>Challenge().restart_handler` is a `RestartHandler` instance, not `None`

## 7. Reference

| Topic | Source |
|---|---|
| Abstract interface (read these first) | [`challenges/interface/challenge.py`](../challenges/interface/challenge.py), [`challenges/interface/probe.py`](../challenges/interface/probe.py) |
| Canonical example | [`challenges/nautilus_ico/`](../challenges/nautilus_ico/) |
| Architecture overview | [`docs/UML/challenges.md`](UML/challenges.md) |
