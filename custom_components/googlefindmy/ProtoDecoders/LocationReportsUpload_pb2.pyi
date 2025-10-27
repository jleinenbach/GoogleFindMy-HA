# custom_components/googlefindmy/ProtoDecoders/LocationReportsUpload_pb2.pyi
from __future__ import annotations

from custom_components.googlefindmy.ProtoDecoders import Common_pb2 as _Common_pb2
from custom_components.googlefindmy.protobuf_typing import MessageProto as _MessageProto
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

Message = _MessageProto

DESCRIPTOR: _descriptor.FileDescriptor

class LocationReportsUpload(Message):
    __slots__ = ("reports", "clientMetadata", "random1", "random2")
    REPORTS_FIELD_NUMBER: _ClassVar[int]
    CLIENTMETADATA_FIELD_NUMBER: _ClassVar[int]
    RANDOM1_FIELD_NUMBER: _ClassVar[int]
    RANDOM2_FIELD_NUMBER: _ClassVar[int]
    reports: _containers.RepeatedCompositeFieldContainer[Report]
    clientMetadata: ClientMetadata
    random1: int
    random2: int
    def __init__(self, reports: _Optional[_Iterable[_Union[Report, _Mapping[str, object]]]] = ..., clientMetadata: _Optional[_Union[ClientMetadata, _Mapping[str, object]]] = ..., random1: _Optional[int] = ..., random2: _Optional[int] = ...) -> None: ...

class Report(Message):
    __slots__ = ("advertisement", "time", "location")
    ADVERTISEMENT_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    LOCATION_FIELD_NUMBER: _ClassVar[int]
    advertisement: Advertisement
    time: _Common_pb2.Time
    location: _Common_pb2.LocationReport
    def __init__(self, advertisement: _Optional[_Union[Advertisement, _Mapping[str, object]]] = ..., time: _Optional[_Union[_Common_pb2.Time, _Mapping[str, object]]] = ..., location: _Optional[_Union[_Common_pb2.LocationReport, _Mapping[str, object]]] = ...) -> None: ...

class Advertisement(Message):
    __slots__ = ("identifier", "unwantedTrackingModeEnabled")
    IDENTIFIER_FIELD_NUMBER: _ClassVar[int]
    UNWANTEDTRACKINGMODEENABLED_FIELD_NUMBER: _ClassVar[int]
    identifier: Identifier
    unwantedTrackingModeEnabled: int
    def __init__(self, identifier: _Optional[_Union[Identifier, _Mapping[str, object]]] = ..., unwantedTrackingModeEnabled: _Optional[int] = ...) -> None: ...

class Identifier(Message):
    __slots__ = ("truncatedEid", "canonicDeviceId")
    TRUNCATEDEID_FIELD_NUMBER: _ClassVar[int]
    CANONICDEVICEID_FIELD_NUMBER: _ClassVar[int]
    truncatedEid: bytes
    canonicDeviceId: bytes
    def __init__(self, truncatedEid: _Optional[bytes] = ..., canonicDeviceId: _Optional[bytes] = ...) -> None: ...

class ClientMetadata(Message):
    __slots__ = ("version",)
    VERSION_FIELD_NUMBER: _ClassVar[int]
    version: ClientVersionInformation
    def __init__(self, version: _Optional[_Union[ClientVersionInformation, _Mapping[str, object]]] = ...) -> None: ...

class ClientVersionInformation(Message):
    __slots__ = ("playServicesVersion",)
    PLAYSERVICESVERSION_FIELD_NUMBER: _ClassVar[int]
    playServicesVersion: str
    def __init__(self, playServicesVersion: _Optional[str] = ...) -> None: ...
