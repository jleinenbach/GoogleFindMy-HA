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
import sys
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
        # Capture both streams: on a fatal exit 2 the engine writes its
        # actionable explanation (resolver, network, or malformed-report cause)
        # to stderr, and capsys drains both buffers here, so a stdout-only grab
        # would strand that diagnostic where pytest can no longer surface it.
        captured = capsys.readouterr()
        report = captured.out

        if exit_code == 2:  # pragma: no cover - tooling/network guard
            pytest.fail(
                "The manifest-only pip-audit gate could not run (tooling or "
                "network failure). Per the AGENTS.md pip-audit contract, exit "
                "codes > 1 are fatal and must not silently skip the gate.\n\n"
                f"stdout:\n{captured.out}\n\nstderr:\n{captured.err}"
            )
        if exit_code == 1:
            pytest.fail(
                "Manifest-only pip-audit found a fixable, integration-owned "
                "vulnerability. Bump the manifest floor for the affected "
                f"package(s).\n\n{report}"
            )
        # A clean exit 0 may still carry non-blocking findings (today the
        # unfixable ecdsa advisory) as the verbose report on stdout plus
        # ::warning/::notice annotations on stderr. capsys drained both buffers
        # above, so re-emit them; otherwise the accepted vulnerabilities the
        # surfacing policy promises to keep visible are swallowed in the CI
        # pytest log. The exit 1/2 branches already fold the captured content
        # into their pytest.fail messages and raise before reaching here, so
        # this re-emit runs only on the success path and never double-prints.
        sys.stdout.write(captured.out)
        sys.stderr.write(captured.err)
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

    def test_fixable_transitive_blocks(self) -> None:
        # A *fixable* vulnerability in a package that is neither owned nor
        # governed (a transitive dependency) is actionable -- the direct parent
        # can be bumped or a constraint added -- so it blocks. This is Fund 1:
        # a fixable transitive finding must not be waved through as INFO.
        findings = audit_manifest.classify_audit(
            _audit("idna", "3.4", "CVE-TEST-2", ["3.7"]),
            {"selenium"},
        )
        assert findings["blocking"] == []
        assert findings["unfixable"] == []
        assert findings["governed"] == []
        assert findings["transitive"] == []
        assert [r["id"] for r in findings["transitive_blocking"]] == ["CVE-TEST-2"]

    def test_unfixable_transitive_is_info(self) -> None:
        # An *unfixable* transitive finding stays a non-blocking INFO: there is
        # no fix to pull in, so blocking would be a permanent red.
        findings = audit_manifest.classify_audit(
            _audit("idna", "3.4", "CVE-TEST-2b", []),
            {"selenium"},
        )
        assert findings["blocking"] == []
        assert findings["transitive_blocking"] == []
        assert [r["id"] for r in findings["transitive"]] == ["CVE-TEST-2b"]

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
        assert findings["transitive_blocking"] == []
        assert [r["id"] for r in findings["governed"]] == ["CVE-TEST-GOV"]

    def test_ha_governed_transitive_is_not_blocking(self) -> None:
        # A fixable CVE in a package HA governs but which is NOT a direct
        # manifest entry (a transitive such as yarl) must be classified as
        # governed, never transitive_blocking: the integration cannot bump HA's
        # runtime pin for it any more than for a direct governed entry, so the
        # "bump the parent" remediation is infeasible and blocking would be a
        # permanent red. This locks the governed check ahead of the fixable
        # transitive branch and requires the FULL HA name set, not only the
        # HA-governed manifest entries. yarl is EXACT-``==``-pinned by HA, so it
        # belongs in the ``governed`` bucket (not ``governed_range``); the exact
        # subset is passed explicitly so the Option B exact/range split is
        # exercised rather than relying on the None default.
        findings = audit_manifest.classify_audit(
            _audit("yarl", "1.9.0", "CVE-YARL", ["1.10.0"]),
            {"selenium"},
            {"yarl"},
            {"yarl"},
        )
        assert findings["blocking"] == []
        assert findings["transitive_blocking"] == []
        assert findings["transitive"] == []
        assert findings["governed_range"] == []
        assert [r["id"] for r in findings["governed"]] == ["CVE-YARL"]

    def test_exact_governed_and_range_governed_route_to_distinct_buckets(
        self,
    ) -> None:
        # Option B core: of two governed packages, ``foo`` is exact-``==``-pinned
        # (unfixable by the integration -> ``governed``) and ``bar`` is only
        # range-floored (the integration COULD tighten its manifest ->
        # ``governed_range``). Both stay out of ``blocking``. Collapsing the
        # exact/range split (passing the full governed set as "exact") would send
        # bar back into ``governed`` and empty ``governed_range`` -- the mutation
        # this test is built to catch.
        audit = {
            "dependencies": [
                {
                    "name": "foo",
                    "version": "1.0",
                    "vulns": [{"id": "CVE-FOO", "fix_versions": ["1.1"]}],
                },
                {
                    "name": "bar",
                    "version": "2.0",
                    "vulns": [{"id": "CVE-BAR", "fix_versions": ["2.1"]}],
                },
            ]
        }
        findings = audit_manifest.classify_audit(
            audit,
            {"selenium"},
            {"foo", "bar"},
            {"foo"},
        )
        assert findings["blocking"] == []
        assert findings["transitive_blocking"] == []
        assert [r["id"] for r in findings["governed"]] == ["CVE-FOO"]
        assert [r["id"] for r in findings["governed_range"]] == ["CVE-BAR"]


class TestReachableGovernedTransitivePins:
    """Which governed transitive packages need a second, HA-pinned audit pass."""

    def test_selects_only_governed_transitive_at_wrong_version(self) -> None:
        # yarl is HA-governed and reaches the manifest only transitively (via
        # aiohttp); pass 1 resolved it to 1.24.5, above HA's 1.20.1 pin, so it
        # must be re-audited at the pin. Every other dependency is excluded for a
        # distinct reason: aiohttp is a *direct* governed manifest entry (already
        # audited at HA's pin), selenium is integration-owned, multidict is
        # governed but pass 1 already resolved it to HA's pin, and idna is not
        # governed at all.
        audit = {
            "dependencies": [
                {"name": "yarl", "version": "1.24.5", "vulns": []},
                {"name": "aiohttp", "version": "3.13.3", "vulns": []},
                {"name": "selenium", "version": "4.25.0", "vulns": []},
                {"name": "multidict", "version": "6.0.0", "vulns": []},
                {"name": "idna", "version": "3.4", "vulns": []},
            ]
        }
        governed_pins = {
            "yarl": "1.20.1",
            "aiohttp": "3.13.3",
            "multidict": "6.0.0",
        }
        result = audit_manifest.reachable_governed_transitive_pins(
            audit,
            governed_pins,
            direct_governed_names={"aiohttp"},
            owned_names={"selenium"},
        )
        assert result == {"yarl": "1.20.1"}


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

    def test_parse_ha_governed_names_includes_range_floors(self) -> None:
        # A range-constrained governed package (somerange) is governed too: its
        # name is read alongside exact pins, because HA governs its version even
        # without a zero-width pin.
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
            "somerange",
        }

    def test_parse_ha_governed_pins_retains_versions(self) -> None:
        # Fund 2: the pin *version* is kept (not discarded), because governed
        # packages are audited at HA's own pin, not at the manifest floor. A
        # range constraint keeps its inclusive floor (somerange>=1.0 -> 1.0). A
        # constraint whose environment marker *holds* on this runtime is honored
        # like any other; the always-true ``python_version >= '3.0'`` keeps
        # ``marked`` governed (a false marker is exercised separately below).
        text = (
            "# generated constraints\n"
            "aiohttp==3.12.15\n"
            "cryptography==45.0.3\n"
            "somerange>=1.0\n"
            "marked==1.2.3 ; python_version >= '3.0'\n"
            "\n"
        )
        assert audit_manifest.parse_ha_governed_pins(text) == {
            "aiohttp": "3.12.15",
            "cryptography": "45.0.3",
            "somerange": "1.0",
            "marked": "1.2.3",
        }
        # The name set stays a derived view of the pin mapping.
        assert audit_manifest.parse_ha_governed_names(text) == set(
            audit_manifest.parse_ha_governed_pins(text)
        )

    def test_marker_gated_constraints_respect_runtime(self) -> None:
        # A PEP 508 environment marker gates whether HA imposes the pin. A
        # constraint whose marker is false on this runtime is NOT governed: HA
        # does not impose it here, so it must not be diverted into the
        # non-blocking governed maps. A satisfied marker is governed like any
        # other. The markers below are interpreter-independent (always
        # true/false on Python 3) so the assertion does not drift with the CI
        # Python version. All three governed views must agree, since they share
        # ``_merged_governed_constraints``.
        text = (
            "plain==4.0\n"  # no marker -> governed
            "kept==1.0 ; python_version >= '3.0'\n"  # true marker -> governed
            "dropped==2.0 ; python_version < '3.0'\n"  # false marker -> skipped
            "droppedrange>=3.0 ; python_version < '3.0'\n"  # false marker -> skipped
        )
        assert audit_manifest.parse_ha_governed_pins(text) == {
            "plain": "4.0",
            "kept": "1.0",
        }
        assert audit_manifest.parse_ha_governed_names(text) == {"plain", "kept"}
        assert audit_manifest.parse_ha_exact_governed_names(text) == {"plain", "kept"}

    def test_parse_ha_governed_pins_skips_empty_and_falls_back(self) -> None:
        # An empty name or empty version on a ``==`` line is skipped; a line
        # whose left-hand side is not a valid PEP 508 name falls back to the
        # canonicalized bare name so a non-standard ``==`` constraint still
        # governs. A non-PEP 508 *range* line has no recoverable floor (the
        # fallback salvages only ``==``), so it is dropped, not guessed.
        text = (
            "==1.0\n"  # empty name -> skipped
            "foo==\n"  # empty version -> skipped
            "weird!name==2.0\n"  # not PEP 508 -> bare-name fallback
            "bad!range>=2.0\n"  # not PEP 508 range -> no salvage, dropped
        )
        assert audit_manifest.parse_ha_governed_pins(text) == {"weird!name": "2.0"}

    def test_parse_ha_governed_pins_reads_range_floors(self) -> None:
        # The regression Codex flagged: HA governs many transitives with a range
        # rather than an exact pin. Each contributes its inclusive lower bound so
        # the audit runs at the worst-case permitted version; a constraint with
        # no inclusive floor (a pure ``!=`` exclusion or an upper-bound-only cap)
        # yields no worst-case version and is dropped.
        text = (
            "urllib3>=2.0\n"
            "typing-extensions>=4.15.0,<5.0\n"
            "certifi>=2021.5.30\n"
            "compat~=1.4.5\n"
            "pubnub!=6.4.0\n"  # no inclusive floor -> dropped
            "gql<4.0.0\n"  # upper-bound only -> dropped
        )
        assert audit_manifest.parse_ha_governed_pins(text) == {
            "urllib3": "2.0",
            "typing-extensions": "4.15.0",
            "certifi": "2021.5.30",
            "compat": "1.4.5",
        }

    def test_parse_ha_exact_governed_names_splits_exact_and_range(self) -> None:
        # Option B: only a single concrete ``==`` pin is exact (the integration
        # cannot tighten it); a range floor (>=, ~=, or ==+cap), even though it
        # is governed and keeps its floor for the re-audit, is NOT exact. A
        # non-PEP 508 salvaged ``==`` counts as exact; a floor-less constraint is
        # neither governed nor exact.
        text = (
            "foo==1.0\n"  # exact
            "bar>=2.0\n"  # range -> not exact
            "compat~=1.4.5\n"  # compatible-release range -> not exact
            "capped==1.0,<2.0\n"  # == plus a cap -> not a lone == -> not exact
            "weird!name==2.0\n"  # non-PEP 508 salvage -> exact
            "gql<4.0.0\n"  # no inclusive floor -> not governed at all
        )
        # Exact is a strict subset of the full governed name set.
        assert audit_manifest.parse_ha_exact_governed_names(text) == {
            "foo",
            "weird!name",
        }
        assert audit_manifest.parse_ha_governed_names(text) == {
            "foo",
            "bar",
            "compat",
            "capped",
            "weird!name",
        }

    def test_duplicate_governed_constraint_uses_combined_floor(self) -> None:
        # Fund 2: pip applies every constraint line for a package together, so a
        # package HA lists more than once (the committed snapshot pins multidict
        # at both >=6.0.2 and >=6.4.2) is governed by the intersection of its
        # lines. A looser bound ordered AFTER a tighter one must not mask the
        # tighter floor -- last-wins would audit multidict==6.0.2, a version HA
        # actually excludes. Both orderings must yield the higher floor.
        looser_last = "multidict>=6.4.2\nmultidict>=6.0.2\n"
        tighter_last = "multidict>=6.0.2\nmultidict>=6.4.2\n"
        assert audit_manifest.parse_ha_governed_pins(looser_last) == {
            "multidict": "6.4.2"
        }
        assert audit_manifest.parse_ha_governed_pins(tighter_last) == {
            "multidict": "6.4.2"
        }

    def test_duplicate_exact_and_range_routes_to_actionable_bucket(self) -> None:
        # Fund 2 exactness: a package HA constrains once as == and once with an
        # extra bound is no longer a lone == in the combined set, so it routes to
        # the actionable governed_range bucket (excluded from the exact names)
        # rather than being silently suppressed as unfixable. Order-invariant.
        for text in ("foo==1.5\nfoo>=1.0\n", "foo>=1.0\nfoo==1.5\n"):
            assert audit_manifest.parse_ha_governed_pins(text) == {"foo": "1.5"}
            assert audit_manifest.parse_ha_governed_names(text) == {"foo"}
            assert audit_manifest.parse_ha_exact_governed_names(text) == set()

    def test_contradictory_duplicate_has_no_derivable_floor(self) -> None:
        # Fund 2 fail-safe: contradictory duplicates (>=2.0 combined with ==1.5)
        # admit no version, so no exact floor is derivable and the package is
        # dropped from the governed maps entirely -- it stays in the blocking
        # bucket rather than being pinned to a version HA's own == excludes.
        for text in ("foo>=2.0\nfoo==1.5\n", "foo==1.5\nfoo>=2.0\n"):
            assert audit_manifest.parse_ha_governed_pins(text) == {}
            assert audit_manifest.parse_ha_exact_governed_names(text) == set()

    def test_no_manifest_dependency_is_ha_range_governed(self) -> None:
        # Tripwire for the latent classification flip a range floor introduces:
        # a DIRECT manifest entry HA governs is audited at HA's own pin
        # (ha_pin_requirements), which is sound only when HA pins it EXACTLY.
        # If HA ever range-constrains a manifest dependency (e.g. aiohttp>=3.0
        # instead of ==), that entry would flip owned->governed, be audited at
        # HA's floor -- possibly below the manifest floor -- and silently drop
        # from blocking to non-blocking INFO. Today every manifest dependency HA
        # governs is ==-pinned; this locks that invariant so a future HA snapshot
        # that breaks it fails CI loudly instead of hiding an actionable finding.
        manifest_names = {
            audit_manifest.requirement_project_name(req)
            for req in audit_manifest.load_manifest_requirements(MANIFEST)
        }
        version = _declared_minimum_ha_version()
        text = _ha_constraints_fixture(version).read_text(encoding="utf-8")
        range_governed: set[str] = set()
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                requirement = audit_manifest.Requirement(line)
            except audit_manifest.InvalidRequirement:
                continue
            operators = {spec.operator for spec in requirement.specifier}
            # Governed (a determinable floor) but not via an exact == pin.
            if audit_manifest._requirement_floor(
                requirement
            ) is not None and operators != {"=="}:
                range_governed.add(audit_manifest.canonicalize_name(requirement.name))
        assert manifest_names & range_governed == set()

    def test_tripwire_follows_declared_ha_version_not_a_hardcoded_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Fund 1: the range-governed tripwire must inspect the snapshot for the
        # HA version hacs.json declares, resolved through the same helpers as the
        # live gate, not a pinned filename. Were the fixture hard-coded, a
        # hacs.json bump to a release whose snapshot range-governs a manifest
        # dependency would keep reading the old clean file and pass, hiding
        # exactly the blocking->INFO flip this tripwire exists to catch. Point
        # the resolver at a synthetic newer snapshot that range-governs a real
        # manifest dependency and assert the *real* tripwire fires.
        manifest_name = audit_manifest.requirement_project_name(
            next(iter(audit_manifest.load_manifest_requirements(MANIFEST)))
        )
        synthetic = tmp_path / "ha_package_constraints_9999.1.1.txt"
        synthetic.write_text(f"{manifest_name}>=0.0.1\n", encoding="utf-8")
        monkeypatch.setattr(
            f"{__name__}._declared_minimum_ha_version", lambda: "9999.1.1"
        )
        monkeypatch.setattr(
            f"{__name__}._ha_constraints_fixture", lambda _version: synthetic
        )
        with pytest.raises(AssertionError):
            self.test_no_manifest_dependency_is_ha_range_governed()

    def test_ha_pin_requirements_pins_to_ha_version(self) -> None:
        # A governed manifest entry is pinned to HA's == version, overriding its
        # declared floor, so the audit reflects what the user actually runs.
        specifiers = audit_manifest.ha_pin_requirements(
            ["aiohttp>=3.11.8", "cryptography>=43.0.3"],
            {"aiohttp": "3.12.15", "cryptography": "45.0.3"},
        )
        assert specifiers == ["aiohttp==3.12.15", "cryptography==45.0.3"]

    def test_ha_pin_preserves_extras_and_marker(self) -> None:
        # The same reconstruction class as floor pinning: a governed manifest
        # entry with extras or a marker must keep both when pinned to HA's
        # version, not collapse to a bare ``name==pin``.
        (specifier,) = audit_manifest.ha_pin_requirements(
            ["aiohttp[speedups]>=3.11.8; python_version >= '3.0'"],
            {"aiohttp": "3.12.15"},
        )
        assert "aiohttp[speedups]==3.12.15" in specifier
        assert "python_version >= " in specifier


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

    def test_floor_excluded_by_specifier_is_unbounded(self) -> None:
        # ``>=1.0,!=1.0`` names 1.0 as its lower bound yet forbids it. Pinning
        # ``foo==1.0`` would be a version the requirement excludes and pip-audit
        # would fail to resolve (tooling exit 2). No exact floor is derivable, so
        # the entry is passed through as declared and surfaced as unbounded.
        specifiers, unbounded = audit_manifest.floor_pin_requirements(
            ["foo>=1.0,!=1.0"]
        )
        assert specifiers == ["foo>=1.0,!=1.0"]
        assert unbounded == ["foo"]

    def test_floor_pin_preserves_extras_and_marker(self) -> None:
        # Reconstructing a pinned entry must keep extras (so extra-only
        # transitive deps and their CVEs are still audited) and the environment
        # marker (so a platform-inapplicable entry is skipped, not blocked).
        (specifier,), unbounded = audit_manifest.floor_pin_requirements(
            ["foo[crypto]>=1.0; sys_platform == 'linux'"]
        )
        assert "foo[crypto]==1.0" in specifier
        assert 'sys_platform == "linux"' in specifier
        assert unbounded == []
        # A bare entry is still rendered exactly as before (no regression).
        (bare,), _ = audit_manifest.floor_pin_requirements(["aiohttp>=3.11.8"])
        assert bare == "aiohttp==3.11.8"


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
        # Fund 2: the governed finding is surfaced as an honest, non-blocking
        # INFO, audited at HA's own pin. The finding is fixable, yet it does not
        # block (the integration cannot lower HA's runtime pin), and the report
        # names the real remediation (raise the hacs.json HA floor).
        assert "Home Assistant's own minimum-version pin" in out
        assert "hacs.json" in out
        assert "CVE-TEST-4" in out
        # The old false assurance must be gone: HA's pinned version may itself
        # be vulnerable, so "resolved by the user's HA version" was untrue.
        assert "resolved by" not in out

    def test_marker_false_governed_manifest_dependency_stays_blocking(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The finding's end-to-end scenario: HA governs a manifest dependency
        # (aiohttp) with a PEP 508 environment marker. The SAME fixable audit is
        # classified twice, flipping only the marker, to prove the marker alone
        # drives the routing (interpreter-independent markers avoid CI drift).
        audit = _audit("aiohttp", "3.13.3", "CVE-MARKER-1", ["3.14.1"])

        def run(marker: str) -> tuple[int, str]:
            constraints = tmp_path / "ha_constraints.txt"
            constraints.write_text(f"aiohttp==3.13.3 ; {marker}\n", encoding="utf-8")
            audit_path = _write_json(tmp_path / "audit.json", audit)
            rc = audit_manifest.main(
                [
                    "--manifest",
                    str(MANIFEST),
                    "--ha-constraints",
                    str(constraints),
                    "--audit-json",
                    str(audit_path),
                    "--verbose",
                ]
            )
            return rc, capsys.readouterr().out

        # True marker: HA imposes the pin here -> governed -> non-blocking INFO.
        rc_true, out_true = run("python_version >= '3.0'")
        assert rc_true == 0
        assert "Home Assistant's own minimum-version pin" in out_true

        # False marker: HA does NOT impose the pin on this runtime, so aiohttp is
        # integration-owned and its fixable finding blocks, instead of being
        # silently suppressed at a version Home Assistant never imposes here.
        rc_false, out_false = run("python_version < '3.0'")
        assert rc_false == 1
        assert "BLOCKING (1)" in out_false

    def test_fixable_transitive_finding_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Fund 1 at the CLI surface: a fixable transitive finding exits 1 and
        # names the transitive remediation, not "bump the manifest floor".
        rc = self._run(tmp_path, _audit("idna", "3.4", "CVE-TEST-5", ["3.7"]))
        out = capsys.readouterr().out
        assert rc == 1
        assert "BLOCKING (1)" in out
        assert "parent dependency" in out

    def test_unfixable_transitive_does_not_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = self._run(tmp_path, _audit("idna", "3.4", "CVE-TEST-6", []))
        out = capsys.readouterr().out
        assert rc == 0
        assert "unfixable transitive" in out
        assert "CVE-TEST-6" in out

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

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param([], id="root-list"),
            pytest.param(None, id="root-null"),
            pytest.param("nope", id="root-string"),
            pytest.param({"dependencies": None}, id="dependencies-null"),
            pytest.param({"dependencies": {}}, id="dependencies-object"),
            pytest.param({"dependencies": [42]}, id="dependency-not-object"),
            pytest.param(
                {"dependencies": [{"name": "x", "vulns": 5}]},
                id="vulns-not-list",
            ),
            pytest.param(
                {"dependencies": [{"name": "x", "vulns": [7]}]},
                id="vuln-not-object",
            ),
            pytest.param(
                {"dependencies": [{"name": 42, "vulns": []}]},
                id="name-not-string",
            ),
            pytest.param(
                {"dependencies": [{"vulns": [{"id": "V", "fix_versions": ["1.0"]}]}]},
                id="name-missing",
            ),
            pytest.param(
                {
                    "dependencies": [
                        {"name": "", "vulns": [{"id": "V", "fix_versions": ["1.0"]}]}
                    ]
                },
                id="name-empty",
            ),
            pytest.param(
                {"dependencies": [{"name": "x", "vulns": [{"fix_versions": None}]}]},
                id="fix-versions-null",
            ),
            pytest.param(
                {"dependencies": [{"name": "x", "vulns": [{"fix_versions": 5}]}]},
                id="fix-versions-not-list",
            ),
            pytest.param(
                {"dependencies": [{"name": "x", "vulns": [{"id": 1}]}]},
                id="id-not-string",
            ),
            pytest.param(
                {"dependencies": [{"name": "x", "vulns": [{"id": ["a"]}]}]},
                id="id-unhashable",
            ),
            pytest.param(
                {"dependencies": [{"name": "x", "vulns": [{"fix_versions": ["1.0"]}]}]},
                id="id-missing",
            ),
            pytest.param(
                {
                    "dependencies": [
                        {"name": "x", "vulns": [{"id": "", "fix_versions": ["1.0"]}]}
                    ]
                },
                id="id-empty",
            ),
            pytest.param(
                {
                    "dependencies": [
                        {"name": "x", "vulns": [{"id": "V", "fix_versions": [42]}]}
                    ]
                },
                id="fix-version-item-not-string",
            ),
        ],
    )
    def test_structurally_invalid_audit_json_is_tooling_error(
        self, tmp_path: Path, payload: object
    ) -> None:
        # Syntactically valid JSON of the wrong shape must exit 2 (the documented
        # tooling error), never crash a downstream consumer with an
        # AttributeError or TypeError (in classify_audit, the reaudit dedup, or
        # render_report). The root and every leaf that a consumer dereferences
        # type-specifically is checked at the single obtain_audit JSON boundary,
        # so both the pass-1 and the governed re-audit inherit the same guard.
        bad = _write_json(tmp_path / "audit.json", payload)
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

    def test_well_formed_nested_audit_passes_shape_validation(
        self, tmp_path: Path
    ) -> None:
        # Guard against an over-strict validator: a fully populated, well-formed
        # report (nested dependency and vuln objects) must still classify and,
        # with only an unfixable owned finding, exit 0 rather than be rejected.
        assert self._run(tmp_path, _audit("ecdsa", "0.19.2", "CVE-OK-1", [])) == 0

    def test_pip_audit_exit_gt_1_is_tooling_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A pip-audit exit code > 1 is a tooling/network failure and must map to
        # exit 2, never to a spurious block. No --audit-json, so main() takes
        # the live branch and invokes the (patched) runner.
        monkeypatch.setattr(
            audit_manifest, "run_pip_audit", lambda _reqs, _out, **_kw: 2
        )
        rc = audit_manifest.main(
            [
                "--manifest",
                str(MANIFEST),
                "--ha-constraints",
                str(self._constraints(tmp_path)),
            ]
        )
        assert rc == 2

    def test_ha_governed_transitive_finding_does_not_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Wiring proof for the P3 fix: main() must pass the FULL HA-governed name
        # set to classify_audit, not only the HA-governed manifest entries. A
        # fixable CVE in a package HA pins transitively (yarl, not a manifest
        # entry) must surface as a non-blocking governed INFO, not a blocking
        # transitive finding. If main() passed only the manifest-intersection
        # set, yarl would exit 1 here.
        constraints = tmp_path / "ha_constraints.txt"
        constraints.write_text("aiohttp==3.13.3\nyarl==1.9.0\n", encoding="utf-8")
        audit_path = _write_json(
            tmp_path / "audit.json",
            _audit("yarl", "1.9.0", "CVE-YARL-WIRE", ["1.10.0"]),
        )
        rc = audit_manifest.main(
            [
                "--manifest",
                str(MANIFEST),
                "--ha-constraints",
                str(constraints),
                "--audit-json",
                str(audit_path),
                "--verbose",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "BLOCKING (0)" in out
        assert "CVE-YARL-WIRE" in out

    def test_governed_transitive_reaudit_surfaces_ha_pinned_cve(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Two-pass wiring proof: yarl is HA-governed but only *transitive* (via
        # aiohttp), so ha_pin_requirements never pins it and pass 1 resolves it
        # to 1.24.5 (above HA's 1.20.1 pin) with NO vuln -- a CVE that lives in
        # HA's pinned 1.20.1 would silently vanish. The second, --no-deps pass
        # re-audits yarl==1.20.1 (supplied here via --governed-audit-json) and
        # finds the fixable CVE. It must surface in the governed INFO bucket at
        # HA's own pin, and -- governed never blocks -- the gate stays green.
        constraints = tmp_path / "ha_constraints.txt"
        constraints.write_text("aiohttp==3.13.3\nyarl==1.20.1\n", encoding="utf-8")
        pass1 = _write_json(
            tmp_path / "pass1.json",
            {"dependencies": [{"name": "yarl", "version": "1.24.5", "vulns": []}]},
        )
        pass2 = _write_json(
            tmp_path / "pass2.json",
            _audit("yarl", "1.20.1", "CVE-YARL-PIN", ["1.20.2"]),
        )
        rc = audit_manifest.main(
            [
                "--manifest",
                str(MANIFEST),
                "--ha-constraints",
                str(constraints),
                "--audit-json",
                str(pass1),
                "--governed-audit-json",
                str(pass2),
                "--verbose",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "BLOCKING (0)" in out
        assert "CVE-YARL-PIN" in out
        assert "Home Assistant's own minimum-version pin" in out

    def test_range_floored_governed_transitive_reaudited_at_floor(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Option B: HA range-constrains urllib3 (>=2.0), a transitive reached via
        # gpsoauth. Pass 1 resolves urllib3 to 2.5.0 (above the 2.0 floor) with
        # no vuln, so a CVE living in the still-permitted 2.0 would vanish. The
        # range floor still enters the governed pins (so reachable selection
        # picks urllib3 up and the second --no-deps pass re-audits urllib3==2.0),
        # but because HA governs it only with a *range* -- not an exact ``==`` --
        # the surfaced CVE belongs in the actionable ``governed_range`` bucket,
        # NOT the unfixable exact-``==`` governed-INFO bucket. It stays
        # non-blocking (HA-ecosystem transitives are deliberately deferred to
        # HA), and the report offers the honest ``name>=<fixed>`` remedy rather
        # than the false "the integration cannot bump these".
        constraints = tmp_path / "ha_constraints.txt"
        constraints.write_text("aiohttp==3.13.3\nurllib3>=2.0\n", encoding="utf-8")
        pass1 = _write_json(
            tmp_path / "pass1.json",
            {"dependencies": [{"name": "urllib3", "version": "2.5.0", "vulns": []}]},
        )
        pass2 = _write_json(
            tmp_path / "pass2.json",
            _audit("urllib3", "2.0", "CVE-URLLIB3-FLOOR", ["2.0.7"]),
        )
        rc = audit_manifest.main(
            [
                "--manifest",
                str(MANIFEST),
                "--ha-constraints",
                str(constraints),
                "--audit-json",
                str(pass1),
                "--governed-audit-json",
                str(pass2),
                "--verbose",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "BLOCKING (0)" in out
        assert "CVE-URLLIB3-FLOOR" in out
        # The CVE is rendered under the range-governed section...
        assert "range-governed dependencies" in out
        assert out.index("range-governed dependencies") < out.index("CVE-URLLIB3-FLOOR")
        assert "COULD pin tighter" in out
        # ...and NOT under the unfixable exact-``==`` governed-INFO section: with
        # no exact-governed finding present, its "cannot bump" header must be
        # absent entirely, proving the CVE did not land in the old bucket.
        assert "cannot bump these" not in out
        # The GitHub annotation branch for range-governed findings is emitted as
        # a ``::notice`` (not the render section title, which shares the
        # "range-governed dependencies" phrase); asserting the annotation prefix
        # keeps the emit_github_annotations branch mutation-covered, not merely
        # line-covered.
        assert "::notice title=Vulnerabilities in Home Assistant range-governed" in out

    def test_no_governed_transitive_skips_second_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An empty reachable set must not trigger the second pass at all: no
        # --governed-audit-json is supplied, and run_pip_audit is booby-trapped
        # to fail if the live re-audit path is ever taken. yarl is already at
        # HA's 1.20.1 pin here, so reachable is empty and the existing behavior
        # is unchanged (exit 0). Dropping the version==pin guard in
        # reachable_governed_transitive_pins would make yarl "reachable" and trip
        # the booby trap.
        constraints = tmp_path / "ha_constraints.txt"
        constraints.write_text("aiohttp==3.13.3\nyarl==1.20.1\n", encoding="utf-8")
        audit_path = _write_json(
            tmp_path / "audit.json",
            {"dependencies": [{"name": "yarl", "version": "1.20.1", "vulns": []}]},
        )

        def _boom(*_args: object, **_kwargs: object) -> int:
            raise AssertionError("second pass must not invoke pip-audit")

        monkeypatch.setattr(audit_manifest, "run_pip_audit", _boom)
        rc = audit_manifest.main(
            [
                "--manifest",
                str(MANIFEST),
                "--ha-constraints",
                str(constraints),
                "--audit-json",
                str(audit_path),
            ]
        )
        assert rc == 0

    def test_offline_audit_json_requires_governed_report_for_reaudit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sibling of test_no_governed_transitive_skips_second_pass with the one
        # difference that matters: yarl resolves in pass 1 to a version other
        # than HA's pin, so reachable is non-empty and a second pass is
        # required. --audit-json promises "no network", so main() must not go
        # live; without --governed-audit-json it returns tooling exit 2 instead
        # of silently invoking pip-audit or silently dropping the second pass.
        # run_pip_audit is booby-trapped to prove no network call is made.
        constraints = tmp_path / "ha_constraints.txt"
        constraints.write_text("aiohttp==3.13.3\nyarl==1.20.1\n", encoding="utf-8")
        audit_path = _write_json(
            tmp_path / "audit.json",
            {"dependencies": [{"name": "yarl", "version": "1.24.5", "vulns": []}]},
        )

        def _boom(*_args: object, **_kwargs: object) -> int:
            raise AssertionError("offline --audit-json must not invoke pip-audit")

        monkeypatch.setattr(audit_manifest, "run_pip_audit", _boom)
        rc = audit_manifest.main(
            [
                "--manifest",
                str(MANIFEST),
                "--ha-constraints",
                str(constraints),
                "--audit-json",
                str(audit_path),
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


def test_main_pins_governed_requirement_to_ha_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() audits a governed package at HA's pin, an owned one at its floor.

    Wiring-level proof of Fund 2 (not the isolated helper): the requirement list
    the production path hands to pip-audit must carry Home Assistant's ``==``
    pin for the governed package (aiohttp) and the declared floor for an
    integration-owned package (selenium), never the governed floor. No
    ``--audit-json``, so main() takes the live branch and invokes the patched
    runner.
    """
    constraints = tmp_path / "ha_constraints.txt"
    constraints.write_text("aiohttp==3.12.15\n", encoding="utf-8")

    captured: dict[str, list[str]] = {}

    def fake_run(reqs: list[str], out_path: Path, *, no_deps: bool = False) -> int:
        captured["reqs"] = list(reqs)
        out_path.write_text('{"dependencies": []}', encoding="utf-8")
        return 0

    monkeypatch.setattr(audit_manifest, "run_pip_audit", fake_run)
    rc = audit_manifest.main(
        ["--manifest", str(MANIFEST), "--ha-constraints", str(constraints)]
    )

    assert rc == 0
    reqs = captured["reqs"]
    # Governed package pinned to HA's runtime version, not its manifest floor.
    assert "aiohttp==3.12.15" in reqs
    assert "aiohttp==3.11.8" not in reqs
    # An integration-owned package is still pinned to its declared floor.
    assert "selenium==4.25.0" in reqs


def test_main_reaudits_governed_transitive_with_no_deps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live two-pass actually shells the second pass with ``no_deps=True``.

    Wiring proof for Fund 2's live path (not the ``--governed-audit-json``
    offline shortcut): when a governed transitive (yarl) resolves in pass 1 to a
    version other than Home Assistant's pin, main() must run a *second*
    pip-audit pass over ``yarl==<HA pin>`` with ``no_deps=True``. A regression
    that drops the flag or skips the second pass fails this test.
    """
    constraints = tmp_path / "ha_constraints.txt"
    constraints.write_text("yarl==1.20.1\n", encoding="utf-8")

    calls: list[dict[str, object]] = []

    def fake_run(reqs: list[str], out_path: Path, *, no_deps: bool = False) -> int:
        calls.append({"reqs": list(reqs), "no_deps": no_deps})
        if not no_deps:
            # Pass 1: the resolver pulls yarl at a newer version than HA's pin.
            out_path.write_text(
                json.dumps(
                    {
                        "dependencies": [
                            {"name": "yarl", "version": "1.24.5", "vulns": []}
                        ]
                    }
                ),
                encoding="utf-8",
            )
        else:
            # Pass 2 at HA's pin surfaces a fixable CVE -> non-blocking governed.
            out_path.write_text(
                json.dumps(
                    {
                        "dependencies": [
                            {
                                "name": "yarl",
                                "version": "1.20.1",
                                "vulns": [
                                    {"id": "CVE-YARL-PIN", "fix_versions": ["1.24.5"]}
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
        return 0

    monkeypatch.setattr(audit_manifest, "run_pip_audit", fake_run)
    rc = audit_manifest.main(
        ["--manifest", str(MANIFEST), "--ha-constraints", str(constraints)]
    )

    assert rc == 0
    # Exactly two passes fired, the second with no_deps over HA's own pin.
    assert len(calls) == 2
    assert calls[0]["no_deps"] is False
    assert calls[1]["no_deps"] is True
    assert calls[1]["reqs"] == ["yarl==1.20.1"]


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
