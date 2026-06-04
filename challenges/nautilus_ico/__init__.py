"""DEFCON nautilus-ico challenge — Pascal binary, FILE_AT_PATH flags.

Composition under `challenges.interface.Challenge`:
  vulbox             — cyberarena/chal-nautilus-ico:<pin> (built by
                       NautilusSource in v1; Stage 9 swaps to a direct
                       VulboxImage(build_context=...) build)
  functionality_test — 4-level chain (process / tcp / protocol / checker);
                       PROTOCOL has no protocol adapter so aliases to
                       CHECKER's `/arena/checker.sh` outcome
  flag_handler       — FILE_AT_PATH: docker exec writes `/flag`, reads via
                       `cat /flag` (own-team verify) or over TCP from a
                       victim's prod (cross-team attack)
  restart_handler    — `docker exec /arena/restart.sh` then verify port 4265
                       is back up; emits a 3-leaf CheckResult (compile /
                       spawn / smoke)
  docs               — intro + func_test_spec; agent_prompts wired in Stage 6

Public surface is the `IcoChallenge` class.
"""

from .challenge import IcoChallenge

__all__ = ["IcoChallenge"]
