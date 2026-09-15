# tests.helpers quickstart

`tests.helpers` exposes small utilities for interactive debugging outside the pytest fixture stack. Highlights:

* `tests.helpers.stub_coordinator_debug`: builds stub coordinators without pytest fixtures and registers teardown callbacks so ad-hoc runs mirror test cleanup.
* `tests.helpers.config_entries_stub`: installs the config-entry stub surface (including subentry helpers) into a target module for quick contract validation.
* `tests.helpers.ast_extract`: compiles single class methods from integration modules for isolated evaluation when import hooks would otherwise pull in Home Assistant.
* `tests.helpers.core_shutdown_state`: seeds a `__new__`-built coordinator with the four attributes the core's `DataUpdateCoordinator.__init__` sets and its `async_shutdown` reads. Required before calling `async_shutdown` on such a double, because `GoogleFindMyCoordinator.async_shutdown` chains to the core; the `conftest` stub mirrors the core's shutdown and `tests/test_core_shutdown_stub_parity.py` pins the mirror.

Import helpers via `from tests.helpers import <module_or_function>` to keep imports package-relative and compatible with mypy strict checking.
