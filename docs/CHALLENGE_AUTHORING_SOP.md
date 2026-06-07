# Challenge Authoring SOP

Spec for packaging a vulnerable application into a CyberArena
challenge. Deliverable: one self-contained `challenges/<snake_name>/`
directory. Reference implementation:
[`challenges/nautilus_ico/`](../challenges/nautilus_ico/) (DEF CON
Finals 2025 `ico`). All required interfaces are defined in
[`challenges/interface/`](../challenges/interface/).

> `challenges/notes/` is a framework smoke test, not a reference —
> it intentionally lacks a defense flow.

## 1. Required files

```
challenges/<snake_name>/
├── __init__.py                # must — exports the Challenge class
├── challenge.py               # must — composition root
├── prompts.py                 # must — build_attack_prompt + build_defense_prompt
├── build.py                   # must — docker build of the image
├── functionality_test.py      # recommended — FunctionalityTest impl
├── flag_handler.py            # recommended — FlagHandler impl
├── restart_handler.py         # recommended — RestartHandler impl
└── image/                     # docker build context
    ├── Dockerfile             # must
    ├── entrypoint.sh          # must — runs at container start
    ├── restart.sh             # must — framework-invoked rebuild + respawn
    ├── checker.sh             # recommended — backs the CHECKER probe
    └── service/               # recommended — source / binaries baked into the image
```

**Must** files are the ones the framework loads or invokes directly.
**Recommended** files exist for organizational clarity: their content
*must* be implemented somewhere (Challenge has to expose
`functionality_test`, `flag_handler`, `restart_handler` — see §3), but
small challenges can inline the classes into `challenge.py` instead of
giving each its own file. The reference (`nautilus_ico`) splits them.

## 2. Naming

Two distinct names. They are **not** interchangeable.

| What | Style | Example | Used as |
|---|---|---|---|
| Directory name | `snake_case` | `nautilus_ico` | Python package path — `from challenges.nautilus_ico import IcoChallenge`. |
| `Challenge.name` (the `@property`) | `kebab-case` | `"nautilus-ico"` | Registry key in `challenges/registry.py`, `name:` field under `challenges:` in `game.yaml`, prompt-dispatcher key in `agents/prompts/__init__.py::_PROMPT_MODULES`, event payload `service` field. |
| Image reference | author's choice (free-form OCI reference) | `cyberarena/chal-nautilus-ico:v1` | `image:` field on team prod nodes in `game.yaml`; the value `Challenge.vulbox.reference` returns. |

Rule of thumb: anything Python imports uses snake_case; anything the
**operator** types into a YAML, sees in the panel, or matches by string
uses kebab-case.

The image reference is **not** required to follow the
`cyberarena/chal-<kebab>:<v>` shape — that's just the convention the
bundled reference challenges use. Anything Docker would accept as a
pull spec works (e.g. `ghcr.io/2077ai/challenges/nautilus-ico:v1`,
`localhost:5000/chal-ico:v1`, …). Keep registry policy out of the
challenge code.

## 3. Interface contracts

`Challenge` is a **composition root**: a single instance is the only
handle the framework needs to deploy + run your challenge. It composes
five components (defined in
[`challenges/interface/`](../challenges/interface/)) and exposes them
as `@property` / `@cached_property` attributes. The framework reads
those properties; it never constructs the components on its own.

All five components are mandatory — `restart_handler` returning `None`
is **not** acceptable for contributed challenges (defense is a
first-class part of the round).

| Component | Lives in | Required surface |
|---|---|---|
| `Challenge` | `challenge.py` | `name`, `vulbox`, `functionality_test`, `flag_handler`, `restart_handler`, `docs` |
| `VulboxImage` | inline in `challenge.py` | `reference: str` (free-form OCI image reference, e.g. `cyberarena/chal-nautilus-ico:v1`), `exposed_ports`, `build_context` |
| `FunctionalityTest.run(target)` | `functionality_test.py` *or* inline in `challenge.py` | Returns a `CheckResult` tree. Recommended 4 leaves: `process`, `tcp`, `protocol`, `checker`. CHECKER must fail when the service's real function is broken — not merely when the port is closed. |
| `FlagHandler.plant/retrieve(target, ...)` | `flag_handler.py` *or* inline | `plant` writes the flag and returns an opaque handle; `retrieve` reads it back via the same handle. Returns `None` on miss, never raises. |
| `RestartHandler.run(target)` | `restart_handler.py` *or* inline | Rebuilds and restarts the service in-place after agent edits. Implementation is challenge-defined and uses whatever execution mechanism `target` provides (today: `target.meta["exec_in_container"]` — docker exec; tomorrow could be SSH / k8s exec / VM agent). Returns a `CheckResult` with three leaves: `compile`, `spawn`, `smoke`. The reference `nautilus_ico/restart_handler.py` invokes `/arena/restart.sh` through that callable. |
| `build_attack_prompt` + `build_defense_prompt` | `prompts.py` | Both required. The defense prompt **must** explicitly state that submitting a flag during DEFENSE is rejected (`reason=WRONG_PHASE`). Vuln hotspots must be named to file / function level. |

## 4. Image requirements

Below, `<svc>` is the author-chosen service slug used **only** inside
the image (the reference uses `ico`). It does not need to match
`<snake>` or `<kebab>`.

`image/Dockerfile` must:

1. `FROM --platform=linux/amd64 ubuntu:24.04` (or compatible amd64 base).
2. Install all runtime dependencies — **no network access at container startup**.
3. Create the `arena_agent` user; disable `PermitRootLogin` in sshd_config.
4. Bake service source to `/opt/challenge_src/<svc>/` (read-only template).
5. Place `/arena/restart.sh` (and optionally `/arena/checker.sh`),
   root-owned, mode 555.
6. For compiled services: pre-compile the initial binary at build time;
   leave the artifact under `/opt/challenge_src/<svc>/`. Keep the
   build toolchain in the runtime image — `restart.sh` rebuilds in-container.
7. `EXPOSE 22` plus the service port(s).

`image/entrypoint.sh` must:

1. Generate sshd host keys on first start (`ssh-keygen -A`).
2. Materialize the writable workdir: `cp -r /opt/challenge_src/<svc>/. /srv/challenge/<svc>/`, chown to `arena_agent`.
3. Launch the service (or leave it to `restart.sh` for compiled services).
4. `exec /usr/sbin/sshd -D -e` as PID 1.

## 5. Defense flow contract

When the agent calls `mcp__cyberarena__restart_service`, the framework
runs your `restart_handler.run(target)`:

```
restart_handler.run(target)
  → recompile from /srv/challenge/<svc>/
  → kill old process, spawn new
  → return CheckResult{ compile, spawn, smoke }
```

The handler decides *how* to execute (`target.meta["exec_in_container"]`
today). The reference implementation invokes `/arena/restart.sh` through
that callable; other transports can plug in without changing the
contract.

The agent never invokes `restart.sh` directly. It can read and execute
`/arena/restart.sh` but cannot modify it; the entire "edit source →
restart service" pathway goes through this contract.

After restart, the framework re-runs `functionality_test`. CHECKER must
pass for the round to count.

## 6. Pipeline integration

In the steps below `<snake>` is your directory name and `<kebab>` is
`Challenge.name` — see §2.

1. Deliver your `challenges/<snake>/` directory in a PR. **Do not modify
   any other framework file.**
2. The maintainers add 2–4 lines to
   [`challenges/registry.py`](../challenges/registry.py) (all keyed by
   `<kebab>`, matching `Challenge.name`):
   ```python
   _CHALLENGE_FACTORIES["<kebab>"] = YourChallenge
   _IMAGE_BUILDERS["<kebab>"]      = _your_build_fn
   # Optional:
   _INITIAL_START["<kebab>"]       = <post-deploy hook>
   _HEALTH_INTERVAL_SECS["<kebab>"] = <seconds>
   ```
3. Reference it from `game.yaml` (or a new `game_<kebab>.yaml`) by
   setting `image:` on the team prod nodes to the same string your
   `VulboxImage.reference` returns, and adding a `challenges:` entry
   with `name: <kebab>`.
4. `python -m main setup -c config/game_<kebab>.yaml` builds the image
   via your `build.py`; `python -m main start -c config/game_<kebab>.yaml`
   runs a full match.

## 7. Acceptance checks (run before submitting)

**Build & startup**
- [ ] `docker build` succeeds and produces the reference `VulboxImage.reference` returns
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
- [ ] `from challenges.<snake> import <Name>Challenge` imports without error
- [ ] `<Name>Challenge().name` returns the kebab-case string used in `game.yaml`
- [ ] `<Name>Challenge().restart_handler` is a `RestartHandler` instance, not `None`

## 8. Reference

| Topic | Source |
|---|---|
| Abstract interface (read these first) | [`challenges/interface/challenge.py`](../challenges/interface/challenge.py), [`challenges/interface/probe.py`](../challenges/interface/probe.py) |
| Canonical example | [`challenges/nautilus_ico/`](../challenges/nautilus_ico/) |
| Architecture overview | [`docs/UML/challenges.md`](UML/challenges.md) |
