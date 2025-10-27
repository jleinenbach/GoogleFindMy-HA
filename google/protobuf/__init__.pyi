# google/protobuf/__init__.pyi
from __future__ import annotations

from types import ModuleType

from . import descriptor as descriptor
from .internal import containers as containers
from .message import DecodeError as DecodeError, Message as Message

text_format: ModuleType
