"""tests/test_pip_audit_security.py: manifest-only pip-audit gate.

This module is the pytest face of the manifest-only dependency audit. It is
consolidated onto ``script/audit_manifest.py`` as the single classification
engine, replacing the former hand-maintained ``IGNORED_VULNERABILITIES``
denylist with a principled policy:

- Only the ``manifest.json`` ``requirements`` are audited, because that list --
  not ``poetry.lock`` -- is what Home Assistant installs for an end user.
- Packages that Home Assistant pins in its ``package_constraints.txt`` for the
  declared-minimum HA version (``hacs.json`` ``homeassistant``) are excluded
  from the blocking decision: the integration cannot raise its floor above
  Home Assistant's ``==`` pin without making installation impossible, so a
  finding there is not actionable. This subsumes the former protobuf ignore.
- Among the remaining integration-owned packages, only a *fixable* finding
  (a CVE with an available fix version) blocks. Unfixable ("won't fix")
  findings such as ``ecdsa`` are surfaced as warnings and never block. This
  subsumes the former ecdsa ignore.

The live gate (``test_no_fixable_integration_owned_vulnerability``) audits each
manifest requirement at its declared *floor* version (``pkg>=X`` -> ``pkg==X``)
over the network and blocks on the first shipped, integration-owned, fixable
CVE. Auditing the floor, not the resolver's newest pick, is what forces a floor
bump when the minimum a user may install is vulnerable. A pip-audit tooling or
network failure (exit 2) is fatal, not a skip, per the AGENTS.md pip-audit
contract. The offline unit tests pin the engine's classification behavior
deterministically without a network.

Usage:
    pytest tests/test_pip_audit_security.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from script import audit_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "custom_components" / "googlefindmy" / "manifest.json"
HACS_JSON = REPO_ROOT / "hacs.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pip_audit"
HA_CONSTRAINTS_URL = (
    "https://raw.githubusercontent.com/home-assistant/core/{version}/"
    "homeassistant/package_constraints.txt"
)


# ---------------------------------------------------------------------------
# Live gate helpers
# ---------------------------------------------------------------------------


def _require_pip_audit() -> None:
    """Skip when pip-audit is not importable (parity with the former test)."""
    try:
        import pip_audit  # noqa: F401
    except ImportError:  # pragma: no cover - environment guard
        pytest.skip("pip-audit not installed. Run: pip install pip-audit")


def _declared_minimum_ha_version() -> str:
    """Return the declared-minimum HA version from hacs.json."""
    data = json.loads(HACS_JSON.read_text(encoding="utf-8"))
    version = str(data.get("homeassistant", "")).strip()
    if not version:  # pragma: no cover - configuration guard
        pytest.skip("hacs.json declares no minimum homeassistant version")
    return version


def _ha_constraints_fixture(version: str) -> Path:
    """Return the committed HA package_constraints.txt snapshot for a version.

    The pytest suite is socket-sandboxed to localhost (tests/conftest.py), so
    the Home Assistant governance set is read from a committed fixture rather
    than fetched at runtime. A missing fixture means ``hacs.json`` was bumped
    without refreshing the snapshot; that is a loud, actionable failure, not a
    silent skip.
    """
    path = FIXTURES / f"ha_package_constraints_{version}.txt"
    if not path.is_file():  # pragma: no cover - refresh guard
        pytest.fail(
            f"Missing HA constraints fixture for declared-minimum HA {version}: "
            f"{path}\nhacs.json was bumped; add the snapshot from "
            f"{HA_CONSTRAINTS_URL.format(version=version)}"
        )
    return path


# ---------------------------------------------------------------------------
# Live gate
# ---------------------------------------------------------------------------


class TestManifestOnlyPipAuditGate:
    """The end-to-end manifest-only gate, resolved over the network."""

    def test_no_fixable_integration_owned_vulnerability(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Fail if a shipped, integration-owned package's floor has a fixable CVE.

        This is the real merge gate: it runs the ``audit_manifest`` engine over
        the live manifest requirements, each pinned to its declared floor
        (pip-audit resolves them over the network in a subprocess, which the
        socket sandbox does not patch) under Home Assistant's committed
        constraints for the declared-minimum version. HA-governed and unfixable
        findings do not block; only actionable, integration-owned findings do.

        A pip-audit tooling or network failure (engine exit 2) is fatal, never a
        skip: the AGENTS.md pip-audit contract requires exit codes > 1 to stay
        fatal so the workflow surfaces real issues instead of a silently green,
        un-audited gate. Absence of pip-audit entirely is the only benign skip
        and is handled by ``_require_pip_audit`` before the engine runs.
        """
        _require_pip_audit()
        version = _declared_minimum_ha_version()
        constraints_path = _ha_constraints_fixture(version)

        exit_code = audit_manifest.main(
            [
                "--manifest",
                str(MANIFEST),
                "--ha-constraints",
                str(constraints_path),
                "--verbose",
            ]
        )
        report = capsys.readouterr().out

        if exit_code == 2:  # pragma: no cover - tooling/network guard
            pytest.fail(
                "The manifest-only pip-audit gate could not run (tooling or "
                "network failure). Per the AGENTS.md pip-audit contract, exit "
                "codes > 1 are fatal and must not silently skip the gate.\n\n"
                f"{report}"
            )
        if exit_code == 1:
            pytest.fail(
                "Manifest-only pip-audit found a fixable, integration-owned "
                "vulnerability. Bump the manifest floor for the affected "
                f"package(s).\n\n{report}"
            )
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Offline engine unit tests (deterministic, no network)
# ---------------------------------------------------------------------------


def _write_json(path: Path, obj: object) -> Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _audit(name: str, version: str, vuln_id: str, fixes: list[str]) -> dict:
    return {
        "dependencies": [
            {
                "name": name,
                "version": version,
                "vulns": [{"id": vuln_id, "fix_versions": fixes}],
            }
        ]
    }


class TestClassifyAudit:
    """The classification core: block / warn / report partitioning."""

    def test_fixable_owned_blocks(self) -> None:
        findings = audit_manifest.classify_audit(
            _audit("selenium", "4.25.0", "PYSEC-TEST-1", ["4.26.0"]),
            {"selenium"},
        )
        assert [r["id"] for r in findings["blocking"]] == ["PYSEC-TEST-1"]
        assert findings["unfixable"] == []
        assert findings["transitive"] == []

    def test_unfixable_owned_warns(self) -> None:
        findings = audit_manifest.classify_audit(
            _audit("ecdsa", "0.19.2", "PYSEC-2026-1325", []),
            {"ecdsa"},
        )
        assert findings["blocking"] == []
        assert [r["id"] for r in findings["unfixable"]] == ["PYSEC-2026-1325"]

    def test_non_owned_is_transitive(self) -> None:
        # A vulnerable package that is neither owned nor governed (e.g. a
        # transitive dependency) is reported, never blocking.
        findings = audit_manifest.classify_audit(
            _audit("idna", "3.4", "CVE-TEST-2", ["3.7"]),
            {"selenium"},
        )
        assert findings["blocking"] == []
        assert findings["unfixable"] == []
        assert findings["governed"] == []
        assert [r["id"] for r in findings["transitive"]] == ["CVE-TEST-2"]

    def test_governed_finding_is_reported_not_blocking(self) -> None:
        # A fixable CVE in an HA-governed direct manifest entry is surfaced for
        # transparency but never blocks (the integration cannot raise its floor
        # above HA's == pin). This is the finding the old design could not
        # produce live, because governed packages were never audited.
        findings = audit_manifest.classify_audit(
            _audit("aiohttp", "3.11.8", "CVE-TEST-GOV", ["3.14.1"]),
            {"selenium"},
            {"aiohttp"},
        )
        assert findings["blocking"] == []
        assert findings["unfixable"] == []
        assert findings["transitive"] == []
        assert [r["id"] for r in findings["governed"]] == ["CVE-TEST-GOV"]


class TestPartitionAndConstraints:
    """Requirement partitioning and HA-constraint parsing."""

    def test_ha_governed_is_excluded_from_owned(self) -> None:
        owned, ha_governed = audit_manifest.partition_requirements(
            ["aiohttp>=3.11.8", "selenium>=4.25.0"], {"aiohttp"}
        )
        assert owned == ["selenium>=4.25.0"]
        assert ha_governed == ["aiohttp>=3.11.8"]

    def test_unparseable_requirement_is_owned(self) -> None:
        owned, ha_governed = audit_manifest.partition_requirements(
            ["not a requirement!!!"], set()
        )
        assert owned == ["not a requirement!!!"]
        assert ha_governed == []

    def test_parse_ha_governed_names_reads_only_pins(self) -> None:
        text = (
            "# generated constraints\n"
            "aiohttp==3.13.3\n"
            "cryptography==46.0.5\n"
            "somerange>=1.0\n"
            "\n"
        )
        assert audit_manifest.parse_ha_governed_names(text) == {
            "aiohttp",
            "cryptography",
        }


class TestFloorPin:
    """Floor pinning: audit the minimum allowed version, not the newest pick."""

    def test_pins_lower_bound_to_exact_floor(self) -> None:
        specifiers, unbounded = audit_manifest.floor_pin_requirements(
            ["selenium>=4.25.0", "aiohttp>=3.11.8"]
        )
        assert specifiers == ["selenium==4.25.0", "aiohttp==3.11.8"]
        assert unbounded == []

    def test_highest_lower_bound_wins(self) -> None:
        # A compound specifier is pinned to its most restrictive lower bound.
        specifiers, unbounded = audit_manifest.floor_pin_requirements(
            ["pkg>=1.0,<2.0", "other>1.0,>=1.5"]
        )
        assert specifiers == ["pkg==1.0", "other==1.5"]
        assert unbounded == []

    def test_compatible_release_pins_to_its_floor(self) -> None:
        # ``~=1.4.5`` allows 1.4.5 itself, so the floor is 1.4.5.
        specifiers, unbounded = audit_manifest.floor_pin_requirements(["pkg~=1.4.5"])
        assert specifiers == ["pkg==1.4.5"]
        assert unbounded == []

    def test_requirement_without_floor_is_flagged(self) -> None:
        # A bare name, an upper-bound-only requirement, or an *exclusive* ``>X``
        # (which excludes X) has no determinable floor; it is passed through
        # (audited at the newest pick) and its name is surfaced.
        specifiers, unbounded = audit_manifest.floor_pin_requirements(
            ["selenium", "capped<2.0", "exclusive>1.0"]
        )
        assert specifiers == ["selenium", "capped<2.0", "exclusive>1.0"]
        assert unbounded == ["selenium", "capped", "exclusive"]

    def test_unparseable_requirement_passes_through(self) -> None:
        specifiers, unbounded = audit_manifest.floor_pin_requirements(["!!bogus!!"])
        assert specifiers == ["!!bogus!!"]
        assert unbounded == []


class TestMainDecision:
    """The CLI decision surface, driven by a pre-generated audit (no network)."""

    def _constraints(self, tmp_path: Path) -> Path:
        path = tmp_path / "ha_constraints.txt"
        path.write_text("aiohttp==3.13.3\n", encoding="utf-8")
        return path

    def _run(self, tmp_path: Path, audit: dict) -> int:
        # ``--verbose`` forces the full report to stdout even when there is no
        # blocking finding, so the WARN/INFO assertions can read it.
        audit_path = _write_json(tmp_path / "audit.json", audit)
        return audit_manifest.main(
            [
                "--manifest",
                str(MANIFEST),
                "--ha-constraints",
                str(self._constraints(tmp_path)),
                "--audit-json",
                str(audit_path),
                "--verbose",
            ]
        )

    def test_blocks_on_fixable_owned(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = self._run(
            tmp_path, _audit("selenium", "4.25.0", "PYSEC-TEST-3", ["4.26.0"])
        )
        assert rc == 1
        assert "BLOCKING (1)" in capsys.readouterr().out

    def test_ha_governed_finding_does_not_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = self._run(tmp_path, _audit("aiohttp", "3.13.3", "CVE-TEST-4", ["3.14.1"]))
        out = capsys.readouterr().out
        assert rc == 0
        # The governed finding is surfaced live as a non-blocking INFO line;
        # this is the transparency the audit-all model restores (was impossible
        # when governed packages were never audited).
        assert "Home Assistant-pinned dependency vulnerabilities" in out
        assert "CVE-TEST-4" in out

    def test_unfixable_owned_does_not_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = self._run(tmp_path, _audit("ecdsa", "0.19.2", "PYSEC-2026-1325", []))
        assert rc == 0
        assert "WARN (1)" in capsys.readouterr().out

    def test_clean_audit_is_green(self, tmp_path: Path) -> None:
        assert self._run(tmp_path, {"dependencies": []}) == 0

    def test_missing_manifest_is_tooling_error(self, tmp_path: Path) -> None:
        audit_path = _write_json(tmp_path / "audit.json", {"dependencies": []})
        rc = audit_manifest.main(
            [
                "--manifest",
                str(tmp_path / "does-not-exist.json"),
                "--ha-constraints",
                str(self._constraints(tmp_path)),
                "--audit-json",
                str(audit_path),
            ]
        )
        assert rc == 2

    def test_malformed_audit_json_is_tooling_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "audit.json"
        bad.write_text("{ this is not valid json", encoding="utf-8")
        rc = audit_manifest.main(
            [
                "--manifest",
                str(MANIFEST),
                "--ha-constraints",
                str(self._constraints(tmp_path)),
                "--audit-json",
                str(bad),
            ]
        )
        assert rc == 2

    def test_pip_audit_exit_gt_1_is_tooling_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A pip-audit exit code > 1 is a tooling/network failure and must map to
        # exit 2, never to a spurious block. No --audit-json, so main() takes
        # the live branch and invokes the (patched) runner.
        monkeypatch.setattr(audit_manifest, "run_pip_audit", lambda _reqs, _out: 2)
        rc = audit_manifest.main(
            [
                "--manifest",
                str(MANIFEST),
                "--ha-constraints",
                str(self._constraints(tmp_path)),
            ]
        )
        assert rc == 2

    def test_unparseable_manifest_entry_does_not_crash(self, tmp_path: Path) -> None:
        # An unparseable manifest requirement is audited, not silently dropped,
        # and must not crash owned-name derivation (wiring, not just the
        # isolated partition helper).
        manifest = _write_json(
            tmp_path / "manifest.json",
            {"requirements": ["selenium>=4.25.0", "!!bogus!!"]},
        )
        audit_path = _write_json(tmp_path / "audit.json", {"dependencies": []})
        rc = audit_manifest.main(
            [
                "--manifest",
                str(manifest),
                "--ha-constraints",
                str(self._constraints(tmp_path)),
                "--audit-json",
                str(audit_path),
            ]
        )
        assert rc == 0


def test_run_pip_audit_invokes_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_pip_audit shells out to ``python -m pip_audit`` and returns its code."""
    seen: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], check: bool) -> object:
        seen["cmd"] = cmd
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_text('{"dependencies": []}', encoding="utf-8")

        class _Completed:
            returncode = 0

        return _Completed()

    monkeypatch.setattr(audit_manifest.subprocess, "run", fake_run)
    output = tmp_path / "audit.json"
    rc = audit_manifest.run_pip_audit(["selenium>=4.25.0"], output)

    assert rc == 0
    assert "pip_audit" in seen["cmd"]
    assert json.loads(output.read_text(encoding="utf-8")) == {"dependencies": []}
