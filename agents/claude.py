"""ClaudeAgentManager — Claude Code adapter for the AgentManager ABC.

Two responsibilities under persistent-memory v1 (see
docs/persistent_memory_agent_plan_v1.md):

  install(handle, cfg)            # once per team at game start: npm
                                  # install, SSH pivot, env, mcp.json.
                                  # No process launched.
  phase_command(model=None)       # returns the shell argv that
                                  # invokes `claude -p --continue ...`
                                  # consuming the prompt from PROMPT_PATH.

The per-phase 3-line dance (write prompt → run_command → kill on
timeout) lives in `agents.runner.run_phase_via_transport`, not here.
GameMgr owns artifact persistence + AGENT_* event emission via
`record_phase_started` / `record_phase_done`.

All side effects on the agent host go through `AgentTransport`
(DockerExecBackend today, SSHBackend tomorrow). The manager never
calls `docker exec` directly.

Memory across phases carries via Claude CLI's `--continue` flag: the
CLI's session store lives at /root/.claude/projects/<workdir-cwd>/
inside the container; `--continue` reloads the most-recent session.
We pin the cwd to STATE_DIR/workdir so different arenas' sessions
stay isolated.

`status` / `kill` / `fetch_logs` are NOT part of the ABC anymore —
they're concrete-only convenience methods kept for the M4-era live
smoke tests (`test_m4_agent::test_live_hello_world`,
`test_m5_6_agent_attack`) and `scripts/m6_screenshot_demo.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import tempfile
from typing import Callable, ClassVar

import docker

from models.agent import AgentConfig, AgentLogs, AgentState, AgentStatus
from orchestrator.handle import ContainerHandle

from .manager import AgentManager
from .transport import AgentHandle, AgentTransport
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

# install() callback. Fires after env / mcp.json / ssh pivot / workdir
# are staged. The persistent-memory v1 install has no separate launch
# step (claude is spawned per-phase by run_phase), so this is the only
# install-side lifecycle hook.
OnInstalled = Callable[[str], None]


class ClaudeAgentManager(AgentManager):
    """Host-side controller. One instance handles every team; each
    method takes a ContainerHandle for the team's dev container.

    The transport is injected (DockerExecBackend by default). Tests
    can pass a FakeTransport that records calls in-memory.
    """

    # ABC contract (`agents.manager.AgentManager`).
    PROMPT_PATH: ClassVar[str] = PROMPT_PATH
    PHASE_KILL_PATTERN: ClassVar[str] = "claude -p"

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
          6. Fire on_installed (caller bridges to AGENT_INSTALLED on the bus).

        NO process is launched. The first `phase_command` invocation
        (driven by `agents.runner.run_phase_via_transport`) is what
        actually starts Claude.
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
            self._npm_install_with_retry(container)

        if cfg.own_prod_container_name:
            self._provision_ssh_pivot(container, cfg)

        self._transport.write_file(container, ENV_PATH, self._build_env_file(cfg).encode())

        if cfg.mcp_endpoint:
            self._write_mcp_config(container, cfg)

        log.info("agent installed on %s (cli=%r)", container.name, cfg.cli)

        # The one install-side lifecycle hook. There's no separate
        # "launched" step — claude is spawned per-phase by
        # `agents.runner.run_phase_via_transport` (which composes
        # `phase_command()` with the transport); GameMgr.record_phase_*
        # emits AGENT_PROMPT_SENT / COMPLETED / TIMEOUT / FAILED.
        if on_installed is not None and cfg.team_id:
            on_installed(cfg.team_id)

    def phase_command(
        self,
        *,
        model: str | None = None,
        session_marker: str | None = None,
    ) -> list[str]:
        """Shell argv that drives one phase of Claude Code.

        Wraps the actual claude invocation in `bash -c` so we can:
          - cd into WORKDIR (claude --continue is cwd-scoped, so the
            session store at /root/.claude/projects/<cwd-hash>/ stays
            isolated to this arena)
          - source agent.env (ANTHROPIC_API_KEY / BASE_URL / CLAUDE_MODEL)
          - redirect stdin from PROMPT_PATH (claude reads the user prompt
            from stdin in -p mode)

        `model` overrides `$CLAUDE_MODEL` (which agent.env exports from
        cfg.model). Production leaves it None; tests pass it explicitly
        when the test mock client doesn't ship a model env var.

        `session_marker` (optional, set by the runner per-invocation):
        when given, claude is launched under `setsid -w bash -c …` so it
        gets its own process group. The inner bash writes its PID (=PGID,
        because it's the session leader) to
        `/tmp/cyberarena_phase_<marker>.pid` and then `exec`s claude in
        place (same PID/PGID). Any subprocesses claude spawns (exploits,
        background jobs) inherit that PGID, so the runner's on-timeout
        `kill_session` does `kill -KILL -- -<pgid>` and reaps the whole
        group — no orphans, no race with a sibling claude that the next
        phase just started in the same container.
        """
        model_arg = (
            f"--model {shlex.quote(model)} "
            if model
            else '--model "$CLAUDE_MODEL" '
        )
        claude_argv = (
            "claude -p --continue "
            "--output-format stream-json --verbose "
            "--dangerously-skip-permissions "
            + model_arg
            + f"--mcp-config {shlex.quote(MCP_CONFIG_PATH)} "
            f"< {shlex.quote(self.PROMPT_PATH)}"
        )
        if session_marker:
            pid_file = f"/tmp/cyberarena_phase_{session_marker}.pid"
            inner = f"echo $$ > {shlex.quote(pid_file)} && exec {claude_argv}"
            launch = f"exec setsid -w bash -c {shlex.quote(inner)}"
        else:
            launch = claude_argv
        return [
            "bash", "-c",
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            + launch,
        ]

    # ── concrete-only convenience methods (NOT on the ABC) ─────────────
    # Kept for the M4-era live-smoke tests (test_m4_agent::test_live_hello_world,
    # test_m5_6_agent_attack) + scripts/m6_screenshot_demo.py. Under
    # persistent-memory v1 install no longer writes PID/WRAPPER_PID/
    # EXIT_CODE files, so these are mostly best-effort no-ops on this
    # subclass — but the live smokes still expect them to be callable.

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

    # npm install hits the registry over the internet — `ECONNRESET` blips
    # are common enough to be worth a short retry. Backs off 2s → 4s → 8s.
    _NPM_RETRY_ATTEMPTS = 3
    _NPM_RETRY_BASE_SECS = 2

    def _npm_install_with_retry(self, container: ContainerHandle) -> None:
        """`npm install -g @anthropic-ai/claude-code` with exponential
        backoff on transient registry errors. Raises only after the final
        attempt — exhaustion bubbles up as a normal install failure."""
        cmd = ["npm", "install", "-g", "@anthropic-ai/claude-code"]
        for attempt in range(1, self._NPM_RETRY_ATTEMPTS + 1):
            rc, out = self._exec_sync(container, cmd)
            if rc == 0:
                if attempt > 1:
                    log.info("npm install succeeded on %s (attempt %d/%d)",
                             container.name, attempt, self._NPM_RETRY_ATTEMPTS)
                return
            if attempt == self._NPM_RETRY_ATTEMPTS:
                raise RuntimeError(
                    f"npm install failed on {container.name} after "
                    f"{self._NPM_RETRY_ATTEMPTS} attempts: rc={rc}, out={out[-300:]!r}"
                )
            backoff = self._NPM_RETRY_BASE_SECS * (2 ** (attempt - 1))
            log.warning(
                "npm install failed on %s (attempt %d/%d, rc=%d); retrying in %ds. tail=%r",
                container.name, attempt, self._NPM_RETRY_ATTEMPTS, rc, backoff, out[-200:],
            )
            time.sleep(backoff)

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
        target image carries that non-root user (every per-challenge
        image bakes this user — cyberarena/chal-notes,
        cyberarena/chal-nautilus-ico), else falls back to root for any
        external/legacy image that doesn't follow the convention.
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
