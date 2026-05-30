"""ClaudeAgentManager — host-side controller for one Claude agent per team.

Lifecycle (persistent-memory v1, see
docs/persistent_memory_agent_plan_v1.md):

  install(handle, cfg)              # once per team at game start:
                                    #   npm install, SSH pivot, env, mcp.json
                                    #   No process is launched.
  run_phase(handle, prompt,         # per phase:
            round_id, phase,        #   write prompt, run claude -p --continue
            timeout)                #   via the AgentTransport, collect
                                    #   stdout/stderr/exit/timed_out, write
                                    #   per-phase artifacts.

All side effects on the agent host go through `AgentTransport`
(DockerExecBackend today, SSHBackend tomorrow). The manager never
calls `docker exec` directly.

Memory across phases carries via Claude CLI's `--continue` flag: the
CLI's session store lives at /root/.claude/projects/<workdir-cwd>/
inside the container; `--continue` reloads the most-recent session.
We pin the cwd to STATE_DIR/workdir so different arenas' sessions
stay isolated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import docker

from models.agent import AgentConfig, AgentLogs, AgentState, AgentStatus, DoneInfo
from orchestrator.service import ContainerHandle

from .manager import AgentManager
from .transport import AgentHandle, AgentTransport, CommandResult
from .transport_docker import DockerExecBackend

log = logging.getLogger(__name__)

# Layout inside the agent host. Paths kept under /var/lib + /var/run +
# /var/log to match the M4-era convention; the WORKDIR is new for
# `--continue` session isolation.
STATE_DIR = "/var/lib/cyberarena"
LOG_DIR = "/var/log/cyberarena"
RUN_DIR = "/var/run/cyberarena"
WORKDIR = f"{STATE_DIR}/workdir"

PROMPT_PATH = f"{STATE_DIR}/prompt.txt"
PHASE_CONTEXT_PATH = f"{STATE_DIR}/phase_context.json"
ENV_PATH = f"{STATE_DIR}/agent.env"
MCP_CONFIG_PATH = f"{STATE_DIR}/mcp.json"

# Legacy paths from the M4 / M4.5 model — kept so fetch_logs / status
# remain meaningful for any code path that still reads them, even
# though the persistent-memory v1 model doesn't write them. New code
# should look at the per-phase stream-{phase}.jsonl / agent-{phase}.log
# artifacts in the host's run directory instead.
PID_PATH = f"{RUN_DIR}/agent.pid"
WRAPPER_PID_PATH = f"{RUN_DIR}/agent.wrapper.pid"
EXIT_CODE_PATH = f"{RUN_DIR}/agent.exit_code"
STDOUT_PATH = f"{LOG_DIR}/agent.stdout"
STDERR_PATH = f"{LOG_DIR}/agent.stderr"

# install() callbacks. on_installed fires after all setup is staged
# (env, mcp.json, ssh pivot, workdir). on_launched is preserved for
# M4.5 back-compat; it fires alongside on_installed in this design
# (no separate launch step exists anymore). wrapper_pid passes 0.
OnInstalled = Callable[[str], None]
OnLaunched = Callable[[str, int], None]


class ClaudeAgentManager(AgentManager):
    """Host-side controller. One instance handles every team; each
    method takes a ContainerHandle for the team's dev container.

    The transport is injected (DockerExecBackend by default). Tests
    can pass a FakeTransport that records calls in-memory.
    """

    def __init__(
        self,
        *,
        transport: AgentTransport | None = None,
        client: docker.DockerClient | None = None,
    ) -> None:
        # Back-compat: existing tests construct `ClaudeAgentManager(client=mock)`.
        # If only client is given, synthesize a DockerExecBackend from it.
        if transport is not None:
            self._transport = transport
            self._client = client  # may be None; some legacy methods need it
        else:
            self._client = client or docker.from_env()
            self._transport = DockerExecBackend(client=self._client)

    # ── public API ──────────────────────────────────────────────────────

    def install(
        self,
        container: ContainerHandle,
        cfg: AgentConfig,
        *,
        on_installed: OnInstalled | None = None,
        on_launched: OnLaunched | None = None,
    ) -> None:
        """One-shot setup per team. Idempotent (npm cache hit, SSH key
        re-push is fine, etc.) but designed to be called exactly once
        per game by the orchestrator.

        Steps:
          1. Make STATE_DIR / LOG_DIR / RUN_DIR / WORKDIR.
          2. npm install -g @anthropic-ai/claude-code (if cli starts with "claude").
          3. SSH pivot if cfg.own_prod_container_name set (M5.6 mode).
          4. Write agent.env (no PROMPT_PATH here — prompts are per-phase).
          5. Write mcp.json if cfg.mcp_endpoint set.
          6. Fire on_installed and on_launched callbacks (both fire here
             in the new model; on_launched is preserved for M4.5 back-
             compat and gets wrapper_pid=0).

        NO process is launched. The first `run_phase` invocation is
        what actually starts Claude.
        """
        # All dirs the per-phase flow expects to exist.
        self._exec_sync(container, ["mkdir", "-p", STATE_DIR, LOG_DIR, RUN_DIR, WORKDIR], check=True)

        # Clear stale state from any prior run (legacy paths) so status()
        # doesn't report a ghost agent from before re-install.
        self._exec_sync(
            container,
            ["sh", "-c", f"rm -f {PID_PATH} {WRAPPER_PID_PATH} {EXIT_CODE_PATH}"],
            check=True,
        )

        if cfg.cli.startswith("claude"):
            self._exec_sync(
                container,
                ["npm", "install", "-g", "@anthropic-ai/claude-code"],
                check=True,
            )

        if cfg.own_prod_container_name:
            self._provision_ssh_pivot(container, cfg)

        self._transport.write_file(container, ENV_PATH, self._build_env_file(cfg).encode())

        if cfg.mcp_endpoint:
            self._write_mcp_config(container, cfg)

        log.info("agent installed on %s (cli=%r)", container.name, cfg.cli)

        # Both lifecycle callbacks fire here. There's no separate
        # "launched" step anymore — `run_phase` is what starts Claude
        # per-phase, and that fires its own AGENT_PROMPT_SENT /
        # COMPLETED / TIMEOUT / FAILED events.
        if on_installed is not None and cfg.team_id:
            on_installed(cfg.team_id)
        if on_launched is not None and cfg.team_id:
            on_launched(cfg.team_id, 0)

    async def run_phase(
        self,
        container: ContainerHandle,
        prompt: str,
        *,
        round_id: int,
        phase: str,
        timeout: float,
        artifact_dir: Path | None = None,
        model: str | None = None,
    ) -> DoneInfo:
        """Run one phase: drop the prompt, invoke `claude -p --continue`
        through the transport, capture artifacts, return DoneInfo.

        `timeout` is the phase timer in seconds. On expiry the transport
        terminates the subprocess (SIGTERM → SIGKILL) and we issue a
        belt-and-suspenders `pkill -KILL -f 'claude -p'` inside the
        container in case docker-exec signal propagation lost the signal.

        `artifact_dir`, if provided, gets `stream-{phase}.jsonl`,
        `agent-{phase}.log`, and `metadata-{phase}.json` written into it.
        If None, only the DoneInfo return value is populated and the
        caller handles persistence.

        `model` overrides `$CLAUDE_MODEL` (sourced from agent.env in the
        container) for this invocation. Useful for tests; production
        flows leave it None.
        """
        ctx = json.dumps({"round_id": round_id, "phase": phase})
        self._transport.write_file(container, PROMPT_PATH, prompt.encode())
        self._transport.write_file(container, PHASE_CONTEXT_PATH, ctx.encode())

        model_arg = (
            f"--model {shlex.quote(model)} "
            if model
            else '--model "$CLAUDE_MODEL" '
        )

        # The whole claude invocation is wrapped in a shell so we can:
        #   - cd into WORKDIR (claude --continue picks the cwd-scoped session)
        #   - source agent.env to get ANTHROPIC_API_KEY / BASE_URL / CLAUDE_MODEL
        #   - redirect stdin from PROMPT_PATH (Claude reads the prompt from stdin
        #     in -p mode)
        # The transport handles the timeout escalation; we just await.
        started_at = time.time()
        result: CommandResult = await self._transport.run_command(
            container,
            [
                "bash", "-c",
                f"cd {shlex.quote(WORKDIR)} && "
                f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
                "claude -p --continue "
                "--output-format stream-json --verbose "
                "--dangerously-skip-permissions "
                + model_arg
                + f"--mcp-config {shlex.quote(MCP_CONFIG_PATH)} "
                f"< {shlex.quote(PROMPT_PATH)}"
            ],
            timeout=timeout,
        )
        ended_at = time.time()
        duration_ms = int((ended_at - started_at) * 1000)

        if result.timed_out:
            # docker exec may not propagate SIGTERM cleanly to the
            # in-container claude child; pkill as a safety net.
            self._transport.kill_process(container, "claude -p")

        # Per-phase artifact files. Path naming matches the existing
        # DEFCON artifact store layout from
        # docs/DEFCON_challenge_framework_report_v1.md.
        stdout_path = ""
        stderr_path = ""
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            stream_p = artifact_dir / f"stream-{phase}.jsonl"
            agent_p = artifact_dir / f"agent-{phase}.log"
            meta_p = artifact_dir / f"metadata-{phase}.json"
            stream_p.write_bytes(result.stdout)
            agent_p.write_bytes(result.stderr)
            meta_p.write_text(json.dumps({
                "round_id": round_id,
                "phase": phase,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_ms": duration_ms,
                "prompt_first_line": (prompt.splitlines()[0] if prompt else ""),
            }))
            stdout_path = str(stream_p)
            stderr_path = str(agent_p)

        return DoneInfo(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    def status(self, container: ContainerHandle) -> AgentState:
        """Legacy M4/M4.5 status read against the supervisor pid files.
        These files are no longer written by the persistent-memory v1
        install (no supervisor exists), so `status()` will normally
        return NOT_INSTALLED. Kept for M4 test back-compat.
        """
        wrapper_pid = self._read_int_legacy(container, WRAPPER_PID_PATH)
        pid = self._read_int_legacy(container, PID_PATH)
        exit_code = self._read_int_legacy(container, EXIT_CODE_PATH)

        if pid is None:
            return AgentState(status=AgentStatus.NOT_INSTALLED)

        if exit_code is not None:
            if exit_code == 0:
                status = AgentStatus.COMPLETED
            elif exit_code in (130, 143):
                status = AgentStatus.TERMINATED
            else:
                status = AgentStatus.CRASHED
            return AgentState(status=status, pid=pid, wrapper_pid=wrapper_pid, exit_code=exit_code)

        rc, _ = self._exec_sync(container, ["kill", "-0", str(pid)])
        if rc == 0:
            return AgentState(status=AgentStatus.RUNNING, pid=pid, wrapper_pid=wrapper_pid)
        return AgentState(status=AgentStatus.CRASHED, pid=pid, wrapper_pid=wrapper_pid)

    def kill(self, container: ContainerHandle) -> None:
        """Legacy kill — sends SIGTERM to any process matching `claude -p`
        inside the container. In the v1 model the per-phase claude is
        typically already dead by the time kill() is called from
        teardown; this is a safety net.
        """
        # Best-effort cleanup of legacy supervisor pid first.
        wrapper_pid = self._read_int_legacy(container, WRAPPER_PID_PATH)
        if wrapper_pid is not None:
            self._exec_sync(
                container,
                ["sh", "-c", f"kill -TERM -- -{wrapper_pid} 2>/dev/null || true"],
            )
        # Then anything still matching claude.
        self._transport.kill_process(container, "claude -p")

    def fetch_logs(self, container: ContainerHandle) -> AgentLogs:
        """Read the legacy stdout/stderr/exit_code paths. Returns empty
        strings under the v1 model (those paths aren't written anymore).
        New code should consume per-phase artifacts written by
        `run_phase` (stream-{phase}.jsonl / agent-{phase}.log /
        metadata-{phase}.json).
        """
        stdout = self._read_text(container, STDOUT_PATH)
        stderr = self._read_text(container, STDERR_PATH)
        exit_code = self._read_int_legacy(container, EXIT_CODE_PATH)
        return AgentLogs(stdout=stdout, stderr=stderr, exit_code=exit_code)

    # ── internals (legacy + helpers) ────────────────────────────────────

    def _write_mcp_config(self, container: ContainerHandle, cfg: AgentConfig) -> None:
        """Drop mcp.json with the team token in headers (M4.5 convention).
        Mode 600 so it's not world-readable inside the container."""
        payload = {
            "mcpServers": {
                "cyberarena": {
                    "type": "http",
                    "url": cfg.mcp_endpoint,
                    "headers": {"X-Team-Token": cfg.team_token},
                }
            }
        }
        self._transport.write_file(
            container, MCP_CONFIG_PATH, (json.dumps(payload, indent=2) + "\n").encode()
        )
        self._exec_sync(container, ["chmod", "600", MCP_CONFIG_PATH], check=True)

    def _build_env_file(self, cfg: AgentConfig) -> str:
        """agent.env contents: API key + base URL + IS_SANDBOX, plus all
        the M5.6 attack-mode fields when populated. CLAUDE_MODEL is now
        also exported so the per-phase claude invocation can pick it up
        without needing --model on the command line."""
        lines = [
            f"export ANTHROPIC_API_KEY={shlex.quote(cfg.api_key)}",
            f"export ANTHROPIC_BASE_URL={shlex.quote(cfg.base_url)}",
            "export IS_SANDBOX=1",
        ]
        if cfg.model:
            lines.append(f"export CLAUDE_MODEL={shlex.quote(cfg.model)}")
        if cfg.team_id:
            lines.append(f"export CYBERARENA_TEAM_ID={shlex.quote(cfg.team_id)}")
        if cfg.team_token:
            lines.append(f"export CYBERARENA_TEAM_TOKEN={shlex.quote(cfg.team_token)}")
        if cfg.own_prod_host:
            lines.append(f"export CYBERARENA_OWN_PROD={shlex.quote(cfg.own_prod_host)}")
        if cfg.opposing_prod_hosts:
            lines.append(
                "export CYBERARENA_OPPOSING_PRODS="
                + shlex.quote(",".join(cfg.opposing_prod_hosts))
            )
        if cfg.submit_url:
            lines.append(f"export CYBERARENA_SUBMIT_URL={shlex.quote(cfg.submit_url)}")
        for k, v in cfg.extra_env.items():
            lines.append(f"export {k}={shlex.quote(v)}")
        return "\n".join(lines) + "\n"

    def _provision_ssh_pivot(self, dev_container: ContainerHandle, cfg: AgentConfig) -> None:
        """Generate an ed25519 keypair on the host, push the pubkey to
        own_prod's authorized_keys, drop the privkey + ssh config into
        dev. Ephemeral key per install() — never persisted.

        Detects the prod-side SSH user at runtime: `arena_agent` if the
        target image carries that non-root user (DEFCON framework image,
        e.g. cyberarena/chal-nautilus-ico), else falls back to root
        (legacy cyberarena/prod-base used by notes / M5 / M5.6 tests).
        """
        prod_name = cfg.own_prod_container_name
        log.info("provisioning ssh pivot %s -> %s", dev_container.name, prod_name)

        with tempfile.TemporaryDirectory(prefix="cyberarena-keys-") as keydir:
            priv = os.path.join(keydir, "id_ed25519")
            pub = priv + ".pub"
            r = subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", priv, "-q", "-C", f"agent-{cfg.team_id}"],
                check=False, capture_output=True,
            )
            if r.returncode != 0:
                raise RuntimeError(f"ssh-keygen failed: {r.stderr.decode(errors='replace')!r}")
            with open(priv) as f:
                privkey = f.read()
            with open(pub) as f:
                pubkey = f.read()

        prod_container = ContainerHandle(container_id="", name=prod_name, networks=[])
        rc, _ = self._exec_sync(prod_container, ["id", "arena_agent"])
        if rc == 0:
            ssh_user = "arena_agent"
            ssh_home = "/home/arena_agent"
        else:
            ssh_user = "root"
            ssh_home = "/root"
        log.info("ssh pivot target user on %s: %s", prod_name, ssh_user)

        ssh_dir = f"{ssh_home}/.ssh"
        self._exec_sync(prod_container, ["mkdir", "-p", ssh_dir], check=True)
        self._exec_sync(prod_container, ["chmod", "700", ssh_dir], check=True)
        self._transport.write_file(prod_container, f"{ssh_dir}/authorized_keys", pubkey.encode())
        self._exec_sync(prod_container, ["chmod", "600", f"{ssh_dir}/authorized_keys"], check=True)
        if ssh_user != "root":
            self._exec_sync(prod_container, ["chown", "-R", f"{ssh_user}:{ssh_user}", ssh_dir], check=True)

        self._exec_sync(prod_container, ["mkdir", "-p", "/run/sshd"], check=True)
        self._exec_sync(prod_container, ["ssh-keygen", "-A"], check=True)
        self._exec_sync(prod_container, ["/usr/sbin/sshd"], check=True)

        self._exec_sync(dev_container, ["mkdir", "-p", "/root/.ssh"], check=True)
        self._exec_sync(dev_container, ["chmod", "700", "/root/.ssh"], check=True)
        self._transport.write_file(dev_container, "/root/.ssh/id_ed25519", privkey.encode())
        self._exec_sync(dev_container, ["chmod", "600", "/root/.ssh/id_ed25519"], check=True)
        ssh_config = (
            f"Host {cfg.own_prod_host}\n"
            f"    User {ssh_user}\n"
            f"    IdentityFile /root/.ssh/id_ed25519\n"
            f"    StrictHostKeyChecking no\n"
            f"    UserKnownHostsFile /dev/null\n"
            f"    LogLevel ERROR\n"
        )
        self._transport.write_file(dev_container, "/root/.ssh/config", ssh_config.encode())
        self._exec_sync(dev_container, ["chmod", "600", "/root/.ssh/config"], check=True)

    # ── sync exec helpers (docker-py based, used by install + legacy paths) ─

    def _exec_sync(
        self,
        container: ContainerHandle,
        cmd: list[str] | str,
        *,
        check: bool = False,
    ) -> tuple[int, str]:
        """Synchronous exec via docker-py. Used during install where we
        don't need async + timeout semantics. The transport's
        run_command is the async path for per-phase work.
        """
        if self._client is None:
            # Transport-only injection (tests). Fall back to a synchronous
            # transport invocation via asyncio.run — install paths in
            # tests typically pass a docker client; this branch is mostly
            # defensive.
            result = asyncio.run(self._transport.run_command(container, cmd))
            out = (result.stdout + result.stderr).decode(errors="replace")
            if check and result.exit_code != 0:
                raise RuntimeError(
                    f"exec failed ({cmd!r}) on {container.name}: rc={result.exit_code}, out={out!r}"
                )
            return result.exit_code, out

        c = self._client.containers.get(container.name)
        result = c.exec_run(cmd, demux=False)
        out = result.output.decode(errors="replace") if result.output else ""
        if check and result.exit_code != 0:
            raise RuntimeError(
                f"exec failed ({cmd!r}) on {container.name}: rc={result.exit_code}, out={out!r}"
            )
        return result.exit_code, out

    def _read_text(self, container: ContainerHandle, path: str) -> str:
        rc, out = self._exec_sync(container, ["sh", "-c", f"cat {path} 2>/dev/null || true"])
        return out

    def _read_int_legacy(self, container: ContainerHandle, path: str) -> int | None:
        rc, out = self._exec_sync(container, ["sh", "-c", f"cat {path} 2>/dev/null || true"])
        out = out.strip()
        if not out:
            return None
        try:
            return int(out)
        except ValueError:
            return None
