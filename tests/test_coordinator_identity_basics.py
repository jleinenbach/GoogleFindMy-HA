# tests/test_coordinator_identity_basics.py
"""Branch-coverage tests for the simple ``IdentityOperations`` mixin methods.

PR 1.A.2 (Phase 2, AP-A). Scope: ``coordinator/identity.py`` lines 72-233
(``_get_account_email``, ``_create_auth_issue``, ``_dismiss_auth_issue``,
``_schedule_eid_resolver_refresh``, ``_register_identity_key``,
``_reset_resolver_offset``). The complex ``get_active_device_identities``
(lines 235-967) lands in a later AP.

Aniche-style adequacy progression: Specification → Boundary → Structural.
:class:`IdentityStub` (``tests.helpers.identity_mixin_stub``) seeds every
attribute ``_MixinBase`` declares and pre-mocks ``_entry_id``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_EID_RESOLVER,
    DOMAIN,
    ISSUE_AUTH_EXPIRED_KEY,
    issue_id_for,
)
from custom_components.googlefindmy.coordinator import identity as identity_mod
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.identity_mixin_stub import IdentityStub, make_hass_stub

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def coord() -> IdentityStub:
    """Return a default :class:`IdentityStub` bound to a synthetic config entry."""

    entry = make_config_entry(
        entry_id="entry-xyz",
        title="My GFM Account",
        data={CONF_GOOGLE_EMAIL: "user@example.com"},
    )
    return IdentityStub(hass=make_hass_stub(), config_entry=entry)


@pytest.fixture
def coord_no_entry() -> IdentityStub:
    """Return a stub without a bound ``ConfigEntry`` (early-startup state)."""

    return IdentityStub(hass=make_hass_stub(), config_entry=None)


# ---------------------------------------------------------------------------
# M1 ``_get_account_email`` (3 branches)
# ---------------------------------------------------------------------------


class TestGetAccountEmail:
    """B1: no entry → ''; B2: email is str → return; B3: missing/non-str → ''."""

    def test_no_entry_returns_empty(self, coord_no_entry: IdentityStub) -> None:
        assert coord_no_entry._get_account_email() == ""

    def test_str_email_is_returned(self, coord: IdentityStub) -> None:
        assert coord._get_account_email() == "user@example.com"

    def test_missing_email_key_returns_empty(self) -> None:
        # ``data`` is an empty dict — ``.get`` returns ``None`` (non-str branch).
        entry = make_config_entry(entry_id="entry-abc", data={})
        stub = IdentityStub(config_entry=entry)
        assert stub._get_account_email() == ""

    def test_non_str_email_returns_empty(self) -> None:
        # ``data`` carries a non-str value (e.g. int from corrupted storage).
        entry = make_config_entry(entry_id="entry-abc", data={CONF_GOOGLE_EMAIL: 42})
        stub = IdentityStub(config_entry=entry)
        assert stub._get_account_email() == ""


# ---------------------------------------------------------------------------
# M2 ``_create_auth_issue`` (3 branches)
# ---------------------------------------------------------------------------


class TestCreateAuthIssue:
    """B1: no entry → no-op; B2: success path; B3: exception swallowed."""

    def test_no_entry_is_noop(
        self, coord_no_entry: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = MagicMock()
        monkeypatch.setattr(ir, "async_create_issue", called)
        coord_no_entry._create_auth_issue()
        called.assert_not_called()

    def test_success_path_passes_email_and_translation_key(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake(
            hass: Any,
            domain: str,
            issue_id: str,
            *,
            is_fixable: bool,
            severity: Any,
            translation_key: str,
            translation_placeholders: dict[str, str],
        ) -> None:
            captured.update(
                hass=hass,
                domain=domain,
                issue_id=issue_id,
                is_fixable=is_fixable,
                severity=severity,
                translation_key=translation_key,
                translation_placeholders=translation_placeholders,
            )

        monkeypatch.setattr(ir, "async_create_issue", _fake)
        coord._create_auth_issue()

        assert captured["domain"] == DOMAIN
        assert captured["issue_id"] == issue_id_for("entry-xyz")
        assert captured["is_fixable"] is False
        assert captured["severity"] is ir.IssueSeverity.ERROR
        assert captured["translation_key"] == ISSUE_AUTH_EXPIRED_KEY
        assert captured["translation_placeholders"] == {
            "email": "user@example.com",
            "entry_title": "My GFM Account",
        }

    def test_unknown_email_falls_back_to_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry = make_config_entry(entry_id="entry-abc", data={})
        stub = IdentityStub(hass=make_hass_stub(), config_entry=entry)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            ir,
            "async_create_issue",
            lambda *a, **kw: captured.update(kw),
        )
        stub._create_auth_issue()
        assert captured["translation_placeholders"]["email"] == "unknown"

    def test_empty_title_falls_back_to_entry_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The factory builds a default title; here we override it to None so the
        # ``entry.title or entry.entry_id`` fallback branch is exercised.
        entry = make_config_entry(
            entry_id="entry-abc",
            title=None,
            data={CONF_GOOGLE_EMAIL: "x@y"},
        )
        stub = IdentityStub(hass=make_hass_stub(), config_entry=entry)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            ir,
            "async_create_issue",
            lambda *a, **kw: captured.update(kw),
        )
        stub._create_auth_issue()
        assert captured["translation_placeholders"]["entry_title"] == "entry-abc"

    def test_exception_is_swallowed(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("registry corrupted")

        monkeypatch.setattr(ir, "async_create_issue", _boom)
        # Must not propagate; debug-logged only.
        coord._create_auth_issue()


# ---------------------------------------------------------------------------
# M3 ``_dismiss_auth_issue`` (5 branches)
# ---------------------------------------------------------------------------


class TestDismissAuthIssue:
    """B1: no entry → False; B2: registry-get raises → False if delete works;
    B3: issue present → True; B4: issue absent → False; B5: delete raises → False.
    """

    def test_no_entry_returns_false(self, coord_no_entry: IdentityStub) -> None:
        assert coord_no_entry._dismiss_auth_issue() is False

    def test_issue_present_returns_true(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = SimpleNamespace(async_get_issue=MagicMock(return_value=object()))
        deleted: list[tuple[Any, str, str]] = []
        monkeypatch.setattr(ir, "async_get", lambda hass: registry)
        monkeypatch.setattr(
            ir,
            "async_delete_issue",
            lambda hass, domain, issue_id: deleted.append((hass, domain, issue_id)),
        )

        assert coord._dismiss_auth_issue() is True
        assert deleted == [(coord.hass, DOMAIN, issue_id_for("entry-xyz"))]
        registry.async_get_issue.assert_called_once_with(
            DOMAIN, issue_id_for("entry-xyz")
        )

    def test_issue_absent_returns_false(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = SimpleNamespace(async_get_issue=MagicMock(return_value=None))
        monkeypatch.setattr(ir, "async_get", lambda hass: registry)
        monkeypatch.setattr(ir, "async_delete_issue", lambda *a, **kw: None)

        assert coord._dismiss_auth_issue() is False

    def test_registry_async_get_raises_then_delete_succeeds_returns_false(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(_hass: Any) -> Any:
            raise RuntimeError("registry init failed")

        monkeypatch.setattr(ir, "async_get", _raise)
        monkeypatch.setattr(ir, "async_delete_issue", lambda *a, **kw: None)
        # ``issue_present`` defaults to ``False`` after the registry lookup
        # failed; delete succeeds, so return value is ``False``.
        assert coord._dismiss_auth_issue() is False

    def test_registry_without_get_method_returns_false(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Registry object exists but lacks ``async_get_issue`` (defensive branch).
        registry = SimpleNamespace()
        monkeypatch.setattr(ir, "async_get", lambda hass: registry)
        monkeypatch.setattr(ir, "async_delete_issue", lambda *a, **kw: None)
        assert coord._dismiss_auth_issue() is False

    def test_get_issue_raises_then_delete_succeeds_returns_false(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = SimpleNamespace(
            async_get_issue=MagicMock(side_effect=RuntimeError("boom"))
        )
        monkeypatch.setattr(ir, "async_get", lambda hass: registry)
        monkeypatch.setattr(ir, "async_delete_issue", lambda *a, **kw: None)
        assert coord._dismiss_auth_issue() is False

    def test_delete_raises_returns_false(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = SimpleNamespace(async_get_issue=MagicMock(return_value=object()))
        monkeypatch.setattr(ir, "async_get", lambda hass: registry)

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("delete failed")

        monkeypatch.setattr(ir, "async_delete_issue", _boom)
        # When delete raises, the function returns ``False`` early — it does
        # not surface the registry-present flag.
        assert coord._dismiss_auth_issue() is False


# ---------------------------------------------------------------------------
# M4 ``_schedule_eid_resolver_refresh`` (5 branches)
# ---------------------------------------------------------------------------


class TestScheduleEidResolverRefresh:
    """B1: ``hass.data`` not a dict; B2: ``DOMAIN`` bucket missing/non-dict;
    B3: resolver missing; B4: ``async_refresh`` not callable; B5: success.
    """

    def test_hass_data_not_dict_is_noop(self, coord: IdentityStub) -> None:
        coord.hass = SimpleNamespace(data=None)
        coord._schedule_eid_resolver_refresh()  # must not raise

    def test_no_hass_attribute_is_noop(self, coord: IdentityStub) -> None:
        # ``hass`` itself is ``None`` (rare but defensive).
        coord.hass = None  # type: ignore[assignment]
        coord._schedule_eid_resolver_refresh()  # must not raise

    def test_missing_domain_bucket_is_noop(self, coord: IdentityStub) -> None:
        coord.hass.data = {}
        coord._schedule_eid_resolver_refresh()  # must not raise

    def test_non_dict_domain_bucket_is_noop(self, coord: IdentityStub) -> None:
        coord.hass.data = {DOMAIN: "not-a-dict"}
        coord._schedule_eid_resolver_refresh()  # must not raise

    def test_missing_resolver_is_noop(self, coord: IdentityStub) -> None:
        coord.hass.data = {DOMAIN: {}}
        coord._schedule_eid_resolver_refresh()  # must not raise

    def test_resolver_without_async_refresh_is_noop(self, coord: IdentityStub) -> None:
        coord.hass.data = {DOMAIN: {DATA_EID_RESOLVER: SimpleNamespace()}}
        coord._schedule_eid_resolver_refresh()
        coord.hass.async_create_task.assert_not_called()

    def test_success_schedules_refresh(self, coord: IdentityStub) -> None:
        refreshed: list[bool] = []

        async def _refresh() -> None:
            refreshed.append(True)

        resolver = SimpleNamespace(async_refresh=_refresh)
        coord.hass.data = {DOMAIN: {DATA_EID_RESOLVER: resolver}}
        coord._schedule_eid_resolver_refresh()
        # ``async_create_task`` is called with the coroutine returned by
        # ``async_refresh``; we don't await it here.
        coord.hass.async_create_task.assert_called_once()
        # Close the coroutine to avoid the ``RuntimeWarning: coroutine was
        # never awaited`` since pytest-asyncio is not driving this test.
        (coro,) = coord.hass.async_create_task.call_args.args
        coro.close()

    def test_hass_without_async_create_task_is_noop(self, coord: IdentityStub) -> None:
        async def _refresh() -> None:  # pragma: no cover - never awaited
            return None

        resolver = SimpleNamespace(async_refresh=_refresh)
        # Replace ``hass`` with a stub lacking ``async_create_task``.
        coord.hass = SimpleNamespace(data={DOMAIN: {DATA_EID_RESOLVER: resolver}})
        # The implementation tolerates this defensively (``callable(None)`` is
        # ``False``); no exception should escape.
        coord._schedule_eid_resolver_refresh()


# ---------------------------------------------------------------------------
# M5 ``_register_identity_key`` (3 branches + shared-tracker log)
# ---------------------------------------------------------------------------


class TestRegisterIdentityKey:
    """B1: non-bytes is rejected; B2: wrong length is rejected;
    B3: new device added; B4: duplicate is a no-op; B5: shared tracker logs.
    """

    def test_non_bytes_key_is_rejected(self, coord: IdentityStub) -> None:
        coord._register_identity_key("dev-1", "not-bytes")  # type: ignore[arg-type]
        assert coord._identity_key_to_devices == {}

    def test_wrong_length_key_is_rejected(self, coord: IdentityStub) -> None:
        coord._register_identity_key("dev-1", b"\x00" * 31)  # too short
        coord._register_identity_key("dev-2", b"\x00" * 33)  # too long
        assert coord._identity_key_to_devices == {}

    def test_new_device_is_added(self, coord: IdentityStub) -> None:
        key = b"\xaa" * 32
        coord._register_identity_key("dev-1", key)
        assert coord._identity_key_to_devices == {key: {"dev-1"}}

    def test_duplicate_device_is_idempotent(self, coord: IdentityStub) -> None:
        key = b"\xaa" * 32
        coord._register_identity_key("dev-1", key)
        coord._register_identity_key("dev-1", key)
        assert coord._identity_key_to_devices == {key: {"dev-1"}}

    def test_shared_tracker_logs_info(
        self, coord: IdentityStub, caplog: pytest.LogCaptureFixture
    ) -> None:
        key = b"\xbb" * 32
        coord._register_identity_key("dev-1", key)
        with caplog.at_level("INFO", logger=identity_mod.__name__):
            coord._register_identity_key("dev-2", key)
        assert coord._identity_key_to_devices == {key: {"dev-1", "dev-2"}}
        assert any(
            "Shared tracker detected" in record.message for record in caplog.records
        )


# ---------------------------------------------------------------------------
# M6 ``_reset_resolver_offset`` (multiple defensive branches)
# ---------------------------------------------------------------------------


class _FakeDeviceReg:
    """Minimal ``dr.async_get(hass)`` substitute for tests."""

    def __init__(self, device: SimpleNamespace | None) -> None:
        self._device = device
        self.calls: list[set[tuple[str, str]]] = []

    def async_get_device(
        self, *, identifiers: set[tuple[str, str]] | None = None
    ) -> SimpleNamespace | None:
        self.calls.append(set(identifiers or set()))
        return self._device


class TestResetResolverOffset:
    """B1: no hass → return; B2: no entry_id → debug log + return;
    B3: device missing → return; B4: hass.data not dict → return;
    B5: bucket missing → return; B6: resolver None → return;
    B7: ``reset_device_offset`` not callable → return; B8: success.
    """

    def test_no_hass_returns_early(self, coord: IdentityStub) -> None:
        coord.hass = None  # type: ignore[assignment]
        coord._reset_resolver_offset("dev-1")  # must not raise

    def test_no_entry_id_returns_without_call(
        self, coord_no_entry: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = MagicMock()
        monkeypatch.setattr(dr, "async_get", called)
        # ``IdentityStub._entry_id`` returns ``None`` when entry is missing.
        coord_no_entry._reset_resolver_offset("dev-1")
        # ``dr.async_get(hass)`` is invoked anyway (guarded by ``entry_id``
        # only for the identifier lookup, not for the registry fetch);
        # ensure no side effects beyond that.
        assert called.called

    def test_device_not_found_logs_and_returns(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_reg = _FakeDeviceReg(device=None)
        monkeypatch.setattr(dr, "async_get", lambda hass: fake_reg)
        coord._reset_resolver_offset("dev-1")
        # The lookup was attempted with both identifier shapes.
        assert fake_reg.calls == [{(DOMAIN, "entry-xyz:dev-1"), (DOMAIN, "dev-1")}]

    def test_hass_data_not_dict_returns(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        device = SimpleNamespace(id="reg-123")
        monkeypatch.setattr(dr, "async_get", lambda hass: _FakeDeviceReg(device))
        coord.hass.data = None
        coord._reset_resolver_offset("dev-1")  # must not raise

    def test_missing_domain_bucket_returns(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        device = SimpleNamespace(id="reg-123")
        monkeypatch.setattr(dr, "async_get", lambda hass: _FakeDeviceReg(device))
        coord.hass.data = {}
        coord._reset_resolver_offset("dev-1")  # must not raise

    def test_non_dict_domain_bucket_returns(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        device = SimpleNamespace(id="reg-123")
        monkeypatch.setattr(dr, "async_get", lambda hass: _FakeDeviceReg(device))
        coord.hass.data = {DOMAIN: "not-a-dict"}
        coord._reset_resolver_offset("dev-1")  # must not raise

    def test_resolver_none_returns(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        device = SimpleNamespace(id="reg-123")
        monkeypatch.setattr(dr, "async_get", lambda hass: _FakeDeviceReg(device))
        coord.hass.data = {DOMAIN: {DATA_EID_RESOLVER: None}}
        coord._reset_resolver_offset("dev-1")  # must not raise

    def test_resolver_without_reset_is_noop(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        device = SimpleNamespace(id="reg-123")
        monkeypatch.setattr(dr, "async_get", lambda hass: _FakeDeviceReg(device))
        resolver = SimpleNamespace()  # no reset_device_offset
        coord.hass.data = {DOMAIN: {DATA_EID_RESOLVER: resolver}}
        coord._reset_resolver_offset("dev-1")  # must not raise

    def test_success_calls_reset_with_registry_id(
        self, coord: IdentityStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        device = SimpleNamespace(id="reg-123")
        monkeypatch.setattr(dr, "async_get", lambda hass: _FakeDeviceReg(device))
        reset_calls: list[str] = []
        resolver = SimpleNamespace(
            reset_device_offset=lambda registry_id: reset_calls.append(registry_id)
        )
        coord.hass.data = {DOMAIN: {DATA_EID_RESOLVER: resolver}}
        coord._reset_resolver_offset("dev-1")
        assert reset_calls == ["reg-123"]
