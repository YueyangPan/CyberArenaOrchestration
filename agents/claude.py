"""ClaudeAgentManager — installs and supervises a Claude Code CLI agent.

Launch strategy (see docs/m4_agent_plan.md §"Agent install 流程"):

  setsid bash -c '...' &           # detach from docker-exec session, new pgid
      └── <cli> ... &              # child whose pid we record in agent.pid
      wait $!
      echo $? > agent.exit_code    # records exit status after child dies

The wrapper writes its own PID (== process-group leader, thanks to setsid)
to agent.wrapper.pid; kill() sends SIGTERM to -<wrapper_pid> so bash + cli +
all its descendants get the signal together. Killing only agent.pid would
leave node helper procs reparented to init and lingering until container
teardown.

If cfg.cli starts with "claude" we npm-install @anthropic-ai/claude-code first;
any other cli (e.g. "sleep 60" used by tests) is launched as-is.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import tempfile
import time
from typing import Callable

import docker

from models.agent import AgentConfig, AgentLogs, AgentState, AgentStatus
from orchestrator.service import ContainerHandle

from .manager import AgentManager

log = logging.getLogger(__name__)

STATE_DIR = "/var/lib/cyberarena"
LOG_DIR = "/var/log/cyberarena"
RUN_DIR = "/var/run/cyberarena"

PROMPT_PATH = f"{STATE_DIR}/prompt.txt"
ENV_PATH = f"{STATE_DIR}/agent.env"
MCP_CONFIG_PATH = f"{STATE_DIR}/mcp.json"
PID_PATH = f"{RUN_DIR}/agent.pid"
WRAPPER_PID_PATH = f"{RUN_DIR}/agent.wrapper.pid"
EXIT_CODE_PATH = f"{RUN_DIR}/agent.exit_code"
STDOUT_PATH = f"{LOG_DIR}/agent.stdout"
STDERR_PATH = f"{LOG_DIR}/agent.stderr"

# M4.5 callback shapes: optional, may be None for non-MCP tests (M4
# hello-world etc. that don't run with a GameMgr). Sync — the install()
# itself is sync and called from non-async contexts; if the caller needs
# to dispatch into async code (e.g. EventBus.publish on a uvicorn loop),
# it wraps with asyncio.run_coroutine_threadsafe.
OnInstalled = Callable[[str], None]
OnLaunched = Callable[[str, int], None]

DEFAULT_PROMPT = "You are an A/D CTF agent. Wait for instructions."


class ClaudeAgentManager(AgentManager):
    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._client = client or docker.from_env()

    # ── public API ──────────────────────────────────────────────────────

    def install(
        self,
        container: ContainerHandle,
        cfg: AgentConfig,
        *,
        on_installed: OnInstalled | None = None,
        on_launched: OnLaunched | None = None,
    ) -> None:
        """Install + launch the supervisor.

        M4.5 split: the callable runs in two phases with optional callbacks
        at the boundary — `on_installed` fires after the prep stage (files /
        env / ssh / mcp.json / npm install all done) and *before* the
        supervisor starts; `on_launched` fires after `_wait_for_pid`
        confirms the supervisor pid was captured. Both callbacks are
        sync — wrap with `asyncio.run_coroutine_threadsafe` if you need
        to dispatch into async (e.g. an EventBus on the uvicorn loop).
        """
        # ── prep stage ─────────────────────────────────────────────────
        self._exec(container, ["mkdir", "-p", STATE_DIR, LOG_DIR, RUN_DIR], check=True)

        # Re-install must replace any running supervisor, not race with it.
        # kill() is a no-op if nothing was installed before. The prior
        # supervisor's bash will exit as a zombie under the container's PID-1
        # (which doesn't reap), but that's harmless: the new supervisor gets
        # a fresh pid + pgid via setsid.
        self.kill(container)

        # Clear stale supervisor state from a prior run, so wait_for_pid does
        # not see an old agent.pid and report success before launch happens.
        self._exec(
            container,
            ["sh", "-c", f"rm -f {PID_PATH} {WRAPPER_PID_PATH} {EXIT_CODE_PATH}"],
            check=True,
        )

        if cfg.cli.startswith("claude"):
            self._exec(
                container,
                ["npm", "install", "-g", "@anthropic-ai/claude-code"],
                check=True,
            )

        # Set up SSH pivot into own prod if requested (M5.6). Generates a key
        # per install() invocation — keys never persist across runs.
        if cfg.own_prod_container_name:
            self._provision_ssh_pivot(container, cfg)

        prompt = cfg.system_prompt or DEFAULT_PROMPT
        self._write_file(container, PROMPT_PATH, prompt.rstrip() + "\n")
        self._write_file(container, ENV_PATH, self._build_env_file(cfg))

        # M4.5 MCP config (only when mcp_endpoint is populated; empty keeps
        # the non-MCP path intact for older tests).
        if cfg.mcp_endpoint:
            self._write_mcp_config(container, cfg)

        # Prep done. Fire on_installed BEFORE launch so it stays diagnostic
        # of "files ready, about to launch" — if _launch then crashes, the
        # absence of AGENT_LAUNCHED tells the operator exactly where to look.
        if on_installed is not None and cfg.team_id:
            on_installed(cfg.team_id)

        # ── launch stage ──────────────────────────────────────────────
        self._launch(container, cfg)
        self._wait_for_pid(container, timeout_s=5.0)
        log.info("agent installed on %s (cli=%r)", container.name, cfg.cli)

        if on_launched is not None and cfg.team_id:
            wrapper_pid = self._read_int(container, WRAPPER_PID_PATH) or 0
            on_launched(cfg.team_id, wrapper_pid)

    def _write_mcp_config(self, container: ContainerHandle, cfg: AgentConfig) -> None:
        """Drop mcp.json with the team token in headers, chmod 600.

        token-in-headers, never as a tool argument — keeps the LLM from
        ever seeing the string (defends against accidental complete /
        echo in stream-json transcripts).
        """
        payload = {
            "mcpServers": {
                "cyberarena": {
                    "type": "http",
                    "url": cfg.mcp_endpoint,
                    "headers": {"X-Team-Token": cfg.team_token},
                }
            }
        }
        self._write_file(container, MCP_CONFIG_PATH, json.dumps(payload, indent=2) + "\n")
        self._exec(container, ["chmod", "600", MCP_CONFIG_PATH], check=True)

    def _build_env_file(self, cfg: AgentConfig) -> str:
        """Render the agent.env file. Anthropic + IS_SANDBOX always present;
        M5.6 attack-mode fields included when populated; cfg.extra_env merged
        last so it can override anything."""
        lines = [
            f"export ANTHROPIC_API_KEY={shlex.quote(cfg.api_key)}",
            f"export ANTHROPIC_BASE_URL={shlex.quote(cfg.base_url)}",
            "export IS_SANDBOX=1",
        ]
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
        """Generate an ed25519 keypair on host, inject pubkey + start sshd on
        own prod, drop privkey + ssh config into dev. Keys are ephemeral
        (host tmpdir scoped to this install() call)."""
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

        # Pubkey + sshd on prod.
        prod_container = ContainerHandle(container_id="", name=prod_name, networks=[])
        self._exec(prod_container, ["mkdir", "-p", "/root/.ssh"], check=True)
        self._exec(prod_container, ["chmod", "700", "/root/.ssh"], check=True)
        self._write_file(prod_container, "/root/.ssh/authorized_keys", pubkey)
        self._exec(prod_container, ["chmod", "600", "/root/.ssh/authorized_keys"], check=True)
        # Generate host keys + start sshd; missing pidfile dir on ubuntu:24.04
        # minimal install bites if you skip the mkdir.
        self._exec(prod_container, ["mkdir", "-p", "/run/sshd"], check=True)
        self._exec(prod_container, ["ssh-keygen", "-A"], check=True)  # generate host keys
        # PermitRootLogin: default in Ubuntu's sshd_config is "prohibit-password" → key-only root OK.
        self._exec(prod_container, ["/usr/sbin/sshd"], check=True)

        # Privkey + ssh config on dev.
        self._exec(dev_container, ["mkdir", "-p", "/root/.ssh"], check=True)
        self._exec(dev_container, ["chmod", "700", "/root/.ssh"], check=True)
        self._write_file(dev_container, "/root/.ssh/id_ed25519", privkey)
        self._exec(dev_container, ["chmod", "600", "/root/.ssh/id_ed25519"], check=True)
        ssh_config = (
            f"Host {cfg.own_prod_host}\n"
            f"    User root\n"
            f"    IdentityFile /root/.ssh/id_ed25519\n"
            f"    StrictHostKeyChecking no\n"
            f"    UserKnownHostsFile /dev/null\n"
            f"    LogLevel ERROR\n"
        )
        self._write_file(dev_container, "/root/.ssh/config", ssh_config)
        self._exec(dev_container, ["chmod", "600", "/root/.ssh/config"], check=True)

    def status(self, container: ContainerHandle) -> AgentState:
        wrapper_pid = self._read_int(container, WRAPPER_PID_PATH)
        pid = self._read_int(container, PID_PATH)
        exit_code = self._read_int(container, EXIT_CODE_PATH)

        if pid is None:
            return AgentState(status=AgentStatus.NOT_INSTALLED)

        if exit_code is not None:
            if exit_code == 0:
                status = AgentStatus.COMPLETED
            elif exit_code in (130, 143):  # 128+SIGINT, 128+SIGTERM
                status = AgentStatus.TERMINATED
            else:
                status = AgentStatus.CRASHED
            return AgentState(status=status, pid=pid, wrapper_pid=wrapper_pid, exit_code=exit_code)

        # No exit_code file yet → child is presumed running. Confirm with kill -0.
        rc, _ = self._exec(container, ["kill", "-0", str(pid)])
        if rc == 0:
            return AgentState(status=AgentStatus.RUNNING, pid=pid, wrapper_pid=wrapper_pid)
        # pid file present but process gone and no exit_code yet — race during
        # supervisor teardown. Report CRASHED conservatively.
        return AgentState(status=AgentStatus.CRASHED, pid=pid, wrapper_pid=wrapper_pid)

    def kill(self, container: ContainerHandle) -> None:
        wrapper_pid = self._read_int(container, WRAPPER_PID_PATH)
        pid = self._read_int(container, PID_PATH)
        if pid is None and wrapper_pid is None:
            return  # never installed, or already cleaned up

        # Step 1: signal the tracked child only. This lets the supervisor bash
        # observe `wait` returning and record exit_code before we kill it too.
        if pid is not None:
            self._exec(
                container,
                ["sh", "-c", f"kill -TERM {pid} 2>/dev/null || true"],
            )
            # Give the supervisor a moment to wake from wait + write exit_code.
            for _ in range(40):  # up to ~2s
                if self._read_int(container, EXIT_CODE_PATH) is not None:
                    break
                time.sleep(0.05)

        # Step 2: nuke the entire process group. For `sleep 60` this is a no-op
        # (supervisor already exited cleanly); for real CLIs it sweeps up any
        # node helper procs that were reparented away from the tracked pid.
        if wrapper_pid is not None:
            self._exec(
                container,
                ["sh", "-c", f"kill -TERM -- -{wrapper_pid} 2>/dev/null || true"],
            )

    def fetch_logs(self, container: ContainerHandle) -> AgentLogs:
        stdout = self._read_text(container, STDOUT_PATH)
        stderr = self._read_text(container, STDERR_PATH)
        exit_code = self._read_int(container, EXIT_CODE_PATH)
        return AgentLogs(stdout=stdout, stderr=stderr, exit_code=exit_code)

    # ── internals ───────────────────────────────────────────────────────

    def _launch(self, container: ContainerHandle, cfg: AgentConfig) -> None:
        # The supervisor bash runs inside `setsid` so it becomes a new session
        # AND process-group leader. We capture $BASHPID (the leader's pid) and
        # use it as the killpg target later.
        cli = cfg.cli
        # M4.5: add --mcp-config only when MCP is enabled for this agent.
        # Goes on the cli command line, not as an env var, because Claude
        # Code expects this as a flag.
        if cfg.mcp_endpoint and cfg.cli.startswith("claude"):
            cli = f"{cfg.cli} --mcp-config {MCP_CONFIG_PATH}"
        script = f"""
set -e
nohup setsid bash -c '
  echo $BASHPID > {WRAPPER_PID_PATH}
  set -a; . {ENV_PATH}; set +a
  {cli} < {PROMPT_PATH} > {STDOUT_PATH} 2> {STDERR_PATH} &
  CHILD_PID=$!
  echo $CHILD_PID > {PID_PATH}
  wait $CHILD_PID
  echo $? > {EXIT_CODE_PATH}
' </dev/null >/dev/null 2>&1 &
"""
        rc, out = self._exec(container, ["bash", "-c", script])
        if rc != 0:
            raise RuntimeError(f"agent launch failed on {container.name}: rc={rc}, out={out!r}")

    def _wait_for_pid(self, container: ContainerHandle, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._read_int(container, PID_PATH) is not None:
                return
            time.sleep(0.1)
        raise TimeoutError(
            f"agent pid file {PID_PATH} not created within {timeout_s}s on {container.name}"
        )

    # ── exec helpers ────────────────────────────────────────────────────

    def _exec(
        self,
        container: ContainerHandle,
        cmd: list[str] | str,
        *,
        check: bool = False,
    ) -> tuple[int, str]:
        c = self._client.containers.get(container.name)
        result = c.exec_run(cmd, demux=False)
        out = result.output.decode(errors="replace") if result.output else ""
        if check and result.exit_code != 0:
            raise RuntimeError(f"exec failed ({cmd!r}) on {container.name}: rc={result.exit_code}, out={out!r}")
        return result.exit_code, out

    def _write_file(self, container: ContainerHandle, path: str, content: str) -> None:
        # Use a here-doc with a randomish sentinel-free delimiter; content is
        # passed verbatim, no shell interpolation, no leading whitespace strip.
        sentinel = "CYBERARENA_EOF_8d4a"
        script = f"cat > {path} <<'{sentinel}'\n{content}{sentinel}\n"
        self._exec(container, ["sh", "-c", script], check=True)

    def _read_text(self, container: ContainerHandle, path: str) -> str:
        rc, out = self._exec(container, ["sh", "-c", f"cat {path} 2>/dev/null || true"])
        return out

    def _read_int(self, container: ContainerHandle, path: str) -> int | None:
        rc, out = self._exec(container, ["sh", "-c", f"cat {path} 2>/dev/null || true"])
        out = out.strip()
        if not out:
            return None
        try:
            return int(out)
        except ValueError:
            return None
