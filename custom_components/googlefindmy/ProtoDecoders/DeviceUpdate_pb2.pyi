# custom_components/googlefindmy/ProtoDecoders/DeviceUpdate_pb2.pyi
from __future__ import annotations

from custom_components.googlefindmy.ProtoDecoders import Common_pb2 as _Common_pb2
from custom_components.googlefindmy.protobuf_typing import (
    EnumTypeWrapperMeta as _EnumTypeWrapperMeta,
    MessageProto as _MessageProto,
)
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

EnumTypeWrapper = _EnumTypeWrapperMeta[int]
Message = _MessageProto

DESCRIPTOR: _descriptor.FileDescriptor

class DeviceType(int, metaclass=EnumTypeWrapper):
    __slots__ = ()
    UNKNOWN_DEVICE_TYPE: _ClassVar[DeviceType]
    ANDROID_DEVICE: _ClassVar[DeviceType]
    SPOT_DEVICE: _ClassVar[DeviceType]
    TEST_DEVICE_TYPE: _ClassVar[DeviceType]
    AUTO_DEVICE: _ClassVar[DeviceType]
    FASTPAIR_DEVICE: _ClassVar[DeviceType]
    SUPERVISED_ANDROID_DEVICE: _ClassVar[DeviceType]

class SpotContributorType(int, metaclass=EnumTypeWrapper):
    __slots__ = ()
    FMDN_DISABLED_DEFAULT: _ClassVar[SpotContributorType]
    FMDN_CONTRIBUTOR_HIGH_TRAFFIC: _ClassVar[SpotContributorType]
    FMDN_CONTRIBUTOR_ALL_LOCATIONS: _ClassVar[SpotContributorType]
    FMDN_HIGH_TRAFFIC: _ClassVar[SpotContributorType]
    FMDN_ALL_LOCATIONS: _ClassVar[SpotContributorType]

class DeviceComponent(int, metaclass=EnumTypeWrapper):
    __slots__ = ()
    DEVICE_COMPONENT_UNSPECIFIED: _ClassVar[DeviceComponent]
    DEVICE_COMPONENT_RIGHT: _ClassVar[DeviceComponent]
    DEVICE_COMPONENT_LEFT: _ClassVar[DeviceComponent]
    DEVICE_COMPONENT_CASE: _ClassVar[DeviceComponent]

class IdentifierInformationType(int, metaclass=EnumTypeWrapper):
    __slots__ = ()
    IDENTIFIER_UNKNOWN: _ClassVar[IdentifierInformationType]
    IDENTIFIER_ANDROID: _ClassVar[IdentifierInformationType]
    IDENTIFIER_SPOT: _ClassVar[IdentifierInformationType]

class SpotDeviceType(int, metaclass=EnumTypeWrapper):
    __slots__ = ()
    DEVICE_TYPE_UNKNOWN: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_BEACON: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_HEADPHONES: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_KEYS: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_WATCH: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_WALLET: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_BAG: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_LAPTOP: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_CAR: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_REMOTE_CONTROL: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_BADGE: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_BIKE: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_CAMERA: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_CAT: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_CHARGER: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_CLOTHING: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_DOG: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_NOTEBOOK: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_PASSPORT: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_PHONE: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_SPEAKER: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_TABLET: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_TOY: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_UMBRELLA: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_STYLUS: _ClassVar[SpotDeviceType]
    DEVICE_TYPE_EARBUDS: _ClassVar[SpotDeviceType]
UNKNOWN_DEVICE_TYPE: DeviceType
ANDROID_DEVICE: DeviceType
SPOT_DEVICE: DeviceType
TEST_DEVICE_TYPE: DeviceType
AUTO_DEVICE: DeviceType
FASTPAIR_DEVICE: DeviceType
SUPERVISED_ANDROID_DEVICE: DeviceType
FMDN_DISABLED_DEFAULT: SpotContributorType
FMDN_CONTRIBUTOR_HIGH_TRAFFIC: SpotContributorType
FMDN_CONTRIBUTOR_ALL_LOCATIONS: SpotContributorType
FMDN_HIGH_TRAFFIC: SpotContributorType
FMDN_ALL_LOCATIONS: SpotContributorType
DEVICE_COMPONENT_UNSPECIFIED: DeviceComponent
DEVICE_COMPONENT_RIGHT: DeviceComponent
DEVICE_COMPONENT_LEFT: DeviceComponent
DEVICE_COMPONENT_CASE: DeviceComponent
IDENTIFIER_UNKNOWN: IdentifierInformationType
IDENTIFIER_ANDROID: IdentifierInformationType
IDENTIFIER_SPOT: IdentifierInformationType
DEVICE_TYPE_UNKNOWN: SpotDeviceType
DEVICE_TYPE_BEACON: SpotDeviceType
DEVICE_TYPE_HEADPHONES: SpotDeviceType
DEVICE_TYPE_KEYS: SpotDeviceType
DEVICE_TYPE_WATCH: SpotDeviceType
DEVICE_TYPE_WALLET: SpotDeviceType
DEVICE_TYPE_BAG: SpotDeviceType
DEVICE_TYPE_LAPTOP: SpotDeviceType
DEVICE_TYPE_CAR: SpotDeviceType
DEVICE_TYPE_REMOTE_CONTROL: SpotDeviceType
DEVICE_TYPE_BADGE: SpotDeviceType
DEVICE_TYPE_BIKE: SpotDeviceType
DEVICE_TYPE_CAMERA: SpotDeviceType
DEVICE_TYPE_CAT: SpotDeviceType
DEVICE_TYPE_CHARGER: SpotDeviceType
DEVICE_TYPE_CLOTHING: SpotDeviceType
DEVICE_TYPE_DOG: SpotDeviceType
DEVICE_TYPE_NOTEBOOK: SpotDeviceType
DEVICE_TYPE_PASSPORT: SpotDeviceType
DEVICE_TYPE_PHONE: SpotDeviceType
DEVICE_TYPE_SPEAKER: SpotDeviceType
DEVICE_TYPE_TABLET: SpotDeviceType
DEVICE_TYPE_TOY: SpotDeviceType
DEVICE_TYPE_UMBRELLA: SpotDeviceType
DEVICE_TYPE_STYLUS: SpotDeviceType
DEVICE_TYPE_EARBUDS: SpotDeviceType

class GetEidInfoForE2eeDevicesResponse(Message):
    __slots__ = ("encryptedOwnerKeyAndMetadata",)
    ENCRYPTEDOWNERKEYANDMETADATA_FIELD_NUMBER: _ClassVar[int]
    encryptedOwnerKeyAndMetadata: EncryptedOwnerKeyAndMetadata
    def __init__(self, encryptedOwnerKeyAndMetadata: _Optional[_Union[EncryptedOwnerKeyAndMetadata, _Mapping[str, object]]] = ...) -> None: ...

class EncryptedOwnerKeyAndMetadata(Message):
    __slots__ = ("encryptedOwnerKey", "ownerKeyVersion", "securityDomain")
    ENCRYPTEDOWNERKEY_FIELD_NUMBER: _ClassVar[int]
    OWNERKEYVERSION_FIELD_NUMBER: _ClassVar[int]
    SECURITYDOMAIN_FIELD_NUMBER: _ClassVar[int]
    encryptedOwnerKey: bytes
    ownerKeyVersion: int
    securityDomain: str
    def __init__(self, encryptedOwnerKey: _Optional[bytes] = ..., ownerKeyVersion: _Optional[int] = ..., securityDomain: _Optional[str] = ...) -> None: ...

class DevicesList(Message):
    __slots__ = ("deviceMetadata",)
    DEVICEMETADATA_FIELD_NUMBER: _ClassVar[int]
    deviceMetadata: _containers.RepeatedCompositeFieldContainer[DeviceMetadata]
    def __init__(self, deviceMetadata: _Optional[_Iterable[_Union[DeviceMetadata, _Mapping[str, object]]]] = ...) -> None: ...

class DevicesListRequest(Message):
    __slots__ = ("deviceListRequestPayload",)
    DEVICELISTREQUESTPAYLOAD_FIELD_NUMBER: _ClassVar[int]
    deviceListRequestPayload: DevicesListRequestPayload
    def __init__(self, deviceListRequestPayload: _Optional[_Union[DevicesListRequestPayload, _Mapping[str, object]]] = ...) -> None: ...

class DevicesListRequestPayload(Message):
    __slots__ = ("type", "id")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    type: DeviceType
    id: str
    def __init__(self, type: _Optional[_Union[DeviceType, str]] = ..., id: _Optional[str] = ...) -> None: ...

class ExecuteActionRequest(Message):
    __slots__ = ("scope", "action", "requestMetadata")
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    REQUESTMETADATA_FIELD_NUMBER: _ClassVar[int]
    scope: ExecuteActionScope
    action: ExecuteActionType
    requestMetadata: ExecuteActionRequestMetadata
    def __init__(self, scope: _Optional[_Union[ExecuteActionScope, _Mapping[str, object]]] = ..., action: _Optional[_Union[ExecuteActionType, _Mapping[str, object]]] = ..., requestMetadata: _Optional[_Union[ExecuteActionRequestMetadata, _Mapping[str, object]]] = ...) -> None: ...

class ExecuteActionRequestMetadata(Message):
    __slots__ = ("type", "requestUuid", "fmdClientUuid", "gcmRegistrationId", "unknown")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    REQUESTUUID_FIELD_NUMBER: _ClassVar[int]
    FMDCLIENTUUID_FIELD_NUMBER: _ClassVar[int]
    GCMREGISTRATIONID_FIELD_NUMBER: _ClassVar[int]
    UNKNOWN_FIELD_NUMBER: _ClassVar[int]
    type: DeviceType
    requestUuid: str
    fmdClientUuid: str
    gcmRegistrationId: GcmCloudMessagingIdProtobuf
    unknown: bool
    def __init__(self, type: _Optional[_Union[DeviceType, str]] = ..., requestUuid: _Optional[str] = ..., fmdClientUuid: _Optional[str] = ..., gcmRegistrationId: _Optional[_Union[GcmCloudMessagingIdProtobuf, _Mapping[str, object]]] = ..., unknown: bool = ...) -> None: ...

class GcmCloudMessagingIdProtobuf(Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class ExecuteActionType(Message):
    __slots__ = ("locateTracker", "startSound", "stopSound")
    LOCATETRACKER_FIELD_NUMBER: _ClassVar[int]
    STARTSOUND_FIELD_NUMBER: _ClassVar[int]
    STOPSOUND_FIELD_NUMBER: _ClassVar[int]
    locateTracker: ExecuteActionLocateTrackerType
    startSound: ExecuteActionSoundType
    stopSound: ExecuteActionSoundType
    def __init__(self, locateTracker: _Optional[_Union[ExecuteActionLocateTrackerType, _Mapping[str, object]]] = ..., startSound: _Optional[_Union[ExecuteActionSoundType, _Mapping[str, object]]] = ..., stopSound: _Optional[_Union[ExecuteActionSoundType, _Mapping[str, object]]] = ...) -> None: ...

class ExecuteActionLocateTrackerType(Message):
    __slots__ = ("lastHighTrafficEnablingTime", "contributorType")
    LASTHIGHTRAFFICENABLINGTIME_FIELD_NUMBER: _ClassVar[int]
    CONTRIBUTORTYPE_FIELD_NUMBER: _ClassVar[int]
    lastHighTrafficEnablingTime: _Common_pb2.Time
    contributorType: SpotContributorType
    def __init__(self, lastHighTrafficEnablingTime: _Optional[_Union[_Common_pb2.Time, _Mapping[str, object]]] = ..., contributorType: _Optional[_Union[SpotContributorType, str]] = ...) -> None: ...

class ExecuteActionSoundType(Message):
    __slots__ = ("component",)
    COMPONENT_FIELD_NUMBER: _ClassVar[int]
    component: DeviceComponent
    def __init__(self, component: _Optional[_Union[DeviceComponent, str]] = ...) -> None: ...

class ExecuteActionScope(Message):
    __slots__ = ("type", "device")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    type: DeviceType
    device: ExecuteActionDeviceIdentifier
    def __init__(self, type: _Optional[_Union[DeviceType, str]] = ..., device: _Optional[_Union[ExecuteActionDeviceIdentifier, _Mapping[str, object]]] = ...) -> None: ...

class ExecuteActionDeviceIdentifier(Message):
    __slots__ = ("canonicId",)
    CANONICID_FIELD_NUMBER: _ClassVar[int]
    canonicId: CanonicId
    def __init__(self, canonicId: _Optional[_Union[CanonicId, _Mapping[str, object]]] = ...) -> None: ...

class DeviceUpdate(Message):
    __slots__ = ("fcmMetadata", "deviceMetadata", "requestMetadata")
    FCMMETADATA_FIELD_NUMBER: _ClassVar[int]
    DEVICEMETADATA_FIELD_NUMBER: _ClassVar[int]
    REQUESTMETADATA_FIELD_NUMBER: _ClassVar[int]
    fcmMetadata: ExecuteActionRequestMetadata
    deviceMetadata: DeviceMetadata
    requestMetadata: RequestMetadata
    def __init__(self, fcmMetadata: _Optional[_Union[ExecuteActionRequestMetadata, _Mapping[str, object]]] = ..., deviceMetadata: _Optional[_Union[DeviceMetadata, _Mapping[str, object]]] = ..., requestMetadata: _Optional[_Union[RequestMetadata, _Mapping[str, object]]] = ...) -> None: ...

class DeviceMetadata(Message):
    __slots__ = ("identifierInformation", "information", "userDefinedDeviceName", "imageInformation")
    IDENTIFIERINFORMATION_FIELD_NUMBER: _ClassVar[int]
    INFORMATION_FIELD_NUMBER: _ClassVar[int]
    USERDEFINEDDEVICENAME_FIELD_NUMBER: _ClassVar[int]
    IMAGEINFORMATION_FIELD_NUMBER: _ClassVar[int]
    identifierInformation: IdentitfierInformation
    information: DeviceInformation
    userDefinedDeviceName: str
    imageInformation: ImageInformation
    def __init__(self, identifierInformation: _Optional[_Union[IdentitfierInformation, _Mapping[str, object]]] = ..., information: _Optional[_Union[DeviceInformation, _Mapping[str, object]]] = ..., userDefinedDeviceName: _Optional[str] = ..., imageInformation: _Optional[_Union[ImageInformation, _Mapping[str, object]]] = ...) -> None: ...

class ImageInformation(Message):
    __slots__ = ("imageUrl",)
    IMAGEURL_FIELD_NUMBER: _ClassVar[int]
    imageUrl: str
    def __init__(self, imageUrl: _Optional[str] = ...) -> None: ...

class IdentitfierInformation(Message):
    __slots__ = ("phoneInformation", "type", "canonicIds")
    PHONEINFORMATION_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    CANONICIDS_FIELD_NUMBER: _ClassVar[int]
    phoneInformation: PhoneInformation
    type: IdentifierInformationType
    canonicIds: CanonicIds
    def __init__(self, phoneInformation: _Optional[_Union[PhoneInformation, _Mapping[str, object]]] = ..., type: _Optional[_Union[IdentifierInformationType, str]] = ..., canonicIds: _Optional[_Union[CanonicIds, _Mapping[str, object]]] = ...) -> None: ...

class PhoneInformation(Message):
    __slots__ = ("canonicIds",)
    CANONICIDS_FIELD_NUMBER: _ClassVar[int]
    canonicIds: CanonicIds
    def __init__(self, canonicIds: _Optional[_Union[CanonicIds, _Mapping[str, object]]] = ...) -> None: ...

class CanonicIds(Message):
    __slots__ = ("canonicId",)
    CANONICID_FIELD_NUMBER: _ClassVar[int]
    canonicId: _containers.RepeatedCompositeFieldContainer[CanonicId]
    def __init__(self, canonicId: _Optional[_Iterable[_Union[CanonicId, _Mapping[str, object]]]] = ...) -> None: ...

class CanonicId(Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class DeviceInformation(Message):
    __slots__ = ("deviceRegistration", "locationInformation", "accessInformation")
    DEVICEREGISTRATION_FIELD_NUMBER: _ClassVar[int]
    LOCATIONINFORMATION_FIELD_NUMBER: _ClassVar[int]
    ACCESSINFORMATION_FIELD_NUMBER: _ClassVar[int]
    deviceRegistration: DeviceRegistration
    locationInformation: LocationInformation
    accessInformation: _containers.RepeatedCompositeFieldContainer[AccessInformation]
    def __init__(self, deviceRegistration: _Optional[_Union[DeviceRegistration, _Mapping[str, object]]] = ..., locationInformation: _Optional[_Union[LocationInformation, _Mapping[str, object]]] = ..., accessInformation: _Optional[_Iterable[_Union[AccessInformation, _Mapping[str, object]]]] = ...) -> None: ...

class DeviceTypeInformation(Message):
    __slots__ = ("deviceType",)
    DEVICETYPE_FIELD_NUMBER: _ClassVar[int]
    deviceType: SpotDeviceType
    def __init__(self, deviceType: _Optional[_Union[SpotDeviceType, str]] = ...) -> None: ...

class DeviceRegistration(Message):
    __slots__ = ("deviceTypeInformation", "encryptedUserSecrets", "manufacturer", "fastPairModelId", "pairDate", "model")
    DEVICETYPEINFORMATION_FIELD_NUMBER: _ClassVar[int]
    ENCRYPTEDUSERSECRETS_FIELD_NUMBER: _ClassVar[int]
    MANUFACTURER_FIELD_NUMBER: _ClassVar[int]
    FASTPAIRMODELID_FIELD_NUMBER: _ClassVar[int]
    PAIRDATE_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    deviceTypeInformation: DeviceTypeInformation
    encryptedUserSecrets: EncryptedUserSecrets
    manufacturer: str
    fastPairModelId: str
    pairDate: int
    model: str
    def __init__(self, deviceTypeInformation: _Optional[_Union[DeviceTypeInformation, _Mapping[str, object]]] = ..., encryptedUserSecrets: _Optional[_Union[EncryptedUserSecrets, _Mapping[str, object]]] = ..., manufacturer: _Optional[str] = ..., fastPairModelId: _Optional[str] = ..., pairDate: _Optional[int] = ..., model: _Optional[str] = ...) -> None: ...

class EncryptedUserSecrets(Message):
    __slots__ = ("encryptedIdentityKey", "ownerKeyVersion", "encryptedAccountKey", "creationDate", "encryptedSha256AccountKeyPublicAddress")
    ENCRYPTEDIDENTITYKEY_FIELD_NUMBER: _ClassVar[int]
    OWNERKEYVERSION_FIELD_NUMBER: _ClassVar[int]
    ENCRYPTEDACCOUNTKEY_FIELD_NUMBER: _ClassVar[int]
    CREATIONDATE_FIELD_NUMBER: _ClassVar[int]
    ENCRYPTEDSHA256ACCOUNTKEYPUBLICADDRESS_FIELD_NUMBER: _ClassVar[int]
    encryptedIdentityKey: bytes
    ownerKeyVersion: int
    encryptedAccountKey: bytes
    creationDate: _Common_pb2.Time
    encryptedSha256AccountKeyPublicAddress: bytes
    def __init__(self, encryptedIdentityKey: _Optional[bytes] = ..., ownerKeyVersion: _Optional[int] = ..., encryptedAccountKey: _Optional[bytes] = ..., creationDate: _Optional[_Union[_Common_pb2.Time, _Mapping[str, object]]] = ..., encryptedSha256AccountKeyPublicAddress: _Optional[bytes] = ...) -> None: ...

class LocationInformation(Message):
    __slots__ = ("reports",)
    REPORTS_FIELD_NUMBER: _ClassVar[int]
    reports: LocationsAndTimestampsWrapper
    def __init__(self, reports: _Optional[_Union[LocationsAndTimestampsWrapper, _Mapping[str, object]]] = ...) -> None: ...

class LocationsAndTimestampsWrapper(Message):
    __slots__ = ("recentLocationAndNetworkLocations",)
    RECENTLOCATIONANDNETWORKLOCATIONS_FIELD_NUMBER: _ClassVar[int]
    recentLocationAndNetworkLocations: RecentLocationAndNetworkLocations
    def __init__(self, recentLocationAndNetworkLocations: _Optional[_Union[RecentLocationAndNetworkLocations, _Mapping[str, object]]] = ...) -> None: ...

class RecentLocationAndNetworkLocations(Message):
    __slots__ = ("recentLocation", "recentLocationTimestamp", "networkLocations", "networkLocationTimestamps", "minLocationsNeededForAggregation")
    RECENTLOCATION_FIELD_NUMBER: _ClassVar[int]
    RECENTLOCATIONTIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    NETWORKLOCATIONS_FIELD_NUMBER: _ClassVar[int]
    NETWORKLOCATIONTIMESTAMPS_FIELD_NUMBER: _ClassVar[int]
    MINLOCATIONSNEEDEDFORAGGREGATION_FIELD_NUMBER: _ClassVar[int]
    recentLocation: _Common_pb2.LocationReport
    recentLocationTimestamp: _Common_pb2.Time
    networkLocations: _containers.RepeatedCompositeFieldContainer[_Common_pb2.LocationReport]
    networkLocationTimestamps: _containers.RepeatedCompositeFieldContainer[_Common_pb2.Time]
    minLocationsNeededForAggregation: int
    def __init__(self, recentLocation: _Optional[_Union[_Common_pb2.LocationReport, _Mapping[str, object]]] = ..., recentLocationTimestamp: _Optional[_Union[_Common_pb2.Time, _Mapping[str, object]]] = ..., networkLocations: _Optional[_Iterable[_Union[_Common_pb2.LocationReport, _Mapping[str, object]]]] = ..., networkLocationTimestamps: _Optional[_Iterable[_Union[_Common_pb2.Time, _Mapping[str, object]]]] = ..., minLocationsNeededForAggregation: _Optional[int] = ...) -> None: ...

class AccessInformation(Message):
    __slots__ = ("email", "hasAccess", "isOwner", "thisAccount")
    EMAIL_FIELD_NUMBER: _ClassVar[int]
    HASACCESS_FIELD_NUMBER: _ClassVar[int]
    ISOWNER_FIELD_NUMBER: _ClassVar[int]
    THISACCOUNT_FIELD_NUMBER: _ClassVar[int]
    email: str
    hasAccess: bool
    isOwner: bool
    thisAccount: bool
    def __init__(self, email: _Optional[str] = ..., hasAccess: bool = ..., isOwner: bool = ..., thisAccount: bool = ...) -> None: ...

class RequestMetadata(Message):
    __slots__ = ("responseTime",)
    RESPONSETIME_FIELD_NUMBER: _ClassVar[int]
    responseTime: _Common_pb2.Time
    def __init__(self, responseTime: _Optional[_Union[_Common_pb2.Time, _Mapping[str, object]]] = ...) -> None: ...

class EncryptionUnlockRequestExtras(Message):
    __slots__ = ("operation", "securityDomain", "sessionId")
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    SECURITYDOMAIN_FIELD_NUMBER: _ClassVar[int]
    SESSIONID_FIELD_NUMBER: _ClassVar[int]
    operation: int
    securityDomain: SecurityDomain
    sessionId: str
    def __init__(self, operation: _Optional[int] = ..., securityDomain: _Optional[_Union[SecurityDomain, _Mapping[str, object]]] = ..., sessionId: _Optional[str] = ...) -> None: ...

class SecurityDomain(Message):
    __slots__ = ("name", "unknown")
    NAME_FIELD_NUMBER: _ClassVar[int]
    UNKNOWN_FIELD_NUMBER: _ClassVar[int]
    name: str
    unknown: int
    def __init__(self, name: _Optional[str] = ..., unknown: _Optional[int] = ...) -> None: ...

class Location(Message):
    __slots__ = ("latitude", "longitude", "altitude")
    LATITUDE_FIELD_NUMBER: _ClassVar[int]
    LONGITUDE_FIELD_NUMBER: _ClassVar[int]
    ALTITUDE_FIELD_NUMBER: _ClassVar[int]
    latitude: int
    longitude: int
    altitude: int
    def __init__(self, latitude: _Optional[int] = ..., longitude: _Optional[int] = ..., altitude: _Optional[int] = ...) -> None: ...

class RegisterBleDeviceRequest(Message):
    __slots__ = ("fastPairModelId", "description", "capabilities", "e2eePublicKeyRegistration", "manufacturerName", "ringKey", "recoveryKey", "unwantedTrackingKey", "modelName")
    FASTPAIRMODELID_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    E2EEPUBLICKEYREGISTRATION_FIELD_NUMBER: _ClassVar[int]
    MANUFACTURERNAME_FIELD_NUMBER: _ClassVar[int]
    RINGKEY_FIELD_NUMBER: _ClassVar[int]
    RECOVERYKEY_FIELD_NUMBER: _ClassVar[int]
    UNWANTEDTRACKINGKEY_FIELD_NUMBER: _ClassVar[int]
    MODELNAME_FIELD_NUMBER: _ClassVar[int]
    fastPairModelId: str
    description: DeviceDescription
    capabilities: DeviceCapabilities
    e2eePublicKeyRegistration: E2EEPublicKeyRegistration
    manufacturerName: str
    ringKey: bytes
    recoveryKey: bytes
    unwantedTrackingKey: bytes
    modelName: str
    def __init__(self, fastPairModelId: _Optional[str] = ..., description: _Optional[_Union[DeviceDescription, _Mapping[str, object]]] = ..., capabilities: _Optional[_Union[DeviceCapabilities, _Mapping[str, object]]] = ..., e2eePublicKeyRegistration: _Optional[_Union[E2EEPublicKeyRegistration, _Mapping[str, object]]] = ..., manufacturerName: _Optional[str] = ..., ringKey: _Optional[bytes] = ..., recoveryKey: _Optional[bytes] = ..., unwantedTrackingKey: _Optional[bytes] = ..., modelName: _Optional[str] = ...) -> None: ...

class E2EEPublicKeyRegistration(Message):
    __slots__ = ("rotationExponent", "encryptedUserSecrets", "publicKeyIdList", "pairingDate")
    ROTATIONEXPONENT_FIELD_NUMBER: _ClassVar[int]
    ENCRYPTEDUSERSECRETS_FIELD_NUMBER: _ClassVar[int]
    PUBLICKEYIDLIST_FIELD_NUMBER: _ClassVar[int]
    PAIRINGDATE_FIELD_NUMBER: _ClassVar[int]
    rotationExponent: int
    encryptedUserSecrets: EncryptedUserSecrets
    publicKeyIdList: PublicKeyIdList
    pairingDate: int
    def __init__(self, rotationExponent: _Optional[int] = ..., encryptedUserSecrets: _Optional[_Union[EncryptedUserSecrets, _Mapping[str, object]]] = ..., publicKeyIdList: _Optional[_Union[PublicKeyIdList, _Mapping[str, object]]] = ..., pairingDate: _Optional[int] = ...) -> None: ...

class PublicKeyIdList(Message):
    __slots__ = ("publicKeyIdInfo",)
    class PublicKeyIdInfo(Message):
        __slots__ = ("timestamp", "publicKeyId", "trackableComponent")
        TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
        PUBLICKEYID_FIELD_NUMBER: _ClassVar[int]
        TRACKABLECOMPONENT_FIELD_NUMBER: _ClassVar[int]
        timestamp: _Common_pb2.Time
        publicKeyId: TruncatedEID
        trackableComponent: int
        def __init__(self, timestamp: _Optional[_Union[_Common_pb2.Time, _Mapping[str, object]]] = ..., publicKeyId: _Optional[_Union[TruncatedEID, _Mapping[str, object]]] = ..., trackableComponent: _Optional[int] = ...) -> None: ...
    PUBLICKEYIDINFO_FIELD_NUMBER: _ClassVar[int]
    publicKeyIdInfo: _containers.RepeatedCompositeFieldContainer[PublicKeyIdList.PublicKeyIdInfo]
    def __init__(self, publicKeyIdInfo: _Optional[_Iterable[_Union[PublicKeyIdList.PublicKeyIdInfo, _Mapping[str, object]]]] = ...) -> None: ...

class TruncatedEID(Message):
    __slots__ = ("truncatedEid",)
    TRUNCATEDEID_FIELD_NUMBER: _ClassVar[int]
    truncatedEid: bytes
    def __init__(self, truncatedEid: _Optional[bytes] = ...) -> None: ...

class UploadPrecomputedPublicKeyIdsRequest(Message):
    __slots__ = ("deviceEids",)
    class DevicePublicKeyIds(Message):
        __slots__ = ("canonicId", "clientList", "pairDate")
        CANONICID_FIELD_NUMBER: _ClassVar[int]
        CLIENTLIST_FIELD_NUMBER: _ClassVar[int]
        PAIRDATE_FIELD_NUMBER: _ClassVar[int]
        canonicId: CanonicId
        clientList: PublicKeyIdList
        pairDate: int
        def __init__(self, canonicId: _Optional[_Union[CanonicId, _Mapping[str, object]]] = ..., clientList: _Optional[_Union[PublicKeyIdList, _Mapping[str, object]]] = ..., pairDate: _Optional[int] = ...) -> None: ...
    DEVICEEIDS_FIELD_NUMBER: _ClassVar[int]
    deviceEids: _containers.RepeatedCompositeFieldContainer[UploadPrecomputedPublicKeyIdsRequest.DevicePublicKeyIds]
    def __init__(self, deviceEids: _Optional[_Iterable[_Union[UploadPrecomputedPublicKeyIdsRequest.DevicePublicKeyIds, _Mapping[str, object]]]] = ...) -> None: ...

class DeviceCapabilities(Message):
    __slots__ = ("isAdvertising", "capableComponents", "trackableComponents")
    ISADVERTISING_FIELD_NUMBER: _ClassVar[int]
    CAPABLECOMPONENTS_FIELD_NUMBER: _ClassVar[int]
    TRACKABLECOMPONENTS_FIELD_NUMBER: _ClassVar[int]
    isAdvertising: bool
    capableComponents: int
    trackableComponents: int
    def __init__(self, isAdvertising: bool = ..., capableComponents: _Optional[int] = ..., trackableComponents: _Optional[int] = ...) -> None: ...

class DeviceDescription(Message):
    __slots__ = ("userDefinedName", "deviceType", "deviceComponentsInformation")
    USERDEFINEDNAME_FIELD_NUMBER: _ClassVar[int]
    DEVICETYPE_FIELD_NUMBER: _ClassVar[int]
    DEVICECOMPONENTSINFORMATION_FIELD_NUMBER: _ClassVar[int]
    userDefinedName: str
    deviceType: SpotDeviceType
    deviceComponentsInformation: _containers.RepeatedCompositeFieldContainer[DeviceComponentInformation]
    def __init__(self, userDefinedName: _Optional[str] = ..., deviceType: _Optional[_Union[SpotDeviceType, str]] = ..., deviceComponentsInformation: _Optional[_Iterable[_Union[DeviceComponentInformation, _Mapping[str, object]]]] = ...) -> None: ...

class DeviceComponentInformation(Message):
    __slots__ = ("imageUrl",)
    IMAGEURL_FIELD_NUMBER: _ClassVar[int]
    imageUrl: str
    def __init__(self, imageUrl: _Optional[str] = ...) -> None: ...
