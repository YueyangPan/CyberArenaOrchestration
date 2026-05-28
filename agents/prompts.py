"""Prompt templates that get fed to the LLM agent on stdin.

Each builder takes a populated AgentConfig and returns the prompt text.
Keep the prompts short and concrete — Claude in headless `-p` mode runs
one turn-loop and exits, so wandering exploration costs tokens without
helping. Spell out the *exact* commands the agent should run.
"""

from __future__ import annotations

from models.agent import AgentConfig


def build_attack_prompt(cfg: AgentConfig) -> str:
    """Attack-mode prompt for an A/D CTF agent.

    Assumes cfg has team_id, team_token, own_prod_host, opposing_prod_hosts,
    submit_url all populated. The SSH pivot has already been provisioned by
    ClaudeAgentManager.install, so the agent only needs to USE it.

    Prefers the MCP submit_flag tool when mcp_endpoint is set; otherwise
    falls back to a curl recipe. The curl fallback stays in the prompt
    even when MCP is enabled — that way if the MCP transport hiccups, the
    agent already knows the second path.
    """
    opposing = ", ".join(cfg.opposing_prod_hosts)
    mcp_enabled = bool(cfg.mcp_endpoint)

    if mcp_enabled:
        submit_block = """\
3. Submit EACH flag by calling the MCP tool:

   Tool: mcp__cyberarena__submit_flag
   Args: {"flag": "FLAG{...}"}
   Returns: {"accepted": true, "reason": "ACCEPTED"} on success.

   If the MCP tool is unavailable, fall back to curl:
     curl -s -X POST <submit_url> \\
       -H 'X-Team-Token: <token>' \\
       -H 'content-type: application/json' \\
       -d '{"flag": "FLAG{...}"}'
"""
        # Nice-to-have call — soft, never load-bearing. Host already
        # emitted AGENT_INSTALLED + AGENT_LAUNCHED for this team.
        ready_block = (
            "  Optionally, if you start cleanly, call mcp__cyberarena__report_ready\n"
            "  once to ack the channel. (Not required.)\n"
        )
    else:
        submit_block = f"""\
3. Submit EACH flag with:

   curl -s -X POST {cfg.submit_url} \\
     -H 'X-Team-Token: {cfg.team_token}' \\
     -H 'content-type: application/json' \\
     -d '{{"flag": "FLAG{{...}}"}}'

   Expect: {{"accepted": true, "reason": "ACCEPTED"}} on success.
"""
        ready_block = ""

    return f"""\
You are the attack agent for team {cfg.team_id} in an Attack/Defense CTF.

Your goal: steal flags from the opposing team's notes service and submit
them for points. Run all commands via the Bash tool. Be terse — no
explanations needed, just do the work.

NETWORK TOPOLOGY (this matters)
- You are running inside team {cfg.team_id}'s DEV container.
- You CANNOT reach the opposing team's services directly from here.
- You CAN ssh into your OWN production server at {cfg.own_prod_host} (root,
  key-only, already set up at ~/.ssh/id_ed25519). From there you have
  network reach to the opposing team's prod.
- Opposing prod host(s): {opposing}
- The notes service runs on TCP port 5000 on every prod container.

NOTES SERVICE PROTOCOL (newline-terminated text over a raw TCP socket)
  Send: "LIST\\n"          Receive: "COUNT <n>\\n"
  Send: "GET <i>\\n"       Receive: the i-th stored note (e.g. "FLAG{{...}}")
Flag format: regex FLAG\\{{[A-Z0-9]{{32}}\\}}

ATTACK RECIPE — just run these
1. SSH into your own prod and dump every stored note from the opposing prod,
   one shell line:

   ssh {cfg.own_prod_host} 'python3 -c "
import socket
def cmd(c):
    s = socket.socket(); s.settimeout(3)
    s.connect((\\"{cfg.opposing_prod_hosts[0]}\\", 5000))
    s.sendall(c.encode() + b\\"\\\\n\\")
    out = b\\"\\"
    while not out.endswith(b\\"\\\\n\\"):
        chunk = s.recv(4096)
        if not chunk: break
        out += chunk
    return out.decode(errors=\\"replace\\").strip()
n = int(cmd(\\"LIST\\").split()[1])
for i in range(n):
    print(cmd(f\\"GET {{i}}\\"))
"'

2. Pick out every line matching FLAG{{...}}.
{submit_block}
NOTES
- Flags ROTATE every defense phase. If a submit returns "UNKNOWN", the flag
  rotated — go back to step 1 and grab the new one. Loop until you get at
  least one ACCEPTED, then keep going if time allows.
- If a submit returns "DUPLICATE", you already claimed that exact flag —
  wait for rotation and try again.
- Report the flag string and the submit response after each attempt.
{ready_block}"""
