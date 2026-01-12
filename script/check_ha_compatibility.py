#!/usr/bin/env python3
"""Check dependency compatibility with Home Assistant constraints.

This script verifies that the integration's requirements are compatible
with Home Assistant's package constraints. It helps prevent dependency
conflicts when users install this integration alongside Home Assistant.

Features:
- Forward check: Verify requirements work with latest/specific HA version
- Backward check: Verify declared minimum HA version satisfies all requirements
- Find minimum: Discover the oldest HA version that satisfies all requirements

Usage:
    python script/check_ha_compatibility.py [--ha-version VERSION] [--verbose]
    python script/check_ha_compatibility.py --find-minimum
    python script/check_ha_compatibility.py --check-declared-minimum

Examples:
    # Check against latest HA version
    python script/check_ha_compatibility.py

    # Check against specific HA version
    python script/check_ha_compatibility.py --ha-version 2025.1.0

    # Find the minimum HA version that satisfies all requirements
    python script/check_ha_compatibility.py --find-minimum

    # Verify the declared minimum in manifest.json is correct
    python script/check_ha_compatibility.py --check-declared-minimum
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
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
    CYAN = "\033[96m"
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


def fetch_ha_versions_from_pypi() -> list[str]:
    """Fetch all Home Assistant versions from PyPI.

    Returns list of stable versions (YYYY.M.P format) sorted newest to oldest.
    """
    url = "https://pypi.org/pypi/homeassistant/json"
    req = urllib.request.Request(url, headers={"User-Agent": "ha-compat-check/1.0"})

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"{Colors.RED}Error fetching HA versions from PyPI: {e}{Colors.RESET}")
        return []

    versions = list(data.get("releases", {}).keys())

    # Filter to stable releases only (YYYY.M.P format, no beta/dev)
    stable_pattern = re.compile(r"^\d{4}\.\d+\.\d+$")
    stable_versions = [v for v in versions if stable_pattern.match(v)]

    # Sort by version (newest first)
    def version_key(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.split("."))

    stable_versions.sort(key=version_key, reverse=True)

    return stable_versions


def fetch_ha_constraints(
    ha_version: str | None = None,
    silent: bool = False,
) -> dict[str, str] | None:
    """Fetch Home Assistant package constraints from GitHub.

    Returns None if the version doesn't exist or constraints can't be fetched.
    """
    if ha_version:
        urls = [
            f"https://raw.githubusercontent.com/home-assistant/core/{ha_version}/homeassistant/package_constraints.txt",
        ]
    else:
        urls = [
            "https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/package_constraints.txt"
        ]

    headers = {"User-Agent": "ha-compat-check/1.0"}

    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                content = response.read().decode("utf-8")
                break
        except urllib.error.HTTPError as e:
            if e.code == 404 and ha_version:
                # Version tag doesn't exist
                return None
            if not silent:
                print(f"{Colors.YELLOW}Warning: Could not fetch {url}: {e}{Colors.RESET}")
            continue
        except urllib.error.URLError as e:
            if not silent:
                print(f"{Colors.YELLOW}Warning: Network error fetching {url}: {e}{Colors.RESET}")
            continue
    else:
        if not silent:
            print(f"{Colors.RED}Error: Could not fetch HA constraints{Colors.RESET}")
        return None

    constraints: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, spec = parse_requirement(line)
        if name:
            constraints[name] = spec

    return constraints


def load_manifest(manifest_path: Path) -> dict:
    """Load manifest.json."""
    with open(manifest_path) as f:
        return json.load(f)


def load_manifest_requirements(manifest_path: Path) -> list[str]:
    """Load requirements from manifest.json."""
    manifest = load_manifest(manifest_path)
    return manifest.get("requirements", [])


def get_declared_ha_minimum(manifest_path: Path) -> str | None:
    """Get the declared minimum HA version from manifest.json."""
    manifest = load_manifest(manifest_path)
    return manifest.get("homeassistant")


def check_requirements_against_constraints(
    requirements: Sequence[str],
    ha_constraints: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Check if requirements are compatible with HA constraints.

    Returns:
        Tuple of (errors, warnings) - empty lists mean compatible
    """
    errors: list[str] = []
    warnings: list[str] = []

    for req in requirements:
        name, our_spec = parse_requirement(req)
        if not name:
            continue

        # Try both underscore and hyphen versions
        ha_spec = ha_constraints.get(name, ha_constraints.get(name.replace("_", "-"), ""))

        if ha_spec:
            ha_pinned = get_pinned_version(ha_spec)
            our_min = get_min_version(our_spec)

            if ha_pinned and our_min:
                if ha_pinned < our_min:
                    errors.append(
                        f"{name}: HA pins {ha_pinned}, we require >={our_min}"
                    )
            elif ha_pinned and our_spec:
                # Check if HA's pinned version satisfies our specifier
                try:
                    spec_set = SpecifierSet(our_spec)
                    if not spec_set.contains(ha_pinned):
                        errors.append(
                            f"{name}: HA pins {ha_pinned}, incompatible with {our_spec}"
                        )
                except Exception:
                    warnings.append(f"{name}: Could not parse specifier {our_spec}")

    return errors, warnings


def check_compatibility(
    requirements: Sequence[str],
    ha_constraints: dict[str, str],
    verbose: bool = False,
) -> tuple[list[str], list[str]]:
    """Check compatibility of requirements against HA constraints with output.

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


def find_minimum_ha_version(
    requirements: Sequence[str],
    verbose: bool = False,
    max_versions_to_check: int = 50,
) -> tuple[str | None, dict[str, str]]:
    """Find the oldest HA version that satisfies all requirements.

    Dynamically fetches versions from PyPI and checks backwards.

    Returns:
        Tuple of (version, blocking_requirements) where blocking_requirements
        shows which packages block older versions.
    """
    print(f"\n{Colors.BOLD}Finding Minimum Compatible HA Version{Colors.RESET}")
    print("=" * 70)

    print("Fetching HA versions from PyPI...", end=" ")
    all_versions = fetch_ha_versions_from_pypi()
    if not all_versions:
        print(f"{Colors.RED}failed{Colors.RESET}")
        return None, {}
    print(f"found {len(all_versions)} stable releases")

    # Limit how far back we search
    versions_to_check = all_versions[:max_versions_to_check]
    print(f"Checking last {len(versions_to_check)} versions (newest to oldest)...")

    minimum_version: str | None = None
    last_compatible: str | None = None
    blocking_reqs: dict[str, str] = {}

    # Test versions from newest to oldest
    for ha_version in versions_to_check:
        if verbose:
            print(f"  Testing HA {ha_version}...", end=" ", flush=True)

        constraints = fetch_ha_constraints(ha_version, silent=True)
        if constraints is None:
            if verbose:
                print(f"{Colors.YELLOW}constraints not found{Colors.RESET}")
            continue

        errors, _ = check_requirements_against_constraints(requirements, constraints)

        if errors:
            if verbose:
                print(f"{Colors.RED}incompatible{Colors.RESET}")
                for err in errors:
                    print(f"      {err}")
            # Record what blocked this version
            if last_compatible:
                for err in errors:
                    pkg = err.split(":")[0]
                    if pkg not in blocking_reqs:
                        blocking_reqs[pkg] = f"requires HA >= {last_compatible}"
        else:
            if verbose:
                print(f"{Colors.GREEN}compatible{Colors.RESET}")
            last_compatible = ha_version
            minimum_version = ha_version

    return minimum_version, blocking_reqs


def check_declared_minimum(
    manifest_path: Path,
    verbose: bool = False,
) -> int:
    """Check if the declared minimum HA version is correct.

    Returns exit code (0 = OK, 1 = error)
    """
    print(f"{Colors.BOLD}Checking Declared Minimum HA Version{Colors.RESET}")
    print("=" * 70)

    declared_min = get_declared_ha_minimum(manifest_path)
    requirements = load_manifest_requirements(manifest_path)

    if not declared_min:
        print(f"{Colors.YELLOW}Warning: No 'homeassistant' field in manifest.json{Colors.RESET}")
        print("Consider adding a minimum HA version requirement.")
        print()

        # Find what it should be
        min_version, blocking = find_minimum_ha_version(requirements, verbose)
        if min_version:
            print(f"\n{Colors.CYAN}Suggested minimum: {min_version}{Colors.RESET}")
            if blocking:
                print("\nBlocking requirements:")
                for pkg, reason in blocking.items():
                    print(f"  - {pkg}: {reason}")
            print(f'\nAdd to manifest.json: "homeassistant": "{min_version}"')
        return 1

    print(f"Declared minimum: {Colors.BLUE}{declared_min}{Colors.RESET}")

    # Fetch constraints for the declared minimum
    constraints = fetch_ha_constraints(declared_min)
    if constraints is None:
        print(f"{Colors.RED}Error: Could not fetch constraints for HA {declared_min}{Colors.RESET}")
        return 1

    print(f"Loaded {len(constraints)} constraints from HA {declared_min}")

    # Check compatibility
    errors, _ = check_requirements_against_constraints(requirements, constraints)

    if errors:
        print(f"\n{Colors.RED}{Colors.BOLD}ERROR: Declared minimum {declared_min} is too old!{Colors.RESET}")
        print(f"\nThe following requirements are NOT satisfied by HA {declared_min}:")
        for err in errors:
            print(f"  {Colors.RED}x{Colors.RESET} {err}")

        # Find correct minimum
        print(f"\n{Colors.CYAN}Searching for correct minimum...{Colors.RESET}")
        min_version, blocking = find_minimum_ha_version(requirements, verbose=False)
        if min_version:
            print(f"\n{Colors.GREEN}Correct minimum should be: {min_version}{Colors.RESET}")
            print(f'\nUpdate manifest.json: "homeassistant": "{min_version}"')
        return 1

    print(f"\n{Colors.GREEN}{Colors.BOLD}OK: Declared minimum {declared_min} is valid{Colors.RESET}")
    print("All requirements are satisfied by this HA version.")

    # Optionally check if we can go lower
    if verbose:
        print(f"\n{Colors.CYAN}Checking if minimum can be lowered...{Colors.RESET}")
        min_version, _ = find_minimum_ha_version(requirements, verbose=False)
        if min_version and min_version != declared_min:
            try:
                declared_v = Version(declared_min)
                min_v = Version(min_version)
                if min_v < declared_v:
                    print(f"Note: Minimum could potentially be lowered to {min_version}")
            except InvalidVersion:
                pass

    return 0


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Check dependency compatibility with Home Assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          Check against latest HA (dev branch)
  %(prog)s --ha-version 2025.1.0    Check against specific version
  %(prog)s --find-minimum           Find oldest compatible HA version
  %(prog)s --check-declared-minimum Verify manifest.json minimum is correct
        """,
    )
    parser.add_argument(
        "--ha-version",
        help="Home Assistant version to check against (default: dev branch)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show all packages and detailed output",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("custom_components/googlefindmy/manifest.json"),
        help="Path to manifest.json",
    )
    parser.add_argument(
        "--find-minimum",
        action="store_true",
        help="Find the oldest HA version that satisfies all requirements",
    )
    parser.add_argument(
        "--check-declared-minimum",
        action="store_true",
        help="Verify the declared minimum HA version in manifest.json is correct",
    )
    parser.add_argument(
        "--max-versions",
        type=int,
        default=50,
        help="Maximum number of versions to check when finding minimum (default: 50)",
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

    # Handle different modes
    if args.check_declared_minimum:
        return check_declared_minimum(args.manifest, args.verbose)

    if args.find_minimum:
        requirements = load_manifest_requirements(args.manifest)
        print(f"Manifest: {args.manifest}")
        print(f"Found {len(requirements)} integration requirements")

        min_version, blocking = find_minimum_ha_version(
            requirements, args.verbose, args.max_versions
        )

        print()
        if min_version:
            print(f"{Colors.GREEN}{Colors.BOLD}Minimum compatible HA version: {min_version}{Colors.RESET}")
            if blocking:
                print("\nRequirements that determine the minimum:")
                for pkg, reason in blocking.items():
                    print(f"  - {pkg}: {reason}")

            # Check current declaration
            declared = get_declared_ha_minimum(args.manifest)
            if declared:
                try:
                    declared_v = Version(declared)
                    min_v = Version(min_version)
                    if declared_v < min_v:
                        print(f"\n{Colors.RED}Warning: Declared minimum ({declared}) is too old!{Colors.RESET}")
                        print(f'Update manifest.json: "homeassistant": "{min_version}"')
                        return 1
                    elif declared_v > min_v:
                        print(f"\n{Colors.CYAN}Note: Declared minimum ({declared}) could be lowered to {min_version}{Colors.RESET}")
                except InvalidVersion:
                    pass
            else:
                print(f'\n{Colors.CYAN}Suggestion: Add to manifest.json: "homeassistant": "{min_version}"{Colors.RESET}')
        else:
            print(f"{Colors.RED}Could not determine minimum HA version{Colors.RESET}")
            return 1
        return 0

    # Standard mode: check against specific or latest version
    ha_version_str = args.ha_version or "latest (dev branch)"
    print(f"Checking against Home Assistant: {Colors.BLUE}{ha_version_str}{Colors.RESET}")
    print(f"Manifest: {args.manifest}")

    ha_constraints = fetch_ha_constraints(args.ha_version)
    if ha_constraints is None:
        return 1

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
