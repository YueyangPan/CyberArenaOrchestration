"""M4.5 — install() callback wiring.

In the persistent-memory v1 model `install()` is one-shot (no
supervisor launches). Both callbacks fire from the end of install():
on_installed first, then on_launched (with wrapper_pid=0, kept for
M4.5 back-compat). The M4.5 three-event chain semantic — INSTALLED
present, LAUNCHED absent on partial failure — no longer applies
because there's no separate launch step.

What we still pin:
  ①both callbacks fire on the happy path, in declared order.
  ②if install raises before the callback site, neither fires.
  ③empty team_id → callbacks skipped (defensive for non-multi-tenant tests).

No docker, no real CLI — ClaudeAgentManager runs against a mock
docker client satisfying the small subset of exec_run calls install()
makes.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from agents.claude import (
    ClaudeAgentManager,
    EXIT_CODE_PATH,
    PID_PATH,
    WRAPPER_PID_PATH,
)
from models.agent import AgentConfig
from orchestrator.service import ContainerHandle


def _make_mock_client(pid: int = 12345, wrapper_pid: int = 12344) -> MagicMock:
    """Build a docker client mock that fakes the small subset of behavior
    ClaudeAgentManager touches during install: exec_run for shell commands
    and pid/wrapper-pid file reads."""
    client = MagicMock()

    # State the supervisor would have written.
    files: dict[str, bytes] = {
        PID_PATH: str(pid).encode(),
        WRAPPER_PID_PATH: str(wrapper_pid).encode(),
    }

    def exec_run(cmd, **kwargs):
        result = MagicMock()
        result.exit_code = 0
        # Reads come through `sh -c "cat <path> 2>/dev/null || true"`
        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[0] == "sh" and cmd[1] == "-c":
            script = cmd[2]
            for path, blob in files.items():
                if path in script and "cat" in script:
                    result.output = blob
                    return result
            # Clear-stale-state sh: matches PID_PATH but isn't a read.
            result.output = b""
            return result
        result.output = b""
        return result

    container = MagicMock()
    container.exec_run = exec_run
    container.id = "mocked-container-id"
    client.containers.get.return_value = container
    return client


def _handle(name: str = "team1_dev") -> ContainerHandle:
    return ContainerHandle(container_id="mocked-id", name=name, networks=["team1_net"])


def test_install_emits_installed_then_launched_in_order():
    """Happy path: both callbacks fire from install(), INSTALLED then
    LAUNCHED. In v1 they fire from the same code path, so timestamps
    may be very close — we only assert ordering by call sequence, not
    by wall clock."""
    client = _make_mock_client(pid=4321, wrapper_pid=4320)
    mgr = ClaudeAgentManager(client=client)
    cfg = AgentConfig(cli="sleep 60", team_id="team1")

    fired: list[tuple[str, tuple]] = []

    def on_installed(team_id):
        fired.append(("INSTALLED", (team_id,)))

    def on_launched(team_id, wrapper_pid):
        fired.append(("LAUNCHED", (team_id, wrapper_pid)))

    mgr.install(_handle(), cfg, on_installed=on_installed, on_launched=on_launched)

    assert len(fired) == 2, f"expected two callbacks, got: {fired}"
    assert fired[0][0] == "INSTALLED", f"first callback should be INSTALLED, got {fired}"
    assert fired[1][0] == "LAUNCHED", f"second callback should be LAUNCHED, got {fired}"
    assert fired[0][1] == ("team1",)
    # v1: wrapper_pid is 0 (no supervisor exists; field preserved for back-compat).
    assert fired[1][1] == ("team1", 0)


def test_install_fault_injection_skips_both_callbacks():
    """If something inside install raises before reaching the callback
    site, neither callback fires. In v1 there's no INSTALLED-yes-LAUNCHED-
    no intermediate state to assert anymore — both callbacks live at the
    very end of install() so failures bail out before either runs.
    """
    client = _make_mock_client()
    mgr = ClaudeAgentManager(client=client)
    cfg = AgentConfig(cli="sleep 60", team_id="team2")

    # Inject failure into _build_env_file — runs before the callback site.
    def boom(_cfg):
        raise RuntimeError("simulated install failure")

    mgr._build_env_file = boom  # type: ignore[assignment]

    fired: list[str] = []
    with pytest.raises(RuntimeError, match="simulated install failure"):
        mgr.install(
            _handle("team2_dev"),
            cfg,
            on_installed=lambda t: fired.append(f"INSTALLED:{t}"),
            on_launched=lambda t, p: fired.append(f"LAUNCHED:{t}:{p}"),
        )

    assert fired == [], (
        "install raised before reaching the callback site; neither "
        f"callback should have fired. got: {fired}"
    )


def test_install_skips_callbacks_when_team_id_blank():
    """Defensive: empty team_id (legacy AgentConfig from M4 tests) means
    the agent isn't multi-tenant — no event lands on the bus. Callbacks
    must NOT fire with empty team_id, or downstream code that keys on
    team_id will choke."""
    client = _make_mock_client()
    mgr = ClaudeAgentManager(client=client)
    cfg = AgentConfig(cli="sleep 60")  # team_id="" by default

    fired: list[str] = []
    mgr.install(
        _handle(),
        cfg,
        on_installed=lambda t: fired.append("installed"),
        on_launched=lambda t, p: fired.append("launched"),
    )
    assert fired == [], f"callbacks must be skipped when team_id=''; got: {fired}"
