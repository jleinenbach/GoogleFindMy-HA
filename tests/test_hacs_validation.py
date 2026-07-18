# tests/test_hacs_validation.py
# tests/test_hacs_validation.py
"""Validate HACS metadata alignment and guard against unsupported characters."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from custom_components.googlefindmy.const import INTEGRATION_VERSION


@pytest.fixture(name="hacs_metadata")
def fixture_hacs_metadata() -> dict[str, object]:
    """Load the hacs.json metadata file."""

    hacs_path = Path("hacs.json")
    metadata = json.loads(hacs_path.read_text(encoding="utf-8"))
    assert isinstance(metadata, dict)
    return metadata


def test_hacs_metadata_matches_manifest(
    hacs_metadata: dict[str, object],
    manifest: dict[str, object],
    integration_root: Path,
) -> None:
    """Ensure HACS metadata mirrors manifest declarations and const values."""

    allowed_keys = {
        "name",
        "homeassistant",
        "content_in_root",
        "render_readme",
        "filename",
        "zip_release",
        "hide_default_branch",
    }
    assert set(hacs_metadata).issubset(allowed_keys)
    assert hacs_metadata["name"] == manifest["name"]

    const_text = (integration_root / "const.py").read_text(encoding="utf-8")
    # No type annotation: semantic-release's version_variables regex only matches
    # `NAME = "x"`, not `NAME: str = "x"` (see const.py / AGENTS.md). This pattern
    # therefore doubles as a guard against re-introducing the annotation.
    match = re.search(r'INTEGRATION_VERSION = "([^"]+)"', const_text)
    assert match, "INTEGRATION_VERSION constant missing (or has a type annotation)"
    assert manifest["version"] == INTEGRATION_VERSION == match.group(1)
    assert "homeassistant" not in manifest


def test_no_micro_sign_in_integration_files(
    integration_python_files: list[Path], integration_root: Path
) -> None:
    """Integration Python files must not contain the micro sign character."""

    offenders: list[str] = []
    for path in integration_python_files:
        text = path.read_text(encoding="utf-8")
        if "\u00b5" in text:
            offenders.append(str(path.relative_to(integration_root)))
    assert not offenders, f"micro sign detected in: {offenders}"


def test_hacs_zip_release_declares_filename(
    hacs_metadata: dict[str, object],
) -> None:
    """When zip_release is enabled, a ``.zip`` release asset must be declared.

    HACS then installs that asset instead of the git source archive, so the
    version-stamped ZIP built by release-stamp.yml actually reaches users. The
    two only line up if hacs.json names the asset; guard that here.
    """

    if hacs_metadata.get("zip_release"):
        filename = hacs_metadata.get("filename")
        assert isinstance(filename, str) and filename.endswith(".zip"), (
            "zip_release=true requires a '.zip' 'filename' entry in hacs.json"
        )


def test_release_zip_places_manifest_at_root() -> None:
    """release-stamp.yml must zip the domain *contents*, not the domain dir.

    With zip_release=true HACS extracts the asset straight into
    ``custom_components/googlefindmy/`` (verified against HACS ``extractall``
    semantics), so ``manifest.json`` has to sit at the ZIP root. Building the ZIP
    from the parent (``zip -r ../googlefindmy.zip googlefindmy``) nests it one
    level too deep and hides the integration -- a broken install for everyone.
    This guard locks the fixed ``cd <domain> && zip . `` form so the regression
    cannot silently return.
    """

    workflow = Path(".github/workflows/release-stamp.yml").read_text(encoding="utf-8")
    filename = json.loads(Path("hacs.json").read_text(encoding="utf-8")).get("filename")
    assert filename, "hacs.json must declare the release asset filename"
    assert "cd custom_components/googlefindmy" in workflow, (
        "ZIP must be built from inside the integration domain directory"
    )
    assert f"zip -r ../../{filename} ." in workflow, (
        f"ZIP step must run 'zip -r ../../{filename} .' from the domain dir "
        "so the asset root is the domain root"
    )
    # Regression guard: the pre-fix form nested the domain dir inside the asset.
    assert "zip -r ../googlefindmy.zip googlefindmy" not in workflow, (
        "found the pre-fix ZIP form that nests custom_components/googlefindmy/ "
        "inside the asset (breaks the HACS install)"
    )
    assert f'gh release upload "$TAG_NAME" {filename} --clobber' in workflow, (
        f"workflow must upload the declared hacs.json asset name {filename!r}"
    )


def test_ci_tolerates_transient_hacs_regression() -> None:
    """ci.yml must guard the HACS job against the transient hacs/integration#5252
    regression without permanently disabling the two affected validators.

    The upstream action intermittently fetches the raw hacs.json / manifest.json
    as None and reports them invalid on byte-identical, valid files. The guard
    runs HACS first (continue-on-error), and only on failure re-runs it with
    exactly ``hacsjson`` and ``integration_manifest`` isolated. If everything
    else passes the failure was the known transient signature (tolerated); a
    failure of any other check still exits non-zero (real defect). The isolation
    must NOT leak into the primary run, otherwise those two checks would be off
    permanently. This guard locks the shape so the resilience cannot silently
    regress into either a blanket-ignore or a hard block.
    """

    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    # Primary HACS run tolerates failure so the classifier can decide.
    assert "continue-on-error: true" in workflow, (
        "primary HACS run must set continue-on-error so the guard can classify"
    )
    # Fallback run isolates exactly the two transient-prone validators, gated on
    # the primary run having failed (so it never runs when HACS is healthy).
    assert "ignore: topics issues hacsjson integration_manifest" in workflow, (
        "fallback HACS run must isolate hacsjson + integration_manifest"
    )
    assert "steps.hacs.outcome == 'failure'" in workflow, (
        "fallback HACS run must be gated on the primary run failing"
    )
    # A defect beyond the transient signature must still fail the job.
    assert "exit 1" in workflow, (
        "classifier must exit non-zero when the failure is not the transient one"
    )
    # The isolation must be scoped to the fallback only: the primary run keeps
    # the original, narrower ignore list (not the extended one).
    assert "ignore: topics issues\n" in workflow, (
        "primary HACS run must keep 'ignore: topics issues' (isolation not permanent)"
    )
