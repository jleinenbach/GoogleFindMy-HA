# tests/fixtures/map_tiles_probe/__init__.py
"""Probe for the runtime contract with Home Assistant's ``map_tiles`` component.

``tests/test_map_view_tiles.py`` runs ``probe.py`` as a real process. It has to
be a process: ``tests/conftest.py`` replaces ``homeassistant.const`` with a stub
that has no ``__version__``, and ``map_tiles/const.py`` imports exactly that, so
an ``importorskip`` inside the test process is skipped on every Core, including
one that ships the component. A clean child interpreter sees the real package.
As a file the probe is read by ``ruff`` like every other test module (a string
literal inside a test is read by no linter). Never imported, never collected:
pytest only collects ``test_*.py`` and ``*_test.py``.
"""
