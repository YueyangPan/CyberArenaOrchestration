"""DEFCON: in-container user + permission model.

Pins the M7 revision of the M5.6 "root SSH" stance: with defense-mode +
MCP-mediated restart, the agent doesn't need root inside the challenge
container. SSH lands as `arena_agent` (non-root); /arena/* is read-only
to the agent via kernel-level Unix permissions (not prompt rules).

Why this is load-bearing: a prompt-injection attack inside the agent's
own context could try to overwrite /arena/restart.sh to bypass the
health chain. Filesystem permissions make that physically impossible.

Runs each assertion via `docker exec --user arena_agent` to simulate
the SSH identity without provisioning a key (orthogonal — that's
covered separately under M5.6 SSH provisioning tests).
"""

from __future__ import annotations

import subprocess

import pytest


def _exec(name: str, cmd: list[str], *, user: str | None = None) -> subprocess.CompletedProcess:
    args = ["docker", "exec"]
    if user:
        args += ["--user", user]
    args += [name, *cmd]
    return subprocess.run(args, capture_output=True, text=True)


# ── arena_agent identity ───────────────────────────────────────────────


def test_arena_agent_id_is_non_root(dcf_ico_container):
    r = _exec(dcf_ico_container, ["id"], user="arena_agent")
    assert r.returncode == 0
    # uid != 0 in the output.
    assert "uid=0" not in r.stdout, f"arena_agent must not be root: {r.stdout}"
    assert "arena_agent" in r.stdout


# ── /srv/challenge: agent's edit surface ───────────────────────────────


def test_arena_agent_can_write_under_srv_challenge(dcf_ico_container):
    """The whole defense workflow assumes the agent can edit the
    Pascal source — confirm by writing a sentinel."""
    name = dcf_ico_container
    r = _exec(
        name,
        ["bash", "-c", "echo '{ patched by test }' >> /srv/challenge/ico/ico.pas"],
        user="arena_agent",
    )
    assert r.returncode == 0, f"write to /srv/challenge denied: {r.stderr}"

    # Read-back verifies the write landed.
    r = _exec(name, ["tail", "-1", "/srv/challenge/ico/ico.pas"], user="arena_agent")
    assert r.returncode == 0
    assert "patched by test" in r.stdout


def test_arena_agent_can_create_new_file_under_srv_challenge(dcf_ico_container):
    """Agent should also be able to add new files (e.g. a helper unit),
    not just modify existing ones."""
    r = _exec(
        dcf_ico_container,
        ["bash", "-c", "echo body > /srv/challenge/ico/test_helper.pas"],
        user="arena_agent",
    )
    assert r.returncode == 0, r.stderr


# ── /arena: framework scripts, read-only to the agent ──────────────────


@pytest.mark.parametrize("path", ["/arena/restart.sh", "/arena/checker.sh"])
def test_arena_agent_cannot_modify_arena_scripts(dcf_ico_container, path):
    """Permission denied at the kernel layer — not a prompt rule.
    A prompt-injection in the agent's context that tries to disable the
    health chain by overwriting checker.sh MUST be physically blocked."""
    r = _exec(
        dcf_ico_container,
        ["bash", "-c", f"echo evil >> {path}"],
        user="arena_agent",
    )
    assert r.returncode != 0, f"agent could write to {path}!"
    assert "Permission denied" in (r.stderr or "")


@pytest.mark.parametrize("path", ["/arena/restart.sh", "/arena/checker.sh", "/arena/entrypoint.sh"])
def test_arena_agent_can_read_arena_scripts(dcf_ico_container, path):
    """Read is fine — the agent may want to introspect the restart flow
    (mode 555 grants read+exec to other)."""
    r = _exec(dcf_ico_container, ["head", "-1", path], user="arena_agent")
    assert r.returncode == 0, f"agent cannot read {path}: {r.stderr}"


def test_arena_agent_cannot_delete_arena_scripts(dcf_ico_container):
    """Defense-in-depth: even if /arena/restart.sh's mode-555 protects
    the file itself, the agent shouldn't be able to unlink it from
    a writable parent. /arena/ is root-owned 555 → agent can't unlink."""
    r = _exec(dcf_ico_container, ["rm", "/arena/restart.sh"], user="arena_agent")
    assert r.returncode != 0, "agent could remove /arena/restart.sh!"


# ── /opt/challenge_src: baked template, read-only to the agent ─────────


def test_arena_agent_cannot_modify_opt_challenge_src(dcf_ico_container):
    """The baked template is the clean baseline used at container start.
    Agent must not be able to corrupt it (otherwise next container restart
    would inherit the corruption)."""
    r = _exec(
        dcf_ico_container,
        ["bash", "-c", "echo evil >> /opt/challenge_src/ico/ico.pas"],
        user="arena_agent",
    )
    assert r.returncode != 0
    assert "Permission denied" in (r.stderr or "")


# ── Root: docker exec as root still works (orchestrator path) ──────────


def test_root_can_run_restart_sh_via_docker_exec(dcf_ico_container):
    """The orchestrator's MCP `restart_service` tool runs the restart
    script via `docker exec` as the container's default user (root).
    Confirm that path is unobstructed."""
    r = _exec(dcf_ico_container, ["/arena/restart.sh"])
    assert r.returncode == 0, f"root /arena/restart.sh failed: stderr={r.stderr}"
