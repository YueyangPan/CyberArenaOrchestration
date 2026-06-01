"""Static panel snapshot writer.

After the round loop ends, embed the run's `events.jsonl` into a copy
of `panel/panel.html` and write it to `runs/<ts>/panel.html`. The
result is a self-contained file that re-renders the final state of
the live panel — no server, no WebSocket, no `/game/status` fetch.

The trick is on the panel side: if `window.INITIAL_EVENTS` is defined
(an array), the boot block replays it and skips the live connection.
Here we just inject one `<script>` tag right before `</body>`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_PANEL_TEMPLATE = Path(__file__).parent.parent / "panel" / "panel.html"


def write_snapshot(run_dir: Path, out_path: Path | None = None) -> Path:
    """Read `run_dir/events.jsonl`, embed as INITIAL_EVENTS into a
    snapshot copy of `panel.html`, write to `out_path` (defaults to
    `run_dir/panel.html`).

    Returns the path written. Best-effort on the events side: any line
    that's not valid JSON is skipped with a warning. Missing
    `events.jsonl` is OK — snapshot still writes with an empty array,
    so the user can open the file and see "no events captured".
    """
    if out_path is None:
        out_path = run_dir / "panel.html"

    events: list[dict] = []
    events_path = run_dir / "events.jsonl"
    if events_path.is_file():
        for i, line in enumerate(events_path.read_text().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("panel_snapshot: skipping malformed line %d", i)
    else:
        log.warning("panel_snapshot: %s missing; writing empty snapshot", events_path)

    template = _PANEL_TEMPLATE.read_text()
    if "</body>" not in template:
        raise RuntimeError(
            f"panel template at {_PANEL_TEMPLATE} missing </body>; cannot inject"
        )

    # `json.dumps` handles HTML-safe escaping for the data values; we
    # additionally close any `</script>` literal that could appear in a
    # payload string. (Browsers terminate inline <script> on the literal
    # bytes `</script>` regardless of JSON quoting.)
    payload = json.dumps(events).replace("</", "<\\/")
    injection = (
        '<script id="snapshot-events">\n'
        f"window.INITIAL_EVENTS = {payload};\n"
        "</script>\n"
    )
    snapshot = template.replace("</body>", injection + "</body>", 1)

    out_path.write_text(snapshot)
    log.info("panel snapshot written: %s (%d events)", out_path, len(events))
    return out_path
