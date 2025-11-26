# tests.helpers quickstart

`tests.helpers` exposes small utilities for interactive debugging outside the pytest fixture stack. Highlights:

* `tests.helpers.stub_coordinator_debug`: builds stub coordinators without pytest fixtures and registers teardown callbacks so ad-hoc runs mirror test cleanup.
* `tests.helpers.config_entries_stub`: installs the config-entry stub surface (including subentry helpers) into a target module for quick contract validation.
* `tests.helpers.ast_extract`: compiles single class methods from integration modules for isolated evaluation when import hooks would otherwise pull in Home Assistant.

Import helpers via `from tests.helpers import <module_or_function>` to keep imports package-relative and compatible with mypy strict checking.
