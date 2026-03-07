#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import sys
from pathlib import Path

# When run as `python main.py` from the googlefindmy/ directory, the repo root
# must be on sys.path so that internal `custom_components.googlefindmy.*`
# imports (used throughout the codebase for HA compatibility) can resolve.
_repo_root = str(Path(__file__).resolve().parents[2])
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from NovaApi.ListDevices.nbe_list_devices import list_devices  # noqa: E402

if __name__ == "__main__":
    list_devices()
