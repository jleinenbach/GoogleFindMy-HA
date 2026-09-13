# tests/fixtures/kill_probe/__init__.py
"""Probe chain for the ancestry filter of the Chrome process cleanup.

``tests/test_chrome_driver.py`` runs these as real processes, three links deep,
so that the cleanup under test has a grandparent it must spare and a stranger
it must still terminate. They were string literals inside the test, which
a review of PR #1274 flagged: no linter ever read them, and a typo surfaced
only as a broken child at run time. As files they are read by ``ruff`` like
every other test module. Never imported, never collected: pytest only
collects ``test_*.py`` and ``*_test.py``.
"""
