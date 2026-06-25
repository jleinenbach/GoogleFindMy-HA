# tests/test_location_request_lazy_imports.py
"""Guard the module-scope lazy-import contract of ``location_request``.

``location_request`` provides dedicated lazy getters
(``_import_decrypt_locations_module`` / ``_import_eid_info_module``) for its
heavy protobuf/crypto dependencies, and resolves the exception types it needs
(``OwnerKeyLookupTransientError`` from ``decrypt_locations`` and
``SpotApiEmptyResponseError`` from ``get_eid_info_request``) via ``getattr`` only
inside the executor-backed runtime paths (the FCM callback and the locate path).

Binding those exception types with a module-scope ``from ... import`` would
bypass the file's own lazy getters and violate the AGENTS.md "Import deferral
reminder". These tests lock that file-local contract so the deferral cannot
silently regress.

Scope note: this is a *file-local* guard. It does NOT assert that the heavy
modules stay out of ``sys.modules`` package-wide, because several other
module-scope importers on the Home Assistant setup path (``api.py``,
``coordinator/locate.py``, ``coordinator/polling.py``, ``Auth/fcm_receiver_ha.py``)
load ``decrypt_locations`` eagerly today. Making the whole startup path lazy is a
separate, broader change tracked outside this fix.
"""

from __future__ import annotations

import importlib


def _location_request_module() -> object:
    return importlib.import_module(
        "custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request"
    )


def test_owner_key_transient_error_not_bound_at_module_scope() -> None:
    """Codex finding: ``OwnerKeyLookupTransientError`` must stay behind the getter.

    A module-scope ``from decrypt_locations import OwnerKeyLookupTransientError``
    would expose the name as a module attribute. The lazy getter pattern keeps it
    a function-local instead, so the attribute must be absent.
    """
    module = _location_request_module()
    assert not hasattr(module, "OwnerKeyLookupTransientError"), (
        "OwnerKeyLookupTransientError is bound at module scope; resolve it via "
        "getattr(_import_decrypt_locations_module(), ...) inside the runtime paths"
    )


def test_spot_empty_response_error_not_bound_at_module_scope() -> None:
    """Variant of the same class: ``SpotApiEmptyResponseError`` getter must hold."""
    module = _location_request_module()
    assert not hasattr(module, "SpotApiEmptyResponseError"), (
        "SpotApiEmptyResponseError is bound at module scope; resolve it via "
        "getattr(_import_eid_info_module(), ...) inside the runtime paths"
    )
