# tests/fixtures/stop_probe/__init__.py
"""Probe for ending a stopped login while the browser thread is still busy.

``tests/test_main_cli_single_publish.py`` runs ``browser_worker.py`` as a real
process: only a real interpreter shows whether ``asyncio.run`` and the
interpreter shutdown wait for the executor thread that runs the browser flow of
sign-in 2. It was a string literal inside the test, which a review of PR #1482
flagged: no linter ever read it. As a file it is read by ``ruff`` like every
other test module. Never imported, never collected: pytest only collects
``test_*.py`` and ``*_test.py``.
"""
