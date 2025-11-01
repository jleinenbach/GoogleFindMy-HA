from __future__ import annotations

from typing import SupportsBytes

_password_type = bytes | bytearray | memoryview | SupportsBytes


def hash(
    password: _password_type,
    *,
    salt: bytes,
    N: int,
    r: int,
    p: int,
    dkLen: int,
) -> bytes: ...
