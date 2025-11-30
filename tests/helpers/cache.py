"""Cache test utilities for TokenCache interactions."""

from __future__ import annotations


class DummyCache:
    """Minimal async cache stub with in-memory storage."""

    def __init__(self) -> None:
        self.values: dict[str, object | None] = {}

    async def get(self, name: str) -> object | None:
        return self.values.get(name)

    async def set(self, name: str, value: object | None) -> None:
        self.values[name] = value
