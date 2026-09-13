# tests/fixtures/kill_probe/cleanup.py
"""Innermost link of the probe chain: run the cleanup and report what it saw.

``argv[1]`` is the marker every link of the chain carries, ``argv[2]`` the pid
of the stranger that carries it too. The two lookup helpers are wrapped rather
than called a second time, so the report describes the very calls that decided
the kill: a repeat call can answer differently under load, and a report of a
different call is not evidence. The sentinels keep "never called" apart from
"called and empty": -1 for the two counts, and ``pgrep_calls`` for
``stranger_seen``, which has no unused value of its own. ``pgrep_calls`` also
pins the "very calls" claim: the wrappers record last-wins, so a second lookup
after the kill would silently rewrite ``stranger_seen`` to False.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from custom_components.googlefindmy import chrome_driver  # noqa: E402


def main() -> None:
    """Run the cleanup for the marker and print one JSON line of observations."""

    marker, stranger_pid = sys.argv[1], int(sys.argv[2])
    real_protected = chrome_driver._protected_pids
    real_pgrep = chrome_driver._pgrep_pids
    obs: dict[str, object] = {
        "protected": -1,
        "seen": -1,
        "stranger_seen": False,
        "pgrep_calls": 0,
    }
    pgrep_calls = 0

    def _protected() -> frozenset[int] | None:
        found = real_protected()
        obs["protected"] = None if found is None else len(found)
        return found

    def _pgrep(pattern: str) -> list[int]:
        nonlocal pgrep_calls
        found = real_pgrep(pattern)
        pgrep_calls += 1
        obs["pgrep_calls"] = pgrep_calls
        obs["seen"] = len(found)
        obs["stranger_seen"] = stranger_pid in found
        return found

    chrome_driver._protected_pids = _protected
    chrome_driver._pgrep_pids = _pgrep
    obs["signalled"] = chrome_driver._terminate_matching_processes(marker)
    print(json.dumps(obs))


if __name__ == "__main__":
    main()
