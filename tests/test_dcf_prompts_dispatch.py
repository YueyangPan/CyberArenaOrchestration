"""DEFCON: per-challenge prompt dispatch.

Pins that `agents.prompts.build_attack_prompt(cfg, service_name=...)`
routes to the right per-challenge module:
- service_name='notes' → notes recipe (LIST/GET on port 5000)
- service_name='nautilus-ico' → ico recipe (binary protocol on 4265)
- service_name=None or unknown → notes fallback (M5.6 back-compat)

Also exercises `build_defense_prompt` per-challenge override + the
generic-fallback path.
"""

from __future__ import annotations

import pytest

from agents.prompts import build_attack_prompt, build_defense_prompt
from models.agent import AgentConfig


def _cfg(**overrides) -> AgentConfig:
    base = AgentConfig(
        cli="claude --print",
        model="claude-sonnet-4-5",
        api_key="test-key",
        base_url="https://api.anthropic.com",
        team_id="team1",
        team_token="tok-team1",
        own_prod_host="team1_prod",
        own_prod_container_name="team1_prod",
        opposing_prod_hosts=("team2_prod",),
        submit_url="http://host.docker.internal:8000/game/flag/submit",
        mcp_endpoint="http://host.docker.internal:8000/mcp/",
    )
    for k, v in overrides.items():
        base = type(base)(**{**base.__dict__, k: v})
    return base


# ── Attack dispatch ────────────────────────────────────────────────────


def test_attack_dispatch_notes_uses_notes_protocol():
    p = build_attack_prompt(_cfg(), service_name="notes")
    # Notes fingerprints: LIST/GET commands, port 5000.
    assert 'Send: "LIST' in p
    assert 'Send: "GET' in p
    assert "port 5000" in p
    # ico-specific opcodes must NOT appear.
    assert "\\x10" not in p or "binary protocol" not in p.lower()
    assert "4265" not in p


def test_attack_dispatch_nautilus_ico_uses_binary_protocol():
    p = build_attack_prompt(_cfg(), service_name="nautilus-ico")
    # ico fingerprints: binary opcodes, port 4265, PNG tEXt md5 leak idea.
    assert "4265" in p
    assert "0x10" in p   # CONNECT opcode in the protocol crib (hex form)
    assert "tEXt" in p   # PNG chunk extraction hint
    assert "md5" in p.lower()
    # Notes commands must NOT appear.
    assert 'Send: "LIST' not in p
    assert 'Send: "GET' not in p
    # Submission discipline must be present (the failure mode from the
    # first live run was a stream of UNKNOWN guesses).
    assert "UNKNOWN" in p
    assert "HIGH confidence" in p


def test_attack_dispatch_unknown_and_none_fall_back_to_notes():
    """Both None and an unrecognized service_name fall back to the notes
    recipe (M5.6 back-compat: callers that don't pass service_name keep
    working)."""
    p_notes = build_attack_prompt(_cfg(), service_name="notes")
    p_unknown = build_attack_prompt(_cfg(), service_name="picoctf-foo")
    p_none = build_attack_prompt(_cfg(), service_name=None)
    for p in (p_unknown, p_none):
        assert 'Send: "LIST' in p, "fallback path must reach the notes recipe"
    assert 'Send: "LIST' in p_notes


# ── Defense dispatch ───────────────────────────────────────────────────


def test_defense_dispatch_nautilus_ico_uses_ico_specific_prompt():
    """ico module exports `build_defense_prompt`; dispatcher must pick it
    over the generic fallback. Fingerprint: mentions the Pascal source
    file layout."""
    p = build_defense_prompt(_cfg(), service_name="nautilus-ico", port=4265)
    assert "ico" in p.lower()
    # Pascal source files mentioned in the ico defense crib:
    assert "server.pas" in p
    assert "image.pas" in p
    # Common defense vocabulary still present.
    assert "restart_service" in p
    assert "report_deploy_complete" in p


def test_defense_dispatch_unknown_uses_generic_fallback():
    """Generic defense recipe is service-agnostic — works for any
    FILE_AT_PATH challenge whose module doesn't override defense."""
    p = build_defense_prompt(_cfg(), service_name="picoctf-foo", port=9000)
    assert "picoctf-foo" in p
    assert "restart_service" in p
    assert "9000" in p
    # Ico-specific Pascal mentions must NOT leak into the generic fallback.
    assert "server.pas" not in p
    assert "Pascal" not in p


# ── Smoke: prompt body still substitutes config fields ────────────────


def test_attack_prompt_substitutes_team_and_opposing():
    p = build_attack_prompt(_cfg(team_id="team5", opposing_prod_hosts=("team6_prod",)), service_name="nautilus-ico")
    assert "team5" in p
    assert "team6_prod" in p


def test_defense_prompt_substitutes_port():
    p = build_defense_prompt(_cfg(), service_name="nautilus-ico", port=12345)
    assert "12345" in p
