"""Panel snapshot writer — embeds events.jsonl into a copy of panel.html.

The output is a self-contained file that re-renders the live panel
offline. These tests just verify the embedding mechanics — the panel
JS itself is exercised by opening the snapshot in a browser, not here.
"""

from __future__ import annotations

import json

from gamemgr.panel_snapshot import write_snapshot


def _read_events_from_snapshot(html: str) -> list[dict]:
    """Parse the INITIAL_EVENTS = [...] payload back out of a snapshot."""
    needle = "window.INITIAL_EVENTS = "
    start = html.index(needle) + len(needle)
    end = html.index(";\n</script>", start)
    raw = html[start:end]
    # Reverse the </ → <\/ escape we applied at write time.
    raw = raw.replace("<\\/", "</")
    return json.loads(raw)


def test_write_snapshot_creates_panel_html_with_embedded_events(tmp_path):
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        '{"id": "a", "ts": 1.0, "type": "ROUND_START", "team_id": null, "round_id": 1, "phase": "INIT", "payload": {}}\n'
        '{"id": "b", "ts": 2.0, "type": "PHASE_CHANGE", "team_id": null, "round_id": 1, "phase": "DEFENSE", "payload": {}}\n'
    )

    out = write_snapshot(tmp_path)
    assert out == tmp_path / "panel.html"
    assert out.is_file()

    html = out.read_text()
    # The boot block must be intact (we only inject one <script> tag).
    assert "window.INITIAL_EVENTS" in html
    assert "if (Array.isArray(window.INITIAL_EVENTS))" in html

    events = _read_events_from_snapshot(html)
    assert len(events) == 2
    assert events[0]["type"] == "ROUND_START"
    assert events[1]["type"] == "PHASE_CHANGE"


def test_write_snapshot_handles_missing_events_file(tmp_path):
    """Missing events.jsonl is logged and snapshot still writes
    (with empty INITIAL_EVENTS) — never let an empty run break the
    teardown path that writes this file."""
    # No events.jsonl in tmp_path
    out = write_snapshot(tmp_path)
    assert out.is_file()
    events = _read_events_from_snapshot(out.read_text())
    assert events == []


def test_write_snapshot_skips_malformed_lines(tmp_path):
    """One bad line in events.jsonl shouldn't kill the snapshot —
    skip and keep going."""
    (tmp_path / "events.jsonl").write_text(
        '{"id": "a", "ts": 1.0, "type": "ROUND_START", "team_id": null, "round_id": 1, "phase": "INIT", "payload": {}}\n'
        "this is not json\n"
        '{"id": "b", "ts": 2.0, "type": "ROUND_STOP", "team_id": null, "round_id": 1, "phase": "ATTACK", "payload": {}}\n'
    )
    out = write_snapshot(tmp_path)
    events = _read_events_from_snapshot(out.read_text())
    assert [e["type"] for e in events] == ["ROUND_START", "ROUND_STOP"]


def test_write_snapshot_escapes_script_close_tag(tmp_path):
    """Payload strings containing literal `</script>` would otherwise
    terminate the inline <script> early and break the snapshot."""
    (tmp_path / "events.jsonl").write_text(
        '{"id": "a", "ts": 1.0, "type": "FLAG_SUBMITTED", "team_id": "team1", "round_id": 1, "phase": "ATTACK", '
        '"payload": {"reason": "got </script> in payload"}}\n'
    )
    out = write_snapshot(tmp_path)
    html = out.read_text()
    # Count: original panel.html closing </script> tags must be intact,
    # the payload-embedded one must have been escaped to <\/script>.
    assert "got <\\/script> in payload" in html, (
        "</script> in event payload must be escaped to <\\/script>"
    )
    # Round-trip parse still works.
    events = _read_events_from_snapshot(html)
    assert events[0]["payload"]["reason"] == "got </script> in payload"
