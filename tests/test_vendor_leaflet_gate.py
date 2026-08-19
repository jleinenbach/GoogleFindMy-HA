# tests/test_vendor_leaflet_gate.py
"""Negative-path tests for the vendored-asset freshness gate.

A gate that is only ever run against a known-good tree proves nothing: a
regression that stops collecting problems would keep CI green. Each test below
mutates one input and asserts the gate goes red for that reason.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "script" / "vendor_leaflet.py"


def _load(tmp_root: Path) -> ModuleType:
    """Load the script with its repository root pointed at a temporary copy."""

    spec = importlib.util.spec_from_file_location("vendor_leaflet_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.REPO_ROOT = tmp_root
    module.VENDOR_DIR = (
        tmp_root / "custom_components" / "googlefindmy" / "vendor" / "leaflet"
    )
    module.NODE_DIST = tmp_root / "node_modules" / "leaflet" / "dist"
    module.NODE_LICENSE = tmp_root / "node_modules" / "leaflet" / "LICENSE"
    return module


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal, self-consistent copy of the parts the gate looks at."""

    vendor = tmp_path / "custom_components" / "googlefindmy" / "vendor" / "leaflet"
    vendor.mkdir(parents=True)
    (vendor / "leaflet.js").write_text("// js\n", encoding="utf-8")
    (vendor / "leaflet.css").write_text("/* css */\n", encoding="utf-8")
    (vendor / "LICENSE").write_text("BSD-2-Clause\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"leaflet": "1.9.4"}}), encoding="utf-8"
    )

    module = _load(tmp_path)
    digests = {name: module.digest(vendor / name) for name in module.ASSETS}
    module.write_version("1.9.4", digests)
    return tmp_path


def test_the_gate_passes_on_a_consistent_tree(tree: Path) -> None:
    assert _load(tree).check() == 0


def test_a_version_bump_without_re_vendoring_fails(tree: Path) -> None:
    (tree / "package.json").write_text(
        json.dumps({"devDependencies": {"leaflet": "1.9.5"}}), encoding="utf-8"
    )

    assert _load(tree).check() == 1


def test_an_edited_asset_fails(tree: Path) -> None:
    asset = (
        tree
        / "custom_components"
        / "googlefindmy"
        / "vendor"
        / "leaflet"
        / "leaflet.js"
    )
    asset.write_text("// tampered\n", encoding="utf-8")

    assert _load(tree).check() == 1


def test_a_missing_asset_fails(tree: Path) -> None:
    (
        tree
        / "custom_components"
        / "googlefindmy"
        / "vendor"
        / "leaflet"
        / "leaflet.css"
    ).unlink()

    assert _load(tree).check() == 1


def test_a_missing_licence_fails(tree: Path) -> None:
    (
        tree / "custom_components" / "googlefindmy" / "vendor" / "leaflet" / "LICENSE"
    ).unlink()

    assert _load(tree).check() == 1


def test_update_refuses_a_stale_node_modules(tree: Path) -> None:
    """Stamping the pin onto an old installation would pass --check afterwards."""

    dist = tree / "node_modules" / "leaflet" / "dist"
    dist.mkdir(parents=True)
    (dist / "leaflet.js").write_text("// old\n", encoding="utf-8")
    (dist / "leaflet.css").write_text("/* old */\n", encoding="utf-8")
    (tree / "node_modules" / "leaflet" / "LICENSE").write_text(
        "BSD\n", encoding="utf-8"
    )
    (tree / "node_modules" / "leaflet" / "package.json").write_text(
        json.dumps({"version": "1.9.3"}), encoding="utf-8"
    )

    assert _load(tree).update() == 2


def test_update_refuses_a_package_without_a_licence(tree: Path) -> None:
    dist = tree / "node_modules" / "leaflet" / "dist"
    dist.mkdir(parents=True)
    (dist / "leaflet.js").write_text("// new\n", encoding="utf-8")
    (dist / "leaflet.css").write_text("/* new */\n", encoding="utf-8")
    (tree / "node_modules" / "leaflet" / "package.json").write_text(
        json.dumps({"version": "1.9.4"}), encoding="utf-8"
    )

    assert _load(tree).update() == 2
