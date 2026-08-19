#!/usr/bin/env python3
# script/vendor_leaflet.py
"""Keep the vendored Leaflet copy in step with the pinned npm version.

Why this exists
---------------
`custom_components/googlefindmy/map_view.py` embeds Leaflet instead of loading it
from a CDN. A vendored library ages silently: Dependabot reads manifests, never
checked-in files, so a bump of the `leaflet` devDependency in `package.json`
would leave the actual copy behind without anything going red.

Two modes
---------
`--check` (what CI runs, no network, no Node):
  * the version in `package.json` matches the one recorded in `VERSION`
  * the digests in `VERSION` match the files actually checked in

default (what a developer runs after a Dependabot bump):
  * copy `node_modules/leaflet/dist/{leaflet.js,leaflet.css}` and `LICENSE` into
    the vendor directory and rewrite `VERSION` with fresh digests
  * requires `npm install` to have run first

Exit codes: 0 = in step / updated, 1 = drift found, 2 = usage or environment error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "custom_components" / "googlefindmy" / "vendor" / "leaflet"
NODE_DIST = REPO_ROOT / "node_modules" / "leaflet" / "dist"
NODE_LICENSE = REPO_ROOT / "node_modules" / "leaflet" / "LICENSE"
ASSETS = ("leaflet.js", "leaflet.css")

_VERSION_LINE = re.compile(r"^leaflet (?P<version>\S+)$", re.M)
_DIGEST_LINE = re.compile(
    r"^sha256 (?P<name>\S+)\s*=\s*(?P<digest>[0-9a-f]{64})$", re.M
)


def pinned_version() -> str:
    """Return the `leaflet` version pinned in package.json devDependencies."""

    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    version = package.get("devDependencies", {}).get("leaflet")
    if not isinstance(version, str) or not version:
        raise SystemExit("package.json has no `leaflet` devDependency")
    return version.lstrip("^~")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recorded() -> tuple[str, dict[str, str]]:
    """Return (version, {asset: digest}) as recorded in VERSION."""

    text = (VENDOR_DIR / "VERSION").read_text(encoding="utf-8")
    match = _VERSION_LINE.search(text)
    if match is None:
        raise SystemExit(f"{VENDOR_DIR / 'VERSION'} has no `leaflet <version>` line")
    digests = {m["name"]: m["digest"] for m in _DIGEST_LINE.finditer(text)}
    return match["version"], digests


def write_version(version: str, digests: dict[str, str]) -> None:
    lines = [
        f"leaflet {version}",
        "license: BSD-2-Clause (see LICENSE)",
        f"source: https://registry.npmjs.org/leaflet/-/leaflet-{version}.tgz (dist/)",
    ]
    lines += [f"sha256 {name:<11} = {digests[name]}" for name in ASSETS]
    lines += [
        "",
        "Written by script/vendor_leaflet.py from node_modules/leaflet/dist.",
        "The map page embeds these two files inline; it does not register a static",
        "path and it does not fetch anything from a CDN. Only leaflet.js and",
        "leaflet.css are vendored: the map draws with L.circleMarker and uses no",
        "layers control, so the image assets those CSS rules reference are never",
        "requested.",
        "",
    ]
    (VENDOR_DIR / "VERSION").write_text("\n".join(lines), encoding="utf-8")


def check() -> int:
    problems: list[str] = []
    want_version = pinned_version()
    have_version, have_digests = recorded()

    if want_version != have_version:
        problems.append(
            f"package.json pins leaflet {want_version}, "
            f"but {VENDOR_DIR.name}/VERSION records {have_version}"
        )

    for name in ASSETS:
        path = VENDOR_DIR / name
        if not path.is_file():
            problems.append(f"missing vendored asset: {path}")
            continue
        actual = digest(path)
        expected = have_digests.get(name)
        if expected is None:
            problems.append(f"VERSION records no digest for {name}")
        elif actual != expected:
            problems.append(
                f"{name} does not match its recorded digest "
                f"(recorded {expected[:12]}…, actual {actual[:12]}…)"
            )

    if not (VENDOR_DIR / "LICENSE").is_file():
        problems.append(f"missing {VENDOR_DIR / 'LICENSE'} (BSD-2-Clause attribution)")

    if problems:
        print("Vendored Leaflet copy is out of step:")
        for problem in problems:
            print(f"  - {problem}")
        print()
        print(
            "Run `npm install && python script/vendor_leaflet.py` and commit the result."
        )
        return 1

    print(f"Vendored Leaflet {have_version} matches package.json and its digests.")
    return 0


def installed_version() -> str | None:
    """Return the version of the Leaflet package actually present in node_modules."""

    package_json = NODE_DIST.parent / "package.json"
    if not package_json.is_file():
        return None
    installed = json.loads(package_json.read_text(encoding="utf-8")).get("version")
    return installed if isinstance(installed, str) else None


def update() -> int:
    if not NODE_DIST.is_dir():
        print(f"{NODE_DIST} not found; run `npm install` first.", file=sys.stderr)
        return 2

    version = pinned_version()

    # Stamping the pinned version onto whatever happens to sit in node_modules
    # would produce a copy that passes --check while carrying the previous
    # release, which is precisely the drift this script exists to prevent.
    present = installed_version()
    if present != version:
        print(
            f"node_modules carries leaflet {present or 'an unknown version'}, "
            f"but package.json pins {version}; run `npm install` first.",
            file=sys.stderr,
        )
        return 2

    if not NODE_LICENSE.is_file():
        print(
            f"{NODE_LICENSE} not found; refusing to keep the previous licence file "
            "next to new sources (BSD-2-Clause attribution).",
            file=sys.stderr,
        )
        return 2
    digests: dict[str, str] = {}
    for name in ASSETS:
        source = NODE_DIST / name
        if not source.is_file():
            print(f"missing {source}", file=sys.stderr)
            return 2
        shutil.copyfile(source, VENDOR_DIR / name)
        digests[name] = digest(VENDOR_DIR / name)

    shutil.copyfile(NODE_LICENSE, VENDOR_DIR / "LICENSE")

    write_version(version, digests)
    print(f"Vendored Leaflet {version} from {NODE_DIST}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify only; do not touch the working tree (what CI runs)",
    )
    args = parser.parse_args(argv)
    return check() if args.check else update()


if __name__ == "__main__":
    raise SystemExit(main())
