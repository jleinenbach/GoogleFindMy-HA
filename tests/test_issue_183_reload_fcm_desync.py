# tests/test_issue_183_reload_fcm_desync.py
"""Regression tests for issue #183 (reload left FCM stuck "Disconnected").

The user reported that changing settings (which reloads the config entry)
flipped the FCM connection status to "Disconnected" and it never recovered
until Home Assistant was fully restarted. The submitted diagnostics were
self-contradictory: the live receiver snapshot was ``healthy: true`` (STARTED,
client_ready) while ``coordinator.fcm_status`` was ``degraded`` with reason
``"Push transport not ready; continuing with cached data"``.

The empirical reproduction that preceded the fix discriminated three candidate
root causes and confirmed **H2** (false-negative readiness via store desync):
the refcount->0 release path (``_pop_fcm_receiver``) emptied the per-entry
provider dict ``fcm_receivers``, but ``_sync_legacy_fcm_receiver_alias`` only
dropped the legacy ``fcm_receiver`` alias when it held the *wrong type*, so a
healthy receiver of the right type survived there. ``is_push_ready()`` resolves
the (now empty) per-entry store and returned ``False`` (rendered as
"Disconnected"), while diagnostics read the surviving singleton and still
reported the receiver as healthy/connected -- exactly the contradiction in the
issue. H1 (frozen cache) and H3 (receiver genuinely dead) were refuted.

The fix makes ``fcm_receivers`` the single source of truth: when the per-entry
dict holds no valid receiver, the legacy alias is dropped as well. These tests
now assert that fixed behaviour (the alias is cleared, and readiness and
diagnostics stay consistent), and that a runtime re-sync heals readiness
without a restart. The end-to-end reload re-acquire repopulation is covered by
``test_fcm_receiver.test_reload_reacquire_repopulates_receiver_store``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.googlefindmy as init_module
from custom_components.googlefindmy.api import GoogleFindMyAPI
from custom_components.googlefindmy.diagnostics import _fcm_receiver_state
from tests.helpers.api_stub import StubCache

ENTRY_ID = "entry-183"


class _StubReceiver:
    """Healthy FCM receiver double used to exercise the reload store handling.

    Exposes only the surface consumed by the production code under test:
    ``is_ready`` (read by the provider getter and ``is_push_ready``),
    ``get_health_snapshots`` (read by the diagnostics summary), and
    ``set_default_entry_id`` (called by ``_sync_receiver_default_entry``).
    """

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self.is_ready = True
        self.start_count = 5
        self.default_entry_id: str | None = None

    def set_default_entry_id(self, entry_id: str) -> None:
        """Record the default entry id propagated by the provider."""

        self.default_entry_id = entry_id

    def get_health_snapshots(self) -> dict[str, dict[str, Any]]:
        """Return a single healthy entry snapshot, mirroring the issue data."""

        return {
            self._entry_id: {
                "healthy": True,
                "run_state": "STARTED",
                "seconds_since_last_activity": 1.0,
                "activity_stale": False,
                "supervisor_running": True,
                "client_ready": True,
                "do_listen": True,
                "last_activity_monotonic": 123.0,
            }
        }


@pytest.fixture
def receiver_cls(monkeypatch: pytest.MonkeyPatch) -> type[_StubReceiver]:
    """Make the store treat ``_StubReceiver`` as the canonical receiver type."""

    monkeypatch.setattr(init_module, "_FCM_RECEIVER_CLASS", _StubReceiver)
    return _StubReceiver


def _make_hass() -> SimpleNamespace:
    """Return a minimal hass stub exposing only the ``data`` mapping."""

    return SimpleNamespace(data={})


def test_release_path_clears_legacy_singleton_when_dict_empties(
    receiver_cls: type[_StubReceiver],
) -> None:
    """Issue #183 root fix: the release path no longer strands the singleton.

    After ``_set_fcm_receiver`` both the per-entry dict and the legacy alias see
    the receiver. After ``_pop_fcm_receiver`` (the operative call inside the
    refcount->0 release path) the per-entry dict is empty *and* the legacy
    singleton is cleared, because ``fcm_receivers`` is the single source of
    truth. Before the fix the healthy singleton survived here, which is what the
    contradictory diagnostics exposed.
    """

    hass = _make_hass()
    bucket = init_module._domain_data(hass)
    receiver = receiver_cls(ENTRY_ID)

    init_module._set_fcm_receiver(bucket, ENTRY_ID, receiver)
    assert init_module._get_fcm_receivers(bucket) == {ENTRY_ID: receiver}
    assert bucket["fcm_receiver"] is receiver

    popped = init_module._pop_fcm_receiver(bucket, ENTRY_ID)
    assert popped is receiver

    # The provider dict is now empty...
    assert init_module._get_fcm_receivers(bucket) == {}
    # ...and the legacy singleton must NOT survive an empty per-entry dict.
    assert bucket.get("fcm_receiver") is None


def test_release_keeps_readiness_and_diagnostics_consistent(
    receiver_cls: type[_StubReceiver],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #183: readiness and diagnostics agree after a release.

    With the per-entry dict emptied (reload release) the production
    ``is_push_ready()`` resolves nothing and returns ``False``. Because the
    legacy singleton is cleared as well, the production diagnostics summary no
    longer reports a stale healthy receiver -- both views consistently report
    "not connected" instead of the contradictory state from the issue. The
    receiver object itself is untouched (it is the *store* that was desynced).
    """

    hass = _make_hass()
    bucket = init_module._domain_data(hass)
    receiver = receiver_cls(ENTRY_ID)

    # Register the production provider getter exactly as setup does.
    def _provider(entry_id: str | None = None) -> Any:
        return init_module._domain_fcm_provider(hass, entry_id)

    monkeypatch.setattr(
        "custom_components.googlefindmy.api._FCM_ReceiverGetter", _provider
    )

    api = GoogleFindMyAPI(cache=StubCache(entry_id=ENTRY_ID))

    # Baseline: a properly registered receiver makes readiness True and the
    # diagnostics report it as connected.
    init_module._set_fcm_receiver(bucket, ENTRY_ID, receiver)
    assert api.is_push_ready() is True
    state = _fcm_receiver_state(hass)
    assert state is not None
    assert state["connected_entries"] == [ENTRY_ID]

    # Reload release path: per-entry dict empties, singleton is cleared too.
    init_module._pop_fcm_receiver(bucket, ENTRY_ID)

    # Readiness is False AND diagnostics report nothing -> consistent, not the
    # contradictory "healthy receiver vs. degraded status" from the issue.
    assert api.is_push_ready() is False
    assert _fcm_receiver_state(hass) is None
    # The receiver instance never stopped being ready; only the store changed.
    assert receiver.is_ready is True


def test_store_resync_restores_push_ready_without_restart(
    receiver_cls: type[_StubReceiver],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H1 refuted: a runtime store re-sync heals readiness, no restart needed.

    Re-registering the receiver in the per-entry dict (what a correct reload
    re-acquire does) flips ``is_push_ready()`` back to ``True`` in the same
    process. The recovery signal is therefore honoured immediately once the
    readiness input is correct; the stuck state was the broken store, not a
    frozen cache.
    """

    hass = _make_hass()
    bucket = init_module._domain_data(hass)
    receiver = receiver_cls(ENTRY_ID)

    def _provider(entry_id: str | None = None) -> Any:
        return init_module._domain_fcm_provider(hass, entry_id)

    monkeypatch.setattr(
        "custom_components.googlefindmy.api._FCM_ReceiverGetter", _provider
    )

    api = GoogleFindMyAPI(cache=StubCache(entry_id=ENTRY_ID))

    init_module._set_fcm_receiver(bucket, ENTRY_ID, receiver)
    init_module._pop_fcm_receiver(bucket, ENTRY_ID)
    assert api.is_push_ready() is False

    # Runtime re-sync (no HA restart): readiness recovers immediately.
    init_module._set_fcm_receiver(bucket, ENTRY_ID, receiver)
    assert api.is_push_ready() is True


def test_absent_per_entry_dict_preserves_legacy_migration(
    receiver_cls: type[_StubReceiver],
) -> None:
    """The legacy-migration path is untouched when ``fcm_receivers`` is absent.

    When the per-entry dict has never been created (legacy upgrade), the alias
    sync must only drop a *wrong-type* legacy receiver and preserve a valid one
    so ``_get_fcm_receivers`` can migrate it into the per-entry store. This
    guards the single-source-of-truth change from over-reaching into the
    migration path.
    """

    hass = _make_hass()
    bucket = init_module._domain_data(hass)

    # Absent per-entry dict + wrong-type alias -> alias is dropped.
    bucket.pop("fcm_receivers", None)
    bucket["fcm_receiver"] = object()
    init_module._sync_legacy_fcm_receiver_alias(bucket)
    assert bucket.get("fcm_receiver") is None

    # Absent per-entry dict + valid alias -> preserved for migration.
    bucket.pop("fcm_receivers", None)
    receiver = receiver_cls(ENTRY_ID)
    bucket["fcm_receiver"] = receiver
    init_module._sync_legacy_fcm_receiver_alias(bucket)
    assert bucket.get("fcm_receiver") is receiver
