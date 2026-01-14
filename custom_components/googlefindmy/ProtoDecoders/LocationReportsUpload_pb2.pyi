# custom_components/googlefindmy/ProtoDecoders/LocationReportsUpload_pb2.pyi
from __future__ import annotations

from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from typing import (
    Any as _Any,
)
from typing import (
    ClassVar as _ClassVar,
)

from custom_components.googlefindmy.protobuf_typing import MessageProto as _MessageProto
from custom_components.googlefindmy.ProtoDecoders import Common_pb2 as _Common_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf.internal import containers as _containers

Message = _message.Message
MessageProto = _MessageProto

DESCRIPTOR: _descriptor.FileDescriptor

class LocationReportsUpload(Message, _MessageProto):
    __slots__ = ("reports", "clientMetadata", "random1", "random2")
    REPORTS_FIELD_NUMBER: _ClassVar[int]
    CLIENTMETADATA_FIELD_NUMBER: _ClassVar[int]
    RANDOM1_FIELD_NUMBER: _ClassVar[int]
    RANDOM2_FIELD_NUMBER: _ClassVar[int]
    reports: _containers.RepeatedCompositeFieldContainer[Report]
    clientMetadata: ClientMetadata
    random1: int
    random2: int
    def __init__(
        self,
        reports: _Iterable[Report | _Mapping[str, _Any]] | None = ...,
        clientMetadata: ClientMetadata | _Mapping[str, _Any] | None = ...,
        random1: int | None = ...,
        random2: int | None = ...,
    ) -> None: ...

class Report(Message, _MessageProto):
    __slots__ = ("advertisement", "time", "location")
    ADVERTISEMENT_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    LOCATION_FIELD_NUMBER: _ClassVar[int]
    advertisement: Advertisement
    time: _Common_pb2.Time
    location: _Common_pb2.LocationReport
    def __init__(
        self,
        advertisement: Advertisement | _Mapping[str, _Any] | None = ...,
        time: _Common_pb2.Time | _Mapping[str, _Any] | None = ...,
        location: _Common_pb2.LocationReport | _Mapping[str, _Any] | None = ...,
    ) -> None: ...

class Advertisement(Message, _MessageProto):
    __slots__ = ("identifier", "unwantedTrackingModeEnabled")
    IDENTIFIER_FIELD_NUMBER: _ClassVar[int]
    UNWANTEDTRACKINGMODEENABLED_FIELD_NUMBER: _ClassVar[int]
    identifier: Identifier
    unwantedTrackingModeEnabled: int
    def __init__(
        self,
        identifier: Identifier | _Mapping[str, _Any] | None = ...,
        unwantedTrackingModeEnabled: int | None = ...,
    ) -> None: ...

class Identifier(Message, _MessageProto):
    __slots__ = ("truncatedEid", "canonicDeviceId")
    TRUNCATEDEID_FIELD_NUMBER: _ClassVar[int]
    CANONICDEVICEID_FIELD_NUMBER: _ClassVar[int]
    truncatedEid: bytes
    canonicDeviceId: bytes
    def __init__(
        self,
        truncatedEid: bytes | None = ...,
        canonicDeviceId: bytes | None = ...,
    ) -> None: ...

class ClientMetadata(Message, _MessageProto):
    __slots__ = ("version",)
    VERSION_FIELD_NUMBER: _ClassVar[int]
    version: ClientVersionInformation
    def __init__(
        self,
        version: ClientVersionInformation | _Mapping[str, _Any] | None = ...,
    ) -> None: ...

class ClientVersionInformation(Message, _MessageProto):
    __slots__ = ("playServicesVersion",)
    PLAYSERVICESVERSION_FIELD_NUMBER: _ClassVar[int]
    playServicesVersion: str
    def __init__(self, playServicesVersion: str | None = ...) -> None: ...
