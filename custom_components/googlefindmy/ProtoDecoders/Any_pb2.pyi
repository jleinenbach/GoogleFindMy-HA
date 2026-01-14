# custom_components/googlefindmy/ProtoDecoders/Any_pb2.pyi
from __future__ import annotations

from typing import ClassVar as _ClassVar

from custom_components.googlefindmy.protobuf_typing import (
    MessageProto as _MessageProto,
)
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message

Message = _message.Message
MessageProto = _MessageProto

DESCRIPTOR: _descriptor.FileDescriptor

class Any(Message, _MessageProto):
    __slots__ = ("type_url", "value")
    TYPE_URL_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    type_url: str
    value: bytes
    def __init__(
        self, type_url: str | None = ..., value: bytes | None = ...
    ) -> None: ...
