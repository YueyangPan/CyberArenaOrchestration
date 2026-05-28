"""MCP server — agent's structured action channel into the host.

Built with anthropic's `mcp` Python SDK (`FastMCP`), Streamable HTTP
transport, mounted under the existing FastAPI app at `MCP_MOUNT_PATH`.
Lifespan must be wired by the caller (see `gamemgr/api.py`); a bare
`app.mount(...)` of `build_mcp_app(...)` without lifespan will 500 on
every request with "Task group is not initialized".

Auth: every tool inspects the `X-Team-Token` header on the live HTTP
request via `Context.request_context.request`. Unknown / missing token
raises an MCP error with code -32001 and a deliberately generic
"Unauthorized" message — no `BAD_TOKEN` / team-id / token-hint strings
on the wire (M4.5 §安全注意: don't be a token oracle).

Tools (three; named per the M4.5 plan):
  - submit_flag(flag)              shared with REST /game/flag/submit
  - report_ready()                 LLM ack that the channel works
  - report_deploy_complete(...)    agent self-reports defense finish

Mount-path constants are exported by-design (see M4.5 §MCP mount 路径):
  MCP_MOUNT_PATH       FastAPI app.mount() prefix
  MCP_PUBLIC_ENDPOINT  what clients put in mcp.json (with trailing slash
                       to dodge FastAPI's `/mcp` → `/mcp/` 307)
GameMgr.agent_config_for uses MCP_PUBLIC_ENDPOINT directly — never
hand-write "/mcp" / "/mcp/" literals anywhere else.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

if TYPE_CHECKING:
    from starlette.applications import Starlette

    from .game_mgr import GameMgr


log = logging.getLogger(__name__)

# ── URL constants ───────────────────────────────────────────────────────
#
# Conservative: two explicit constants instead of any "auto-detect SDK
# internal path" magic. FastMCP defaults streamable_http_path to "/mcp"
# inside its sub-app — combined with our outer mount at "/mcp" that would
# expose the wire URL as "/mcp/mcp", so we set the inner path to "/" so
# the wire URL is just "{MCP_MOUNT_PATH}/" ⇒ "/mcp/".
#
# If the SDK changes its internal layout in a future minor, the Claude CLI
# smoke test will catch it and the fix is to update MCP_PUBLIC_ENDPOINT
# here — not to write code that introspects the SDK.

MCP_MOUNT_PATH = "/mcp"
MCP_PUBLIC_ENDPOINT = "/mcp/"

# JSON-RPC error code for auth failure. Outside the SDK's reserved range
# (-32600..-32603) so it doesn't collide with transport-level errors.
_UNAUTHORIZED_CODE = -32001
_UNAUTHORIZED_MESSAGE = "Unauthorized"


def build_mcp_app(game: "GameMgr") -> tuple[FastMCP, "Starlette"]:
    """Construct the FastMCP server bound to a GameMgr and return
    (server, asgi_app). The caller mounts asgi_app under MCP_MOUNT_PATH
    and runs server.session_manager.run() inside the outer app's lifespan.
    """
    # FastMCP auto-enables DNS rebinding protection when host is localhost
    # (default), restricting allowed Host headers to 127.0.0.1/localhost/::1.
    # Agents inside dev containers reach us through host.docker.internal —
    # add it (and bare localhost without the port-wildcard quirks) so the
    # 421 Misdirected Request doesn't kill every tool call.
    mcp = FastMCP(
        "cyberarena",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "127.0.0.1:*", "localhost:*", "[::1]:*",
                "host.docker.internal:*",
            ],
            allowed_origins=[
                "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                "http://host.docker.internal:*",
            ],
        ),
    )
    # Mount the sub-app at "/" internally so the public wire path is just
    # whatever prefix the outer FastAPI mounts at (MCP_MOUNT_PATH + "/").
    mcp.settings.streamable_http_path = "/"

    def _team_id_from_ctx(ctx: Context) -> str:
        """Resolve X-Team-Token header → team_id, or raise generic
        Unauthorized. Never reveals which step failed (no header / unknown
        token / etc.) — see M4.5 token-oracle hardening."""
        req = ctx.request_context.request
        token = ""
        if req is not None:
            token = req.headers.get("x-team-token", "")
        team_id = game._token_to_team.get(token) if token else None
        if team_id is None:
            log.info("mcp: rejecting request (BAD_TOKEN, internal); returning Unauthorized on the wire")
            raise McpError(
                ErrorData(code=_UNAUTHORIZED_CODE, message=_UNAUTHORIZED_MESSAGE)
            )
        return team_id

    @mcp.tool()
    async def submit_flag(flag: str, ctx: Context) -> dict:
        """Submit a stolen flag. Returns {accepted, reason}.

        Shares the same `GameMgr.submit_flag()` handler as REST, so the
        emitted `FLAG_SUBMITTED` event is identical regardless of which
        transport the submission arrived through.
        """
        team_id = _team_id_from_ctx(ctx)
        result = await game.submit_flag(team_id, flag)
        return {"accepted": result.accepted, "reason": result.reason}

    @mcp.tool()
    async def report_ready(ctx: Context) -> dict:
        """Agent ack: 'I'm running and the MCP channel works.' Best-effort
        — never the only signal that the agent is alive."""
        team_id = _team_id_from_ctx(ctx)
        await game.record_agent_ready(team_id)
        return {"ok": True}

    @mcp.tool()
    async def report_deploy_complete(
        service: str,
        duration_ms: int | None,
        ctx: Context,
    ) -> dict:
        """Agent self-report after defense deploy finishes. Service name
        is validated against the yaml challenges whitelist before anything
        gets written — typos / unknown services are rejected here so they
        never reach adaptive-duration aggregation."""
        team_id = _team_id_from_ctx(ctx)
        if not game.is_known_service(service):
            raise McpError(
                ErrorData(code=INVALID_PARAMS, message="unknown service")
            )
        await game.record_deploy_complete(team_id, service, duration_ms)
        return {"ok": True}

    @mcp.tool()
    async def restart_service(service: str, ctx: Context) -> dict:
        """Restart the team's service container after a defense edit.

        Async by design: returns `{job_id}` immediately. The orchestrator
        runs the per-challenge restart_cmd via docker exec in a background
        task; STARTED / COMPLETED|FAILED + per-level SERVICE_HEALTH_CHECK
        events land on the bus as the job progresses. Agent waits for the
        service to be reachable (TCP-probe its own prod) and then calls
        report_deploy_complete.

        Returns:
          {"job_id": "<12-hex>"} — restart accepted, running in background.
        Raises:
          InvalidParams: unknown service, no restart_cmd in spec, or a
            restart is already in flight for (team, service).
          Unauthorized: bad / missing X-Team-Token (silent on the bus).
        """
        team_id = _team_id_from_ctx(ctx)
        try:
            job_id = await game.restart_service(team_id, service)
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        return {"job_id": job_id}

    asgi_app = mcp.streamable_http_app()
    return mcp, asgi_app


def canonical_endpoint_url(base_url: str) -> str:
    """Compose the wire URL clients should put in mcp.json.

    base_url is the FastAPI server's origin (e.g.
    "http://host.docker.internal:8000"); the returned URL appends
    MCP_PUBLIC_ENDPOINT so the trailing slash is present.
    """
    return f"{base_url.rstrip('/')}{MCP_PUBLIC_ENDPOINT}"
