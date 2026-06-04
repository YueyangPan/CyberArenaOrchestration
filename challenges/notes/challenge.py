"""NotesChallenge — composition root for the game-simple notes daemon.

A tiny TCP "notes" daemon: clients send line-terminated commands and
the server stores text. Originally ported from the reference repo's
challenge_simple NotesService; this is the v2 wrapping under the
abstract `Challenge` ABC.

Attack-only — there's no defense workflow, so `restart_handler`
stays the inherited default of `None` and the orchestrator's
DEFENSE_KICKOFF dispatch filters notes out.
"""

from __future__ import annotations

from functools import cached_property

from challenges.interface import (
    Challenge,
    ChallengeDocs,
    FlagHandler,
    FunctionalityTest,
    VulboxImage,
)

from .flag_handler import NotesFlagHandler
from .functionality_test import NotesFunctionalityTest


# The notes daemon ships in `cyberarena/prod-base` (the same generic
# image every dev/prod container is built from). Until Stage 9 bakes a
# dedicated `cyberarena/chal-notes:<pin>` image, the orchestrator's
# deploy step also docker-execs `challenges.notes._server_script` into
# the container at startup — see GameMgr.deploy_challenges.
_VULBOX_IMAGE = VulboxImage(
    reference="cyberarena/prod-base",
    exposed_ports={"service": 5000},
)


_DOCS = ChallengeDocs(
    intro=(
        "Tiny in-memory TCP notes daemon used for end-to-end orchestration "
        "demos. Newline-terminated protocol: PUT <text> stores and returns "
        "'OK <id>', GET <id> reads back the stored text or 'ERR', LIST "
        "answers 'COUNT <n>'. No vulnerable surface — attacks rely on "
        "having read access to a victim team's prod (i.e. cross-team "
        "network open during ATTACK) and submitting flags lifted via GET."
    ),
    # Stage 6 will populate this with the actual rendered prompt strings.
    # For now agents/prompts/__init__.py is still the dispatcher and reads
    # from agents/prompts/notes.py.
    agent_prompts={},
    func_test_spec=(
        "PROCESS  — the python3 daemon at /opt/cyberarena/notes/server.py "
        "is alive (pgrep).\n"
        "TCP      — port 5000 accepts a socket-connect from 127.0.0.1 "
        "inside the container.\n"
        "PROTOCOL — sending 'LIST\\n' returns a reply starting with 'COUNT '.\n"
        "CHECKER  — aliased to PROTOCOL; notes has no separable gameplay "
        "surface beyond the protocol response."
    ),
)


class NotesChallenge(Challenge):
    @property
    def name(self) -> str:
        return "notes"

    @cached_property
    def vulbox(self) -> VulboxImage:
        return _VULBOX_IMAGE

    @cached_property
    def functionality_test(self) -> FunctionalityTest:
        return NotesFunctionalityTest()

    @cached_property
    def flag_handler(self) -> FlagHandler:
        return NotesFlagHandler()

    @cached_property
    def docs(self) -> ChallengeDocs:
        return _DOCS
