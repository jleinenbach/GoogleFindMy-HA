# google/protobuf/any_pb2.pyi
from __future__ import annotations

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message

DESCRIPTOR: _descriptor.FileDescriptor

class Any(_message.Message):
    type_url: str
    value: bytes
    def __init__(
        self, *, type_url: str | None = ..., value: bytes | None = ...
    ) -> None: ...
