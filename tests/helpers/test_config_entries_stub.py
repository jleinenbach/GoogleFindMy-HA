"""Smoke tests for the canonical ``make_config_entry`` factory.

These guard the documented defaults (``data``/``options`` always dict,
auto-generated ``entry_id``, ``**extra`` pass-through) so future migrations
to the factory can rely on them. See ``tests/AGENTS.md`` for the convention.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests.helpers.config_entries_stub import make_config_entry


def test_make_config_entry_returns_simplenamespace() -> None:
    entry = make_config_entry()
    assert isinstance(entry, SimpleNamespace)


def test_data_and_options_default_to_dict_not_none() -> None:
    entry = make_config_entry()
    assert entry.data == {}
    assert entry.options == {}
    assert isinstance(entry.data, dict)
    assert isinstance(entry.options, dict)


def test_data_and_options_are_copied_into_dict() -> None:
    src_data = {"oauth_token": "abc"}
    src_options = {"poll_interval": 60}
    entry = make_config_entry(data=src_data, options=src_options)

    assert entry.data == {"oauth_token": "abc"}
    assert entry.options == {"poll_interval": 60}
    entry.data["mutated"] = True
    assert "mutated" not in src_data


def test_entry_id_auto_generated_when_omitted() -> None:
    entry_a = make_config_entry()
    entry_b = make_config_entry()
    assert entry_a.entry_id.startswith("entry-test-")
    assert entry_b.entry_id.startswith("entry-test-")
    assert entry_a.entry_id != entry_b.entry_id


def test_entry_id_explicit_overrides_auto() -> None:
    entry = make_config_entry(entry_id="custom-id")
    assert entry.entry_id == "custom-id"


def test_default_domain_is_googlefindmy() -> None:
    entry = make_config_entry()
    assert entry.domain == "googlefindmy"


def test_explicit_kwargs_override_defaults() -> None:
    entry = make_config_entry(
        domain="other",
        title="My Account",
        unique_id="user@example.com",
        state="setup_in_progress",
        version=2,
        minor_version=3,
        source="reauth",
    )
    assert entry.domain == "other"
    assert entry.title == "My Account"
    assert entry.unique_id == "user@example.com"
    assert entry.state == "setup_in_progress"
    assert entry.version == 2
    assert entry.minor_version == 3
    assert entry.source == "reauth"


def test_extra_kwargs_are_passed_through() -> None:
    entry = make_config_entry(my_test_marker="present")
    assert entry.my_test_marker == "present"


def test_runtime_data_defaults_to_none() -> None:
    entry = make_config_entry()
    assert entry.runtime_data is None


def test_runtime_data_can_be_set() -> None:
    sentinel = object()
    entry = make_config_entry(runtime_data=sentinel)
    assert entry.runtime_data is sentinel
