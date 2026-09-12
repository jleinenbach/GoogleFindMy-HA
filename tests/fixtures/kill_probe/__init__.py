# tests/fixtures/kill_probe/__init__.py
"""Probe chain for the ancestry filter of the Chrome process cleanup.

``tests/test_chrome_driver.py`` runs these as real processes, three links deep,
so that the cleanup under test has a grandparent it must spare and a stranger
it must still terminate. They were string literals inside the test until PR
#1274 (``N-24``); as files they are read by ``ruff`` like every other test
module, and a typo no longer surfaces as a broken child at run time. Never
imported, never collected: pytest only collects ``test_*.py``.
"""
