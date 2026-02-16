# custom_components/googlefindmy/ProtoDecoders/RpcStatus_pb2.pyi
from __future__ import annotations

from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from typing import Any as _Any
from typing import (
    ClassVar as _ClassVar,
)

from custom_components.googlefindmy.protobuf_typing import (
    MessageProto as _MessageProto,
)
from google.protobuf import any_pb2 as _any_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf.internal.containers import RepeatedCompositeFieldContainer

Message = _message.Message
MessageProto = _MessageProto

DESCRIPTOR: _descriptor.FileDescriptor

class Status(Message, _MessageProto):
    __slots__ = ("code", "message", "details")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    DETAILS_FIELD_NUMBER: _ClassVar[int]
    code: int
    message: str
    details: RepeatedCompositeFieldContainer[_any_pb2.Any]
    def __init__(
        self,
        code: int | None = ...,
        message: str | None = ...,
        details: _Iterable[_any_pb2.Any | _Mapping[str, _Any]] | None = ...,
    ) -> None: ...
