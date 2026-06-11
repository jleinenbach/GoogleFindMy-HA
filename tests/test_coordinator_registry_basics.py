# tests/test_coordinator_registry_basics.py
"""Branch-coverage tests for ``RegistryOperations`` mixin methods M1-M14 + M17.

PR 1.A.1b (AP-1.1a-β). Scope: ``coordinator/registry.py`` lines 121-499
(M1-M14) and lines 1289-1293 (M17). M15/M16/M18 land in PR 1.A.2.

Aniche-style adequacy progression: Specification → Boundary → Structural.
:class:`RegistryStub` (``tests.helpers.registry_mixin_stub``) seeds every
attribute ``_MixinBase`` declares and pre-mocks the cross-mixin
``NotImplementedError`` traps (notes sidecar risk R-NEU-3).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.const import (
    DOMAIN,
    LEGACY_SERVICE_IDENTIFIER,
    SERVICE_DEVICE_IDENTIFIER_PREFIX,
)
from custom_components.googlefindmy.coordinator import registry as registry_mod
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.registry_mixin_stub import (
    RegistryStub,
    _DevRegStub,
    _EntRegStub,
    make_hass_stub,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def coord() -> RegistryStub:
    """Return a default :class:`RegistryStub` bound to a synthetic config entry."""

    entry = make_config_entry(entry_id="entry-xyz")
    return RegistryStub(hass=make_hass_stub(), config_entry=entry)


@pytest.fixture
def coord_no_entry() -> RegistryStub:
    """Return a stub without a bound ``ConfigEntry`` (early-startup state)."""

    return RegistryStub(hass=make_hass_stub(), config_entry=None)


# ---------------------------------------------------------------------------
# M14 ``_redact_text`` (3 documented branches) — start with the trivial Pure
# function so a smoke failure surfaces as a stub problem, not a Mixin issue.
# ---------------------------------------------------------------------------


class TestRedactText:
    """B1: ``None``/empty → ``''``; B2: short → identity; B3: long → truncated."""

    def test_none_returns_empty(self, coord: RegistryStub) -> None:
        assert coord._redact_text(None) == ""

    def test_empty_string_returns_empty(self, coord: RegistryStub) -> None:
        # `not value` short-circuits on the empty string (falsy);
        # whitespace-only strings are truthy and pass through unchanged.
        assert coord._redact_text("") == ""

    @pytest.mark.parametrize("value", ["   ", "\t"])
    def test_whitespace_is_truthy_and_passes_through(
        self, coord: RegistryStub, value: str
    ) -> None:
        # Documents the current contract: only Python-falsy values short-circuit.
        # Whitespace strings are returned verbatim (no .strip() in production).
        assert coord._redact_text(value) == value

    def test_short_returns_identity(self, coord: RegistryStub) -> None:
        assert coord._redact_text("hello") == "hello"

    def test_long_is_truncated_with_ellipsis(self, coord: RegistryStub) -> None:
        long = "x" * 200
        out = coord._redact_text(long, max_len=120)
        assert out == "x" * 120 + "…"
        assert len(out) == 121  # 120 chars + ellipsis

    def test_default_max_len_is_120(self, coord: RegistryStub) -> None:
        assert coord._redact_text("x" * 121).endswith("…")
        assert coord._redact_text("x" * 120) == "x" * 120


# ---------------------------------------------------------------------------
# M12 ``_entry_id`` (3 branches) + M13 ``_config_entry_exists`` (5 branches)
# ---------------------------------------------------------------------------


class TestEntryId:
    """B1: no entry → None; B2: entry without entry_id → None; B3: present → str."""

    def test_no_entry_returns_none(self, coord_no_entry: RegistryStub) -> None:
        assert coord_no_entry._entry_id() is None

    def test_entry_without_attr_returns_none(self) -> None:
        coord = RegistryStub(config_entry=SimpleNamespace())  # no entry_id attr
        assert coord._entry_id() is None

    def test_entry_with_id_returns_str(self, coord: RegistryStub) -> None:
        assert coord._entry_id() == "entry-xyz"


class TestConfigEntryExists:
    """5 branches: missing id, no hass, getter ok/None/raises."""

    def test_no_entry_id_returns_false(self, coord_no_entry: RegistryStub) -> None:
        coord_no_entry.hass = None
        assert coord_no_entry._config_entry_exists() is False

    def test_no_hass_returns_true_defensively(self, coord: RegistryStub) -> None:
        coord.hass = None
        assert coord._config_entry_exists() is True

    def test_getter_returns_entry(self, coord: RegistryStub) -> None:
        coord.hass.config_entries.async_get_entry = MagicMock(
            return_value=SimpleNamespace(entry_id="entry-xyz")
        )
        assert coord._config_entry_exists() is True

    def test_getter_returns_none(self, coord: RegistryStub) -> None:
        coord.hass.config_entries.async_get_entry = MagicMock(return_value=None)
        assert coord._config_entry_exists() is False

    def test_getter_raises_returns_true_defensively(
        self, coord: RegistryStub
    ) -> None:
        def _boom(_eid: str) -> Any:
            raise RuntimeError("registry corrupted")

        coord.hass.config_entries.async_get_entry = _boom
        assert coord._config_entry_exists() is True

    def test_no_callable_getter_returns_true(self, coord: RegistryStub) -> None:
        coord.hass.config_entries.async_get_entry = "not-callable"
        assert coord._config_entry_exists() is True

    def test_explicit_entry_id_argument(self, coord: RegistryStub) -> None:
        coord.hass.config_entries.async_get_entry = MagicMock(return_value=object())
        assert coord._config_entry_exists("custom-id") is True
        coord.hass.config_entries.async_get_entry.assert_called_once_with("custom-id")


# ---------------------------------------------------------------------------
# M9 ``_ensure_device_name_cache`` (2 branches) + M10 ``_apply_pending_via_updates``
# ---------------------------------------------------------------------------


class TestEnsureDeviceNameCache:
    """B1: not initialized → fresh dict; B2: initialized → identity."""

    def test_first_call_initializes_empty_dict(self, coord: RegistryStub) -> None:
        cache = coord._ensure_device_name_cache()
        assert cache == {}
        assert coord._device_names is cache

    def test_second_call_returns_same_object(self, coord: RegistryStub) -> None:
        first = coord._ensure_device_name_cache()
        first["abc"] = "Alice"
        second = coord._ensure_device_name_cache()
        assert second is first
        assert second["abc"] == "Alice"


class TestApplyPendingViaUpdates:
    """M10 is a documented no-op for backward compatibility."""

    def test_returns_none_without_side_effects(self, coord: RegistryStub) -> None:
        snapshot = vars(coord).copy()
        assert coord._apply_pending_via_updates() is None
        assert vars(coord) == snapshot


# ---------------------------------------------------------------------------
# M11 ``_device_display_name`` (passthrough)
# ---------------------------------------------------------------------------


class TestDeviceDisplayName:
    """Passes through to ``extract_device_display_name`` helper."""

    def test_prefers_name_by_user(self, coord: RegistryStub) -> None:
        dev = SimpleNamespace(name_by_user="Custom", name="Default")
        assert coord._device_display_name(dev, "fallback") == "Custom"

    def test_falls_back_to_name(self, coord: RegistryStub) -> None:
        dev = SimpleNamespace(name_by_user=None, name="Default")
        assert coord._device_display_name(dev, "fallback") == "Default"

    def test_uses_fallback_when_both_empty(self, coord: RegistryStub) -> None:
        dev = SimpleNamespace(name_by_user=None, name=None)
        assert coord._device_display_name(dev, "fallback") == "fallback"


# ---------------------------------------------------------------------------
# M3 ``_device_registry_build_legacy_kwargs`` (delegation) + M2 retry detection
# ---------------------------------------------------------------------------


class TestBuildLegacyKwargs:
    """Static-passthrough to ``build_legacy_device_registry_kwargs``."""

    def test_translates_modern_keys(self) -> None:
        out = RegistryStub._device_registry_build_legacy_kwargs(
            {"add_config_entry_id": "e1", "add_config_subentry_id": "s1"}
        )
        assert out == {"config_entry_id": "e1", "config_subentry_id": "s1"}

    def test_drops_remove_config_subentry_id(self) -> None:
        out = RegistryStub._device_registry_build_legacy_kwargs(
            {"remove_config_subentry_id": "s1", "name": "Phone"}
        )
        assert out == {"name": "Phone"}

    def test_does_not_mutate_input(self) -> None:
        original = {"add_config_entry_id": "e1"}
        RegistryStub._device_registry_build_legacy_kwargs(original)
        assert original == {"add_config_entry_id": "e1"}


class TestKwargsNeedLegacyRetry:
    """Delegates to ``needs_legacy_kwarg_retry`` helper after kwarg-name lookup."""

    def test_modern_registry_does_not_retry(self, coord: RegistryStub) -> None:
        def modern_api(*, add_config_subentry_id: str | None = None) -> None: ...

        err = TypeError("add_config_subentry_id rejected")
        assert (
            coord._device_registry_kwargs_need_legacy_retry(
                modern_api, err, {"add_config_subentry_id": "s1"}
            )
            is False
        )

    def test_legacy_registry_triggers_retry(self, coord: RegistryStub) -> None:
        def legacy_api(*, config_subentry_id: str | None = None) -> None: ...

        err = TypeError("got unexpected keyword argument 'add_config_entry_id'")
        assert (
            coord._device_registry_kwargs_need_legacy_retry(
                legacy_api, err, {"add_config_entry_id": "e1"}
            )
            is True
        )


# ---------------------------------------------------------------------------
# M4 ``_device_registry_config_subentry_kwarg_name`` (7+ branches)
# ---------------------------------------------------------------------------


class TestConfigSubentryKwargName:
    """Cover signature-based detection, caching, and defensive fallbacks."""

    def test_returns_config_subentry_id_when_present(
        self, coord: RegistryStub
    ) -> None:
        def api(*, config_subentry_id: str | None = None) -> None: ...

        assert (
            coord._device_registry_config_subentry_kwarg_name(api)
            == "config_subentry_id"
        )

    def test_returns_add_config_subentry_id_when_present(
        self, coord: RegistryStub
    ) -> None:
        def api(*, add_config_subentry_id: str | None = None) -> None: ...

        assert (
            coord._device_registry_config_subentry_kwarg_name(api)
            == "add_config_subentry_id"
        )

    def test_var_keyword_returns_config_subentry_id(
        self, coord: RegistryStub
    ) -> None:
        def api(**kwargs: Any) -> None: ...

        assert (
            coord._device_registry_config_subentry_kwarg_name(api)
            == "config_subentry_id"
        )

    def test_unrelated_signature_returns_none(self, coord: RegistryStub) -> None:
        def api(a: int, b: int) -> None: ...

        assert coord._device_registry_config_subentry_kwarg_name(api) is None

    def test_signature_typeerror_returns_none(
        self, coord: RegistryStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def api() -> None: ...

        def _raise(_call: Any) -> Any:
            raise TypeError("C extension")

        monkeypatch.setattr(inspect, "signature", _raise)
        assert coord._device_registry_config_subentry_kwarg_name(api) is None

    def test_signature_valueerror_returns_none(
        self, coord: RegistryStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def api() -> None: ...

        def _raise(_call: Any) -> Any:
            raise ValueError("no signature")

        monkeypatch.setattr(inspect, "signature", _raise)
        assert coord._device_registry_config_subentry_kwarg_name(api) is None

    def test_cache_hit_avoids_second_signature_call(
        self, coord: RegistryStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def api(*, config_subentry_id: str | None = None) -> None: ...

        original_signature = inspect.signature
        call_count = {"n": 0}

        def _counting(call: Any) -> Any:
            call_count["n"] += 1
            return original_signature(call)

        monkeypatch.setattr(inspect, "signature", _counting)

        assert coord._device_registry_config_subentry_kwarg_name(api) == (
            "config_subentry_id"
        )
        assert coord._device_registry_config_subentry_kwarg_name(api) == (
            "config_subentry_id"
        )
        assert call_count["n"] == 1

    def test_cache_resets_when_attribute_is_not_a_dict(
        self, coord: RegistryStub
    ) -> None:
        coord._device_registry_config_subentry_kwarg_cache = "not-a-dict"  # type: ignore[assignment]

        def api(*, config_subentry_id: str | None = None) -> None: ...

        assert (
            coord._device_registry_config_subentry_kwarg_name(api)
            == "config_subentry_id"
        )
        assert isinstance(coord._device_registry_config_subentry_kwarg_cache, dict)

    def test_bound_method_uses_underlying_func_for_cache_key(
        self, coord: RegistryStub
    ) -> None:
        class _Api:
            def call(self, *, config_subentry_id: str | None = None) -> None: ...

        a = _Api()
        b = _Api()
        # First call seeds the cache via the unbound function.
        assert (
            coord._device_registry_config_subentry_kwarg_name(a.call)
            == "config_subentry_id"
        )
        cache = coord._device_registry_config_subentry_kwarg_cache
        assert isinstance(cache, dict)
        assert len(cache) == 1
        # Different bound instance, same underlying ``__func__`` → reuses entry.
        assert (
            coord._device_registry_config_subentry_kwarg_name(b.call)
            == "config_subentry_id"
        )
        assert len(cache) == 1


# ---------------------------------------------------------------------------
# M5 ``_device_registry_allows_translation_update`` (5 branches)
# ---------------------------------------------------------------------------


class TestAllowsTranslationUpdate:
    """Detect translation kwarg support in ``async_update_device``."""

    def test_cached_true_returned_directly(self, coord: RegistryStub) -> None:
        coord._device_registry_supports_translation_update = True
        assert coord._device_registry_allows_translation_update(MagicMock()) is True

    def test_cached_false_returned_directly(self, coord: RegistryStub) -> None:
        coord._device_registry_supports_translation_update = False
        assert coord._device_registry_allows_translation_update(MagicMock()) is False

    def test_missing_update_helper_returns_false(
        self, coord: RegistryStub
    ) -> None:
        dev_reg = SimpleNamespace()  # no async_update_device attribute
        assert (
            coord._device_registry_allows_translation_update(dev_reg) is False
        )
        assert coord._device_registry_supports_translation_update is False

    def test_both_translation_params_returns_true(
        self, coord: RegistryStub
    ) -> None:
        def async_update_device(
            device_id: str,
            *,
            translation_key: str | None = None,
            translation_placeholders: dict[str, str] | None = None,
        ) -> None: ...

        dev_reg = SimpleNamespace(async_update_device=async_update_device)
        assert coord._device_registry_allows_translation_update(dev_reg) is True

    def test_partial_translation_params_returns_false(
        self, coord: RegistryStub
    ) -> None:
        def async_update_device(
            device_id: str, *, translation_key: str | None = None
        ) -> None: ...

        dev_reg = SimpleNamespace(async_update_device=async_update_device)
        assert (
            coord._device_registry_allows_translation_update(dev_reg) is False
        )

    def test_signature_failure_returns_false(
        self, coord: RegistryStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dev_reg = SimpleNamespace(async_update_device=lambda *a, **k: None)

        def _raise(_call: Any) -> Any:
            raise TypeError("C extension")

        monkeypatch.setattr(inspect, "signature", _raise)
        assert (
            coord._device_registry_allows_translation_update(dev_reg) is False
        )


# ---------------------------------------------------------------------------
# M1 ``_call_device_registry_api`` (10 branches)
# ---------------------------------------------------------------------------


class TestCallDeviceRegistryApi:
    """B1..B10: kwargs handling, retry, fallback paths."""

    def test_no_base_kwargs_calls_with_empty_kwargs(
        self, coord: RegistryStub
    ) -> None:
        call = MagicMock(return_value="ok")
        assert coord._call_device_registry_api(call) == "ok"
        call.assert_called_once_with()

    def test_config_subentry_dropped_when_replacement_is_none(
        self, coord: RegistryStub
    ) -> None:
        def api(*, name: str) -> str:
            return f"name={name}"

        result = coord._call_device_registry_api(
            api,
            base_kwargs={"name": "Phone", "config_subentry_id": "s1"},
        )
        assert result == "name=Phone"

    def test_replacement_equal_to_config_subentry_id_no_rename(
        self, coord: RegistryStub
    ) -> None:
        captured: dict[str, Any] = {}

        def api(*, config_subentry_id: str) -> None:
            captured["seen"] = config_subentry_id

        coord._call_device_registry_api(api, base_kwargs={"config_subentry_id": "s1"})
        assert captured == {"seen": "s1"}

    def test_replacement_renames_kwarg(self, coord: RegistryStub) -> None:
        captured: dict[str, Any] = {}

        def api(*, add_config_subentry_id: str) -> None:
            captured["seen"] = add_config_subentry_id

        coord._call_device_registry_api(api, base_kwargs={"config_subentry_id": "s1"})
        assert captured == {"seen": "s1"}

    def test_typeerror_without_legacy_match_reraises(
        self, coord: RegistryStub
    ) -> None:
        def api(*, name: str) -> None:
            raise TypeError("unrelated error about 'name'")

        with pytest.raises(TypeError, match="unrelated error"):
            coord._call_device_registry_api(api, base_kwargs={"name": "Phone"})

    def test_typeerror_with_legacy_match_triggers_retry(
        self, coord: RegistryStub
    ) -> None:
        attempts: list[dict[str, Any]] = []

        def api(**kwargs: Any) -> str:
            attempts.append(kwargs.copy())
            if "add_config_entry_id" in kwargs:
                raise TypeError("got unexpected keyword 'add_config_entry_id'")
            return "ok"

        result = coord._call_device_registry_api(
            api, base_kwargs={"add_config_entry_id": "e1"}
        )
        assert result == "ok"
        assert len(attempts) == 2
        assert attempts[0] == {"add_config_entry_id": "e1"}
        assert attempts[1] == {"config_entry_id": "e1"}

    def test_unknown_subentry_with_add_config_kwarg_reraises(
        self, coord: RegistryStub
    ) -> None:
        from homeassistant.config_entries import UnknownSubEntry

        def api(*, add_config_subentry_id: str | None = None) -> None:
            raise UnknownSubEntry()

        with pytest.raises(UnknownSubEntry):
            coord._call_device_registry_api(
                api, base_kwargs={"config_subentry_id": "missing"}
            )

    def test_unknown_subentry_without_config_subentry_id_reraises(
        self, coord: RegistryStub
    ) -> None:
        from homeassistant.config_entries import UnknownEntry

        def api(*, name: str) -> None:
            raise UnknownEntry()

        with pytest.raises(UnknownEntry):
            coord._call_device_registry_api(api, base_kwargs={"name": "Phone"})

    def test_unknown_subentry_with_legacy_kwarg_falls_back(
        self, coord: RegistryStub
    ) -> None:
        from homeassistant.config_entries import UnknownSubEntry

        attempts: list[dict[str, Any]] = []

        def api(*, config_subentry_id: str | None = None, name: str = "") -> str:
            attempts.append(
                {"config_subentry_id": config_subentry_id, "name": name}
            )
            if config_subentry_id == "missing":
                raise UnknownSubEntry()
            return "ok"

        result = coord._call_device_registry_api(
            api,
            base_kwargs={"name": "Phone", "config_subentry_id": "missing"},
        )
        assert result == "ok"
        # First attempt with subentry, fallback without it.
        assert attempts[0]["config_subentry_id"] == "missing"
        assert attempts[1]["config_subentry_id"] is None
        assert attempts[1]["name"] == "Phone"


# ---------------------------------------------------------------------------
# M7 ``_extract_our_identifier`` (3 branches)
# ---------------------------------------------------------------------------


class TestExtractOurIdentifier:
    """Walks ``device.identifiers``; first valid wins, else ``None``."""

    def test_empty_identifiers_returns_none(self, coord: RegistryStub) -> None:
        device = SimpleNamespace(identifiers=set())
        assert coord._extract_our_identifier(device) is None

    def test_only_foreign_identifiers_returns_none(
        self, coord: RegistryStub
    ) -> None:
        device = SimpleNamespace(identifiers={("other-domain", "xyz")})
        assert coord._extract_our_identifier(device) is None

    def test_entry_scoped_identifier_returns_raw_device_id(
        self, coord: RegistryStub
    ) -> None:
        device = SimpleNamespace(
            identifiers={(DOMAIN, "entry-xyz:dev-1")}
        )
        assert coord._extract_our_identifier(device) == "dev-1"

    def test_legacy_identifier_returns_device_id(
        self, coord: RegistryStub
    ) -> None:
        device = SimpleNamespace(identifiers={(DOMAIN, "dev-legacy")})
        assert coord._extract_our_identifier(device) == "dev-legacy"

    def test_service_device_identifier_returns_none(
        self, coord: RegistryStub
    ) -> None:
        # SERVICE_DEVICE_IDENTIFIER_PREFIX-based identifiers are not tracker IDs;
        # the helper filters them out at parse time.
        device = SimpleNamespace(
            identifiers={
                (DOMAIN, f"{SERVICE_DEVICE_IDENTIFIER_PREFIX}entry-xyz")
            }
        )
        assert coord._extract_our_identifier(device) is None

    def test_legacy_service_identifier_returns_none(
        self, coord: RegistryStub
    ) -> None:
        device = SimpleNamespace(
            identifiers={(DOMAIN, LEGACY_SERVICE_IDENTIFIER)}
        )
        assert coord._extract_our_identifier(device) is None


# ---------------------------------------------------------------------------
# M8 ``_sync_owner_index`` (8+ branches)
# ---------------------------------------------------------------------------


class TestSyncOwnerIndex:
    """Cover early-exit, defensive, prune, and overwrite-guard branches."""

    def test_no_hass_returns_early(self, coord: RegistryStub) -> None:
        coord.hass = None
        # Should not raise.
        coord._sync_owner_index([{"canonicalId": "abc"}])

    def test_no_entry_id_returns_early(
        self, coord_no_entry: RegistryStub
    ) -> None:
        coord_no_entry._sync_owner_index([{"canonicalId": "abc"}])
        assert "device_owner_index" not in coord_no_entry.hass.data.get(DOMAIN, {})

    def test_setdefault_error_does_not_raise(self) -> None:
        coord = RegistryStub(
            hass=make_hass_stub(with_setdefault_error=True),
            config_entry=SimpleNamespace(entry_id="e1"),
        )
        # Defensive guard: error logged at DEBUG, no exception propagated.
        coord._sync_owner_index([{"canonicalId": "abc"}])

    def test_none_devices_is_no_op(self, coord: RegistryStub) -> None:
        coord._sync_owner_index(None)
        bucket = coord.hass.data.get(DOMAIN, {})
        assert bucket.get("device_owner_index", {}) == {}

    def test_canonical_id_field_wins(self, coord: RegistryStub) -> None:
        coord._sync_owner_index(
            [
                {"canonicalId": "cid-1"},
                {"canonical_id": "cid-2"},
                {"id": "id-3"},
                {"device_id": "id-4"},
            ]
        )
        index = coord.hass.data[DOMAIN]["device_owner_index"]
        assert index == {
            "cid-1": "entry-xyz",
            "cid-2": "entry-xyz",
            "id-3": "entry-xyz",
            "id-4": "entry-xyz",
        }

    def test_missing_id_skipped(self, coord: RegistryStub) -> None:
        coord._sync_owner_index([{"unrelated": "value"}])
        assert coord.hass.data.get(DOMAIN, {}).get("device_owner_index", {}) == {}

    def test_non_string_canonical_is_coerced(self, coord: RegistryStub) -> None:
        coord._sync_owner_index([{"id": 12345}])
        assert (
            coord.hass.data[DOMAIN]["device_owner_index"]["12345"] == "entry-xyz"
        )

    def test_empty_canonical_after_strip_skipped(
        self, coord: RegistryStub
    ) -> None:
        coord._sync_owner_index([{"id": "   "}])
        assert coord.hass.data.get(DOMAIN, {}).get("device_owner_index", {}) == {}

    def test_existing_owner_from_other_entry_not_overwritten(
        self, coord: RegistryStub
    ) -> None:
        coord.hass.data[DOMAIN] = {
            "device_owner_index": {"shared": "entry-other"}
        }
        coord._sync_owner_index([{"id": "shared"}])
        assert (
            coord.hass.data[DOMAIN]["device_owner_index"]["shared"]
            == "entry-other"
        )

    def test_stale_entries_for_this_entry_are_pruned(
        self, coord: RegistryStub
    ) -> None:
        coord.hass.data[DOMAIN] = {
            "device_owner_index": {
                "stale": "entry-xyz",
                "fresh": "entry-xyz",
                "other": "entry-other",
            }
        }
        coord._sync_owner_index([{"id": "fresh"}])
        index = coord.hass.data[DOMAIN]["device_owner_index"]
        assert "stale" not in index  # pruned: belonged to this entry, not seen
        assert index["fresh"] == "entry-xyz"
        assert index["other"] == "entry-other"  # other entry never pruned


# ---------------------------------------------------------------------------
# M6 ``_reindex_poll_targets_from_device_registry`` (7 branches + cross-call)
# ---------------------------------------------------------------------------


class TestReindexPollTargets:
    """Drive every documented branch via monkeypatched dr/er modules."""

    @pytest.fixture
    def reindex_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[_DevRegStub, _EntRegStub]:
        dev_reg = _DevRegStub()
        ent_reg = _EntRegStub()
        monkeypatch.setattr(registry_mod.dr, "async_get", lambda _hass: dev_reg)
        monkeypatch.setattr(registry_mod.er, "async_get", lambda _hass: ent_reg)
        monkeypatch.setattr(
            registry_mod.dr,
            "async_entries_for_config_entry",
            lambda _reg, _eid: list(dev_reg.devices),
        )
        monkeypatch.setattr(
            registry_mod.er,
            "async_entries_for_config_entry",
            lambda _reg, _eid: list(ent_reg.entries),
        )
        return dev_reg, ent_reg

    def test_no_entry_id_resets_sets_and_returns(
        self,
        coord_no_entry: RegistryStub,
        reindex_env: tuple[_DevRegStub, _EntRegStub],
    ) -> None:
        coord_no_entry._devices_with_entry = {"stale"}
        coord_no_entry._enabled_poll_device_ids = {"stale"}
        coord_no_entry._reindex_poll_targets_from_device_registry()
        assert coord_no_entry._devices_with_entry == set()
        assert coord_no_entry._enabled_poll_device_ids == set()
        coord_no_entry._refresh_subentry_index.assert_not_called()

    def test_happy_path_two_devices_one_enabled(
        self,
        coord: RegistryStub,
        reindex_env: tuple[_DevRegStub, _EntRegStub],
    ) -> None:
        dev_reg, ent_reg = reindex_env
        d1 = dev_reg.add_device(identifiers={(DOMAIN, "entry-xyz:tracker-1")})
        d2 = dev_reg.add_device(identifiers={(DOMAIN, "entry-xyz:tracker-2")})
        ent_reg.add_entry(device_id=d1.id)
        # d2 has no entity → present but not enabled.
        # d1 has tracker entity → enabled.

        coord._reindex_poll_targets_from_device_registry()

        assert coord._devices_with_entry == {"tracker-1", "tracker-2"}
        assert coord._enabled_poll_device_ids == {"tracker-1"}
        coord._refresh_subentry_index.assert_called_once()
        coord._schedule_eid_resolver_refresh.assert_called_once()
        # Silence unused-fixture warning.
        assert d2.id in {dev.id for dev in dev_reg.devices}

    def test_disabled_entity_does_not_enable_device(
        self,
        coord: RegistryStub,
        reindex_env: tuple[_DevRegStub, _EntRegStub],
    ) -> None:
        dev_reg, ent_reg = reindex_env
        d1 = dev_reg.add_device(identifiers={(DOMAIN, "entry-xyz:t1")})
        ent_reg.add_entry(device_id=d1.id, disabled_by="user")

        coord._reindex_poll_targets_from_device_registry()
        assert coord._devices_with_entry == {"t1"}
        assert coord._enabled_poll_device_ids == set()

    def test_wrong_platform_entity_skipped(
        self,
        coord: RegistryStub,
        reindex_env: tuple[_DevRegStub, _EntRegStub],
    ) -> None:
        dev_reg, ent_reg = reindex_env
        d1 = dev_reg.add_device(identifiers={(DOMAIN, "entry-xyz:t1")})
        ent_reg.add_entry(platform="other", device_id=d1.id)

        coord._reindex_poll_targets_from_device_registry()
        assert coord._enabled_poll_device_ids == set()

    def test_wrong_domain_entity_skipped(
        self,
        coord: RegistryStub,
        reindex_env: tuple[_DevRegStub, _EntRegStub],
    ) -> None:
        dev_reg, ent_reg = reindex_env
        d1 = dev_reg.add_device(identifiers={(DOMAIN, "entry-xyz:t1")})
        ent_reg.add_entry(domain="sensor", device_id=d1.id)

        coord._reindex_poll_targets_from_device_registry()
        assert coord._enabled_poll_device_ids == set()

    def test_disabled_device_present_but_not_enabled(
        self,
        coord: RegistryStub,
        reindex_env: tuple[_DevRegStub, _EntRegStub],
    ) -> None:
        dev_reg, ent_reg = reindex_env
        d1 = dev_reg.add_device(
            identifiers={(DOMAIN, "entry-xyz:t1")}, disabled_by="user"
        )
        ent_reg.add_entry(device_id=d1.id)

        coord._reindex_poll_targets_from_device_registry()
        assert coord._devices_with_entry == {"t1"}
        assert coord._enabled_poll_device_ids == set()

    def test_device_without_known_identifier_skipped(
        self,
        coord: RegistryStub,
        reindex_env: tuple[_DevRegStub, _EntRegStub],
    ) -> None:
        dev_reg, _ent_reg = reindex_env
        dev_reg.add_device(identifiers={("other-domain", "stranger")})

        coord._reindex_poll_targets_from_device_registry()
        assert coord._devices_with_entry == set()


# ---------------------------------------------------------------------------
# M17 ``find_tracker_entity_entry`` (public wrapper)
# ---------------------------------------------------------------------------


class TestFindTrackerEntityEntry:
    """Wraps :meth:`_find_tracker_entity_entry`; pure delegation."""

    def test_passes_device_id_and_returns_result(
        self, coord: RegistryStub
    ) -> None:
        sentinel = SimpleNamespace(entity_id="device_tracker.x")
        coord._find_tracker_entity_entry = MagicMock(return_value=sentinel)  # type: ignore[method-assign]

        assert coord.find_tracker_entity_entry("dev-1") is sentinel
        coord._find_tracker_entity_entry.assert_called_once_with("dev-1")

    def test_returns_none_when_inner_returns_none(
        self, coord: RegistryStub
    ) -> None:
        coord._find_tracker_entity_entry = MagicMock(return_value=None)  # type: ignore[method-assign]
        assert coord.find_tracker_entity_entry("missing") is None
