"""M4.5 — host-emitted AGENT_INSTALLED / AGENT_LAUNCHED ordering.

Pins the two-phase split that makes mid-install failures observable:
  - prep stage finishes  → on_installed(team_id)  → AGENT_INSTALLED
  - _launch + _wait_for_pid done → on_launched(...) → AGENT_LAUNCHED

①happy path: both fire, in order, INSTALLED ts < LAUNCHED ts.
②fault injection: _launch raises mid-flow → INSTALLED present, LAUNCHED absent.
   This is the property that lets an operator distinguish "prep blew up"
   from "launch blew up" without reading logs.

No docker, no real CLI — ClaudeAgentManager is exercised against a mock
docker client that satisfies the _exec / _write_file / _read_int calls
it makes during install().
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
    """Happy path: both callbacks fire; INSTALLED strictly precedes
    LAUNCHED in both call order and timestamps."""
    client = _make_mock_client(pid=4321, wrapper_pid=4320)
    mgr = ClaudeAgentManager(client=client)
    cfg = AgentConfig(cli="sleep 60", team_id="team1")

    fired: list[tuple[str, float, tuple]] = []

    def on_installed(team_id):
        fired.append(("INSTALLED", time.time(), (team_id,)))
        time.sleep(0.01)  # make ts ordering meaningful even on fast machines

    def on_launched(team_id, wrapper_pid):
        fired.append(("LAUNCHED", time.time(), (team_id, wrapper_pid)))

    mgr.install(_handle(), cfg, on_installed=on_installed, on_launched=on_launched)

    assert len(fired) == 2, f"expected two callbacks, got: {fired}"
    assert fired[0][0] == "INSTALLED", f"first callback should be INSTALLED, got {fired}"
    assert fired[1][0] == "LAUNCHED", f"second callback should be LAUNCHED, got {fired}"
    assert fired[0][1] < fired[1][1], (
        f"INSTALLED ts must be strictly less than LAUNCHED ts; got "
        f"{fired[0][1]} vs {fired[1][1]}"
    )
    assert fired[0][2] == ("team1",)
    # wrapper_pid is whatever the mock returned for WRAPPER_PID_PATH.
    assert fired[1][2] == ("team1", 4320)


def test_install_fault_injection_only_emits_installed():
    """If _launch blows up between the two checkpoints, INSTALLED is on
    the bus but LAUNCHED is not — this is the distinguishing signal that
    drove the three-event design (M4.5 plan §three-event chain)."""
    client = _make_mock_client()
    mgr = ClaudeAgentManager(client=client)
    cfg = AgentConfig(cli="sleep 60", team_id="team2")

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated launch failure")

    mgr._launch = boom  # type: ignore[assignment]

    fired: list[str] = []
    with pytest.raises(RuntimeError, match="simulated launch failure"):
        mgr.install(
            _handle("team2_dev"),
            cfg,
            on_installed=lambda t: fired.append(f"INSTALLED:{t}"),
            on_launched=lambda t, p: fired.append(f"LAUNCHED:{t}:{p}"),
        )

    assert fired == ["INSTALLED:team2"], (
        "fault path must emit INSTALLED but never LAUNCHED; "
        f"got: {fired}"
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
