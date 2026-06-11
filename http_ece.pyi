from __future__ import annotations

from typing import Any

__all__ = ["ECEException", "decrypt", "encrypt"]

class ECEException(Exception): ...

def decrypt(*args: Any, **kwargs: Any) -> bytes: ...
def encrypt(*args: Any, **kwargs: Any) -> bytes: ...
