# tests/fixtures/kill_probe/relay.py
"""One link of the probe chain: run the next link and relay its result.

``argv[1]`` is this link's timeout in seconds, ``argv[2:]`` the next link's
argv (an interpreter is prepended). The chain uses this file twice, as the
grandparent and as the parent: the outermost link is the one only a complete
ancestry walk can reach, the middle one buys the distance to the cleanup. Both
hand the marker down, so every link's argv carries it, which is what lets
``pgrep`` find the whole chain. The budgets are staggered by the caller so
that an inner link fires first and reports, instead of the outermost killing
the others silently.
"""

import subprocess
import sys


def main() -> int:
    """Run the next link with a budget and pass its streams and status through."""

    proc = subprocess.run(  # noqa: S603 - fixed interpreter, argv from the test
        [sys.executable, *sys.argv[2:]],
        capture_output=True,
        text=True,
        timeout=float(sys.argv[1]),
        check=False,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
