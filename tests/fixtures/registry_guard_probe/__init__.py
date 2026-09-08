# tests/fixtures/registry_guard_probe/__init__.py
"""Deliberate violations, one per rule of the device registry ratchet.

These files are never imported and never scanned as production code.  They exist
so ``tests/test_guard_device_registry_kwargs_probe.py`` can prove that each rule
can actually fail: a guard whose red path was only ever asserted in a commit
message is a guard nobody has tested.
"""
