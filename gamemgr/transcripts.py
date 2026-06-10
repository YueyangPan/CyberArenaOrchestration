"""Parse agent stream-json transcripts from a run's artifact dir.

Each per-phase agent invocation writes `stream-{phase}.jsonl` (the raw
CLI stream-json) under `runs/<ts>/artifacts/<team>/round-<N>/`. This
module turns those files into structured cards the panel renders below
the timeline:

  ⚙ system · 🧠 thinking · 💬 text · 🔧 tool_use · ↩ tool_result

Adapted from the offline `vis/logging.py` transcript renderer, minus the
shared-timeline offset interpolation — these cards render in file order
as a per-(team, round, phase) stack, so wall-clock alignment isn't
needed. Stdlib-only; consumed by `gamemgr/api.py::/game/agents`.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class AgentEvent:
    """One flattened event from a stream-json line."""

    kind: str  # system | thinking | text | tool_use | tool_result
    title: str  # one-line summary (the card header)
    body: str  # full content (collapsible in the panel)
    tool_name: str | None  # set on tool_use
    is_error: bool  # tool_result with is_error
    tokens_in: int | None
    tokens_out: int | None


def _truncate(s: str, n: int = 140) -> str:
    s = s.strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _flatten_msg_content(item: dict) -> list[dict]:
    """A line's `message.content` can carry several typed blocks (a
    thinking block then a tool_use, etc.); walk them out."""
    msg = item.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            return [c for c in content if isinstance(c, dict)]
    return []


def _unpack_line(obj: dict) -> list[AgentEvent]:
    """Translate one stream-json line into 0..N flat AgentEvents."""
    out: list[AgentEvent] = []
    top = obj.get("type")

    if top == "system" and obj.get("subtype") == "init":
        model = obj.get("model", "?")
        tools = obj.get("tools") or []
        out.append(AgentEvent(
            kind="system",
            title=f"session init · model={model} · {len(tools)} tools",
            body=json.dumps(obj, indent=2),
            tool_name=None, is_error=False, tokens_in=None, tokens_out=None,
        ))
        return out

    if top == "assistant":
        for c in _flatten_msg_content(obj):
            ctype = c.get("type")
            if ctype == "thinking":
                text = c.get("thinking", "")
                out.append(AgentEvent(
                    "thinking", _truncate(text), text, None, False, None, None,
                ))
            elif ctype == "text":
                text = c.get("text", "")
                out.append(AgentEvent(
                    "text", _truncate(text), text, None, False, None, None,
                ))
            elif ctype == "tool_use":
                name = c.get("name", "?")
                ti = c.get("input") or {}
                summary = (
                    ti.get("description") or ti.get("command")
                    or ti.get("file_path") or ti.get("pattern") or ""
                )
                title = f"{name}: {_truncate(str(summary), 120)}" if summary else name
                out.append(AgentEvent(
                    "tool_use", title, json.dumps(ti, indent=2), name, False, None, None,
                ))
        # `usage` is per-turn, not per-block — attach it to just the first
        # event so summing tokens across events doesn't multiply-count one
        # turn's usage by its block count.
        if out:
            usage = (obj.get("message") or {}).get("usage") or {}
            out[0].tokens_in = usage.get("input_tokens")
            out[0].tokens_out = usage.get("output_tokens")
        return out

    if top == "user":
        for c in _flatten_msg_content(obj):
            if c.get("type") != "tool_result":
                continue
            content = c.get("content", "")
            # tool_result.content is a list-of-blocks or a plain string
            # depending on the tool; coerce to a string body.
            if isinstance(content, list):
                content = "\n".join(
                    blk.get("text", "") if isinstance(blk, dict) else str(blk)
                    for blk in content
                )
            content_str = str(content)
            is_err = bool(c.get("is_error"))
            out.append(AgentEvent(
                "tool_result",
                ("ERR " if is_err else "") + _truncate(content_str, 120),
                content_str, None, is_err, None, None,
            ))
        return out

    return out


def parse_stream_json(path: Path) -> list[AgentEvent]:
    """Parse one `stream-*.jsonl` file into flat AgentEvents, in file
    order. Malformed lines are skipped (the CLI occasionally writes a
    partial line on shutdown)."""
    events: list[AgentEvent] = []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.extend(_unpack_line(obj))
    return events


def summary_stats(events: list[AgentEvent]) -> dict:
    """Per-transcript headline shown in the panel's `<summary>`."""
    tool_counts: dict[str, int] = {}
    tin = tout = 0
    n_err = 0
    for ev in events:
        if ev.kind == "tool_use" and ev.tool_name:
            tool_counts[ev.tool_name] = tool_counts.get(ev.tool_name, 0) + 1
        if ev.is_error:
            n_err += 1
        if ev.tokens_in:
            tin += ev.tokens_in
        if ev.tokens_out:
            tout += ev.tokens_out
    return {
        "events": len(events),
        "tools": sum(tool_counts.values()),
        "tools_breakdown": ", ".join(
            f"{n}×{name}" for name, n in sorted(tool_counts.items(), key=lambda kv: -kv[1])
        ) or "(none)",
        "errors": n_err,
        "tokens_in": tin,
        "tokens_out": tout,
    }


_STREAM_RE = re.compile(r"^stream-(?P<phase>.+)\.jsonl$")
_ROUND_RE = re.compile(r"^round-(?P<round>\d+)$")
# Phases run defense → attack within a round; a plain string sort would
# put "attack" before "defense". Rank so transcripts list chronologically.
_PHASE_RANK = {"defense": 0, "attack": 1}

# Parsed-result cache keyed by run dir → ((path, mtime_ns, size) signature,
# transcripts). The panel polls collect_transcripts every few seconds and
# transcripts are append-once at phase end, so most polls hit the cache and
# skip re-reading/parsing thousands of JSONL lines.
_CACHE: dict[str, tuple[tuple, list[dict]]] = {}


def collect_transcripts(run_dir: Path | str) -> list[dict]:
    """Walk `runs/<ts>/artifacts/<team>/round-<N>/stream-<phase>.jsonl`
    and return one transcript dict per non-empty file, sorted by
    (team_id, round_id, phase). Each dict is JSON-ready for /game/agents.
    """
    root = Path(run_dir) / "artifacts"
    if not root.is_dir():
        return []
    paths = sorted(root.glob("*/round-*/stream-*.jsonl"))

    # Cheap change-signature: stat (not read) each file. Re-parse only when
    # a file appears or changes; otherwise return the cached result.
    sig_list = []
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        sig_list.append((str(p), st.st_mtime_ns, st.st_size))
    sig = tuple(sig_list)
    cached = _CACHE.get(str(run_dir))
    if cached is not None and cached[0] == sig:
        return cached[1]

    out: list[dict] = []
    for stream_path in paths:
        m = _STREAM_RE.match(stream_path.name)
        rm = _ROUND_RE.match(stream_path.parent.name)
        if not m or not rm:
            continue
        events = parse_stream_json(stream_path)
        if not events:
            continue
        out.append({
            "team_id": stream_path.parent.parent.name,
            "round_id": int(rm.group("round")),
            "phase": m.group("phase"),
            "stats": summary_stats(events),
            "events": [asdict(e) for e in events],
        })
    out.sort(key=lambda t: (t["team_id"], t["round_id"], _PHASE_RANK.get(t["phase"], 99)))
    _CACHE[str(run_dir)] = (sig, out)
    return out
