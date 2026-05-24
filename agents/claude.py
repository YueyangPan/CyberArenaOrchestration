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

import logging
import shlex
import time

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
PID_PATH = f"{RUN_DIR}/agent.pid"
WRAPPER_PID_PATH = f"{RUN_DIR}/agent.wrapper.pid"
EXIT_CODE_PATH = f"{RUN_DIR}/agent.exit_code"
STDOUT_PATH = f"{LOG_DIR}/agent.stdout"
STDERR_PATH = f"{LOG_DIR}/agent.stderr"

DEFAULT_PROMPT = "You are an A/D CTF agent. Wait for instructions."


class ClaudeAgentManager(AgentManager):
    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._client = client or docker.from_env()

    # ── public API ──────────────────────────────────────────────────────

    def install(self, container: ContainerHandle, cfg: AgentConfig) -> None:
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

        prompt = cfg.system_prompt or DEFAULT_PROMPT
        self._write_file(container, PROMPT_PATH, prompt.rstrip() + "\n")
        self._write_file(
            container,
            ENV_PATH,
            (
                f"export ANTHROPIC_API_KEY={shlex.quote(cfg.api_key)}\n"
                f"export ANTHROPIC_BASE_URL={shlex.quote(cfg.base_url)}\n"
                f"export IS_SANDBOX=1\n"
            ),
        )

        self._launch(container, cfg)
        self._wait_for_pid(container, timeout_s=5.0)
        log.info("agent installed on %s (cli=%r)", container.name, cfg.cli)

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
        script = f"""
set -e
nohup setsid bash -c '
  echo $BASHPID > {WRAPPER_PID_PATH}
  set -a; . {ENV_PATH}; set +a
  {cfg.cli} < {PROMPT_PATH} > {STDOUT_PATH} 2> {STDERR_PATH} &
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
