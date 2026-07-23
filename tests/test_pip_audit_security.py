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

The live gate (``test_no_fixable_integration_owned_vulnerability``) resolves the
manifest specifiers over the network exactly as Home Assistant would and blocks
on the first shipped, integration-owned, fixable CVE. The offline unit tests
pin the engine's classification behavior deterministically without a network.

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
        """Fail if a shipped, integration-owned package has a fixable CVE.

        This is the real merge gate: it runs the ``audit_manifest`` engine over
        the live manifest requirements (pip-audit resolves them over the network
        in a subprocess, which the socket sandbox does not patch) under Home
        Assistant's committed constraints for the declared-minimum version.
        HA-governed and unfixable findings do not block; only actionable,
        integration-owned findings do. A pip-audit tooling/network failure
        (exit 2) skips rather than blocks.
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
            pytest.skip(
                f"pip-audit could not run (tooling or network unavailable):\n{report}"
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
        # aiohttp is HA-governed here (absent from owned_names) -> never blocks.
        findings = audit_manifest.classify_audit(
            _audit("aiohttp", "3.13.3", "CVE-TEST-2", ["3.14.1"]),
            {"selenium"},
        )
        assert findings["blocking"] == []
        assert findings["unfixable"] == []
        assert [r["id"] for r in findings["transitive"]] == ["CVE-TEST-2"]


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

    def test_ha_governed_finding_does_not_block(self, tmp_path: Path) -> None:
        rc = self._run(tmp_path, _audit("aiohttp", "3.13.3", "CVE-TEST-4", ["3.14.1"]))
        assert rc == 0

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
