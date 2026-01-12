#!/usr/bin/env python3
"""Check dependency compatibility with Home Assistant constraints.

This script verifies that the integration's requirements are compatible
with Home Assistant's package constraints. It helps prevent dependency
conflicts when users install this integration alongside Home Assistant.

Usage:
    python script/check_ha_compatibility.py [--ha-version VERSION] [--verbose]

Examples:
    # Check against latest HA version
    python script/check_ha_compatibility.py

    # Check against specific HA version
    python script/check_ha_compatibility.py --ha-version 2025.1.0

    # Verbose output with all package comparisons
    python script/check_ha_compatibility.py --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

try:
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version
except ImportError:
    print("Error: 'packaging' library required. Install with: pip install packaging")
    sys.exit(1)


# ANSI color codes
class Colors:
    """ANSI color codes for terminal output."""

    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


def parse_requirement(req_str: str) -> tuple[str | None, str]:
    """Parse a requirement string into (name, specifier)."""
    match = re.match(r"^([a-zA-Z0-9_-]+)\s*(.*)$", req_str.strip())
    if match:
        name = match.group(1).lower().replace("-", "_")
        specifier = match.group(2).strip()
        return name, specifier
    return None, ""


def get_pinned_version(specifier_str: str) -> Version | None:
    """Extract pinned version from specifier like ==1.0.0."""
    if not specifier_str:
        return None
    match = re.search(r"==\s*([0-9][0-9a-zA-Z._-]*)", specifier_str)
    if match:
        try:
            return Version(match.group(1))
        except InvalidVersion:
            return None
    return None


def get_min_version(specifier_str: str) -> Version | None:
    """Extract minimum version from a specifier like >=1.0.0."""
    if not specifier_str:
        return None
    match = re.search(r">=\s*([0-9][0-9a-zA-Z._-]*)", specifier_str)
    if match:
        try:
            return Version(match.group(1))
        except InvalidVersion:
            return None
    return None


def fetch_ha_constraints(ha_version: str | None = None) -> dict[str, str]:
    """Fetch Home Assistant package constraints from GitHub."""
    if ha_version:
        # Try tag first, then dev branch
        urls = [
            f"https://raw.githubusercontent.com/home-assistant/core/{ha_version}/homeassistant/package_constraints.txt",
            "https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/package_constraints.txt",
        ]
    else:
        urls = [
            "https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/package_constraints.txt"
        ]

    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                content = response.read().decode("utf-8")
                break
        except urllib.error.HTTPError:
            continue
    else:
        print(f"{Colors.RED}Error: Could not fetch HA constraints{Colors.RESET}")
        sys.exit(1)

    constraints: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, spec = parse_requirement(line)
        if name:
            constraints[name] = spec

    return constraints


def load_manifest_requirements(manifest_path: Path) -> list[str]:
    """Load requirements from manifest.json."""
    with open(manifest_path) as f:
        manifest = json.load(f)
    return manifest.get("requirements", [])


def check_compatibility(
    requirements: Sequence[str],
    ha_constraints: dict[str, str],
    verbose: bool = False,
) -> tuple[list[str], list[str]]:
    """Check compatibility of requirements against HA constraints.

    Returns:
        Tuple of (errors, warnings)
    """
    errors: list[str] = []
    warnings: list[str] = []

    print(f"\n{Colors.BOLD}Dependency Compatibility Check{Colors.RESET}")
    print("=" * 70)
    print(
        f"{'Package':<25} {'Our Requirement':<18} {'HA Constraint':<18} {'Status'}"
    )
    print("-" * 70)

    for req in requirements:
        name, our_spec = parse_requirement(req)
        if not name:
            continue

        # Try both underscore and hyphen versions
        ha_spec = ha_constraints.get(name, ha_constraints.get(name.replace("_", "-"), ""))

        status = f"{Colors.GREEN}OK{Colors.RESET}"
        status_detail = ""

        if ha_spec:
            ha_pinned = get_pinned_version(ha_spec)
            our_min = get_min_version(our_spec)

            if ha_pinned and our_min:
                if ha_pinned < our_min:
                    status = f"{Colors.RED}ERROR{Colors.RESET}"
                    status_detail = f"HA pins {ha_pinned}, we require >={our_min}"
                    errors.append(
                        f"{name}: Home Assistant pins version {ha_pinned}, "
                        f"but we require >={our_min}. "
                        f"Consider lowering minimum to >={ha_pinned}"
                    )
                elif ha_pinned > our_min:
                    status = f"{Colors.GREEN}OK{Colors.RESET}"
                    status_detail = f"HA {ha_pinned} >= our {our_min}"
            elif ha_pinned and our_spec:
                # Check if HA's pinned version satisfies our specifier
                try:
                    spec_set = SpecifierSet(our_spec)
                    if not spec_set.contains(ha_pinned):
                        status = f"{Colors.RED}CONFLICT{Colors.RESET}"
                        status_detail = f"HA pins {ha_pinned}, incompatible with {our_spec}"
                        errors.append(
                            f"{name}: HA pins {ha_pinned} which is incompatible "
                            f"with our requirement {our_spec}"
                        )
                except Exception:
                    status = f"{Colors.YELLOW}UNKNOWN{Colors.RESET}"
                    warnings.append(f"{name}: Could not parse specifier {our_spec}")
        elif verbose:
            status = f"{Colors.BLUE}N/A{Colors.RESET}"
            status_detail = "Not constrained by HA"

        if verbose or "OK" not in status:
            ha_display = ha_spec[:18] if ha_spec else "-"
            our_display = our_spec[:18] if our_spec else "-"
            print(f"{name:<25} {our_display:<18} {ha_display:<18} {status}")
            if status_detail:
                print(f"{'':>25} {status_detail}")

    print("-" * 70)
    return errors, warnings


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Check dependency compatibility with Home Assistant"
    )
    parser.add_argument(
        "--ha-version",
        help="Home Assistant version to check against (default: latest dev)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show all packages, not just issues",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("custom_components/googlefindmy/manifest.json"),
        help="Path to manifest.json",
    )
    args = parser.parse_args()

    # Find manifest
    if not args.manifest.exists():
        # Try relative to script location
        script_dir = Path(__file__).parent.parent
        args.manifest = script_dir / "custom_components/googlefindmy/manifest.json"

    if not args.manifest.exists():
        print(f"{Colors.RED}Error: manifest.json not found at {args.manifest}{Colors.RESET}")
        return 1

    print(f"{Colors.BOLD}Home Assistant Dependency Compatibility Checker{Colors.RESET}")
    print()

    # Fetch HA constraints
    ha_version_str = args.ha_version or "latest (dev branch)"
    print(f"Checking against Home Assistant: {Colors.BLUE}{ha_version_str}{Colors.RESET}")
    print(f"Manifest: {args.manifest}")

    ha_constraints = fetch_ha_constraints(args.ha_version)
    print(f"Loaded {len(ha_constraints)} HA package constraints")

    # Load our requirements
    requirements = load_manifest_requirements(args.manifest)
    print(f"Found {len(requirements)} integration requirements")

    # Check compatibility
    errors, warnings = check_compatibility(requirements, ha_constraints, args.verbose)

    # Summary
    print()
    if errors:
        print(f"{Colors.RED}{Colors.BOLD}FAILED:{Colors.RESET} {len(errors)} compatibility issue(s) found")
        print()
        for error in errors:
            print(f"  {Colors.RED}x{Colors.RESET} {error}")
        print()
        print("These issues may prevent the integration from working with Home Assistant.")
        print("Consider updating the version bounds in manifest.json")
        return 1
    elif warnings:
        print(f"{Colors.YELLOW}{Colors.BOLD}WARNING:{Colors.RESET} {len(warnings)} warning(s)")
        for warning in warnings:
            print(f"  {Colors.YELLOW}!{Colors.RESET} {warning}")
        return 0
    else:
        print(
            f"{Colors.GREEN}{Colors.BOLD}PASSED:{Colors.RESET} "
            f"All dependencies are compatible with Home Assistant"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
