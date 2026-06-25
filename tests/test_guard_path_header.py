# tests/test_guard_path_header.py
"""Guard requiring the repository-relative path header on every test file.

``AGENTS.md`` (Scope & authority, File headers) mandates that every Python file in
scope begin with a single-line comment containing its repository-relative path (after
an optional shebang), e.g. ``# tests/test_example.py``. The convention is a plain text
rule with no linter behind it, so omissions pass ``ruff``/``mypy``/``codespell``
silently and only surface in review. This guard fails loudly instead, mirroring the
sibling ``test_guard_async_test_marker`` guard: the legacy files below are frozen as a
documented backlog, and no new violations may be added outside the allowlist.

Scope note: this guard covers ``tests/**`` only, matching the sibling test guards and
the directory where the omission has recurred. Generated modules and the wider
``custom_components`` tree retain the lenient retrofit posture described in AGENTS.md.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TESTS_DIR = _REPO_ROOT / "tests"

# Pre-existing test files that predate this guard and still lack the path header.
# Do not add new entries; add the header to new files instead. Shrinking this list
# (by retrofitting headers during unrelated fixes, as AGENTS.md invites) is welcome.
LEGACY_ALLOWLIST: set[str] = {
    "tests/conftest.py",
    "tests/helpers/cache.py",
    "tests/helpers/config_entries_stub.py",
    "tests/helpers/stub_coordinator_debug.py",
    "tests/helpers/test_config_entries_stub.py",
    "tests/test_bandit_security.py",
    "tests/test_binary_sensor_subentry_setup.py",
    "tests/test_blocking_imports.py",
    "tests/test_broken_pipe_regression.py",
    "tests/test_button_restore_state.py",
    "tests/test_callback_stub_lint.py",
    "tests/test_config_entry_startup.py",
    "tests/test_config_flow_fcm_extraction.py",
    "tests/test_config_flow_reconfigure.py",
    "tests/test_coordinator_cache.py",
    "tests/test_coordinator_cache_merge.py",
    "tests/test_coordinator_cache_update.py",
    "tests/test_coordinator_geo.py",
    "tests/test_coordinator_identity.py",
    "tests/test_coordinator_key_priority.py",
    "tests/test_coordinator_polling.py",
    "tests/test_coordinator_predictive_polling.py",
    "tests/test_coordinator_priority.py",
    "tests/test_coordinator_registry.py",
    "tests/test_coordinator_registry_entity.py",
    "tests/test_coordinator_semantic_mappings.py",
    "tests/test_coordinator_stats.py",
    "tests/test_coordinator_subentry.py",
    "tests/test_coordinator_subentry_index.py",
    "tests/test_coordinator_timeout.py",
    "tests/test_coordinator_update.py",
    "tests/test_decoder_location_ranking.py",
    "tests/test_device_entity_registration.py",
    "tests/test_device_tracker_subentry_setup.py",
    "tests/test_dual_key_strategy.py",
    "tests/test_eid_resolver_owner_key_refresh.py",
    "tests/test_entity_recovery_manager.py",
    "tests/test_fcm_receiver_canonic_id.py",
    "tests/test_fcm_receiver_guard.py",
    "tests/test_fcm_receiver_thread_safety.py",
    "tests/test_fmdn_finder_bermuda_listener.py",
    "tests/test_fmdn_finder_location_uploader.py",
    "tests/test_homeassistant_callback_stub_helper.py",
    "tests/test_import_smoke.py",
    "tests/test_key_propagation_flow.py",
    "tests/test_map_view_features.py",
    "tests/test_metadata_helpers.py",
    "tests/test_options_flow_semantic_locations.py",
    "tests/test_phone_device_key_handling.py",
    "tests/test_pip_audit_security.py",
    "tests/test_python315_compat_guard.py",
    "tests/test_semantic_location_translations.py",
    "tests/test_sensor_config_subentry_forwarding.py",
    "tests/test_sensor_subentry_setup.py",
    "tests/test_spot_grpc_client.py",
    "tests/test_spot_grpc_request_logic.py",
    "tests/test_spot_grpc_resilience.py",
    "tests/test_spot_grpc_transport.py",
    "tests/test_spot_transport_lifecycle.py",
    "tests/test_token_cache_context.py",
}


def _has_path_header(path: Path, rel: str) -> bool:
    """Whether ``path``'s first non-shebang line is its repository-relative comment."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        lines = handle.read().splitlines()
    index = 1 if (lines and lines[0].startswith("#!")) else 0
    first = lines[index].strip() if index < len(lines) else ""
    return first == f"# {rel}"


def _iter_test_files() -> list[tuple[Path, str]]:
    """Every ``.py`` file under ``tests/`` as (absolute path, repo-relative posix)."""
    files: list[tuple[Path, str]] = []
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        files.append((path, rel))
    return files


def test_new_test_files_carry_repository_path_header() -> None:
    """No new test file may ship without the AGENTS.md repository-relative path header."""
    offenders = [
        rel
        for path, rel in _iter_test_files()
        if rel not in LEGACY_ALLOWLIST and not _has_path_header(path, rel)
    ]
    assert not offenders, (
        "Test files missing the AGENTS.md repository-relative path header "
        f"(add `# <path>` as the first line): {offenders}"
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Allowlisted files that gained a header (or vanished) must leave the allowlist."""
    known = {rel for _, rel in _iter_test_files()}
    stale = sorted(
        rel
        for rel in LEGACY_ALLOWLIST
        if rel not in known
        or _has_path_header(_REPO_ROOT / rel, rel)
    )
    assert not stale, (
        "Stale LEGACY_ALLOWLIST entries (file now has a header or no longer exists); "
        f"remove them to keep the backlog honest: {stale}"
    )
