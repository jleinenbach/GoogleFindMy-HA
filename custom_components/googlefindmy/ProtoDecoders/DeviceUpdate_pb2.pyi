# custom_components/googlefindmy/ProtoDecoders/DeviceUpdate_pb2.pyi
from __future__ import annotations

from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from typing import (
    Any as _Any,
)
from typing import (
    ClassVar as _ClassVar,
)

from custom_components.googlefindmy.protobuf_typing import (
    EnumTypeWrapperMeta as _EnumTypeWrapperMeta,
)
from custom_components.googlefindmy.protobuf_typing import (
    MessageProto as _MessageProto,
)
from custom_components.googlefindmy.ProtoDecoders import Common_pb2 as _Common_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf.internal import containers as _containers

EnumTypeWrapper = _EnumTypeWrapperMeta[int]
Message = _message.Message
MessageProto = _MessageProto

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

class GetEidInfoForE2eeDevicesResponse(Message, _MessageProto):
    __slots__ = ("encryptedOwnerKeyAndMetadata",)
    ENCRYPTEDOWNERKEYANDMETADATA_FIELD_NUMBER: _ClassVar[int]
    encryptedOwnerKeyAndMetadata: EncryptedOwnerKeyAndMetadata
    def __init__(
        self,
        encryptedOwnerKeyAndMetadata: EncryptedOwnerKeyAndMetadata
        | _Mapping[str, _Any]
        | None = ...,
    ) -> None: ...

class EncryptedOwnerKeyAndMetadata(Message, _MessageProto):
    __slots__ = ("encryptedOwnerKey", "ownerKeyVersion", "securityDomain")
    ENCRYPTEDOWNERKEY_FIELD_NUMBER: _ClassVar[int]
    OWNERKEYVERSION_FIELD_NUMBER: _ClassVar[int]
    SECURITYDOMAIN_FIELD_NUMBER: _ClassVar[int]
    encryptedOwnerKey: bytes
    ownerKeyVersion: int
    securityDomain: str
    def __init__(
        self,
        encryptedOwnerKey: bytes | None = ...,
        ownerKeyVersion: int | None = ...,
        securityDomain: str | None = ...,
    ) -> None: ...

class DevicesList(Message, _MessageProto):
    __slots__ = ("deviceMetadata",)
    DEVICEMETADATA_FIELD_NUMBER: _ClassVar[int]
    deviceMetadata: _containers.RepeatedCompositeFieldContainer[DeviceMetadata]
    def __init__(
        self,
        deviceMetadata: _Iterable[DeviceMetadata | _Mapping[str, _Any]] | None = ...,
    ) -> None: ...

class DevicesListRequest(Message, _MessageProto):
    __slots__ = ("deviceListRequestPayload",)
    DEVICELISTREQUESTPAYLOAD_FIELD_NUMBER: _ClassVar[int]
    deviceListRequestPayload: DevicesListRequestPayload
    def __init__(
        self,
        deviceListRequestPayload: DevicesListRequestPayload
        | _Mapping[str, _Any]
        | None = ...,
    ) -> None: ...

class DevicesListRequestPayload(Message, _MessageProto):
    __slots__ = ("type", "id")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    type: DeviceType
    id: str
    def __init__(
        self, type: DeviceType | str | None = ..., id: str | None = ...
    ) -> None: ...

class ExecuteActionRequest(Message, _MessageProto):
    __slots__ = ("scope", "action", "requestMetadata")
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    REQUESTMETADATA_FIELD_NUMBER: _ClassVar[int]
    scope: ExecuteActionScope
    action: ExecuteActionType
    requestMetadata: ExecuteActionRequestMetadata
    def __init__(
        self,
        scope: ExecuteActionScope | _Mapping[str, _Any] | None = ...,
        action: ExecuteActionType | _Mapping[str, _Any] | None = ...,
        requestMetadata: ExecuteActionRequestMetadata
        | _Mapping[str, _Any]
        | None = ...,
    ) -> None: ...

class ExecuteActionRequestMetadata(Message, _MessageProto):
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
    def __init__(
        self,
        type: DeviceType | str | None = ...,
        requestUuid: str | None = ...,
        fmdClientUuid: str | None = ...,
        gcmRegistrationId: GcmCloudMessagingIdProtobuf
        | _Mapping[str, _Any]
        | None = ...,
        unknown: bool = ...,
    ) -> None: ...

class GcmCloudMessagingIdProtobuf(Message, _MessageProto):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: str | None = ...) -> None: ...

class ExecuteActionType(Message, _MessageProto):
    __slots__ = ("locateTracker", "startSound", "stopSound")
    LOCATETRACKER_FIELD_NUMBER: _ClassVar[int]
    STARTSOUND_FIELD_NUMBER: _ClassVar[int]
    STOPSOUND_FIELD_NUMBER: _ClassVar[int]
    locateTracker: ExecuteActionLocateTrackerType
    startSound: ExecuteActionSoundType
    stopSound: ExecuteActionSoundType
    def __init__(
        self,
        locateTracker: ExecuteActionLocateTrackerType
        | _Mapping[str, _Any]
        | None = ...,
        startSound: ExecuteActionSoundType | _Mapping[str, _Any] | None = ...,
        stopSound: ExecuteActionSoundType | _Mapping[str, _Any] | None = ...,
    ) -> None: ...

class ExecuteActionLocateTrackerType(Message, _MessageProto):
    __slots__ = ("lastHighTrafficEnablingTime", "contributorType")
    LASTHIGHTRAFFICENABLINGTIME_FIELD_NUMBER: _ClassVar[int]
    CONTRIBUTORTYPE_FIELD_NUMBER: _ClassVar[int]
    lastHighTrafficEnablingTime: _Common_pb2.Time
    contributorType: SpotContributorType
    def __init__(
        self,
        lastHighTrafficEnablingTime: _Common_pb2.Time
        | _Mapping[str, _Any]
        | None = ...,
        contributorType: SpotContributorType | str | None = ...,
    ) -> None: ...

class ExecuteActionSoundType(Message, _MessageProto):
    __slots__ = ("component",)
    COMPONENT_FIELD_NUMBER: _ClassVar[int]
    component: DeviceComponent
    def __init__(self, component: DeviceComponent | str | None = ...) -> None: ...

class ExecuteActionScope(Message, _MessageProto):
    __slots__ = ("type", "device")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    type: DeviceType
    device: ExecuteActionDeviceIdentifier
    def __init__(
        self,
        type: DeviceType | str | None = ...,
        device: ExecuteActionDeviceIdentifier | _Mapping[str, _Any] | None = ...,
    ) -> None: ...

class ExecuteActionDeviceIdentifier(Message, _MessageProto):
    __slots__ = ("canonicId",)
    CANONICID_FIELD_NUMBER: _ClassVar[int]
    canonicId: CanonicId
    def __init__(
        self, canonicId: CanonicId | _Mapping[str, _Any] | None = ...
    ) -> None: ...

class DeviceUpdate(Message, _MessageProto):
    __slots__ = ("fcmMetadata", "deviceMetadata", "requestMetadata")
    FCMMETADATA_FIELD_NUMBER: _ClassVar[int]
    DEVICEMETADATA_FIELD_NUMBER: _ClassVar[int]
    REQUESTMETADATA_FIELD_NUMBER: _ClassVar[int]
    fcmMetadata: ExecuteActionRequestMetadata
    deviceMetadata: DeviceMetadata
    requestMetadata: RequestMetadata
    def __init__(
        self,
        fcmMetadata: ExecuteActionRequestMetadata | _Mapping[str, _Any] | None = ...,
        deviceMetadata: DeviceMetadata | _Mapping[str, _Any] | None = ...,
        requestMetadata: RequestMetadata | _Mapping[str, _Any] | None = ...,
    ) -> None: ...

class DeviceMetadata(Message, _MessageProto):
    __slots__ = (
        "identifierInformation",
        "information",
        "userDefinedDeviceName",
        "imageInformation",
    )
    IDENTIFIERINFORMATION_FIELD_NUMBER: _ClassVar[int]
    INFORMATION_FIELD_NUMBER: _ClassVar[int]
    USERDEFINEDDEVICENAME_FIELD_NUMBER: _ClassVar[int]
    IMAGEINFORMATION_FIELD_NUMBER: _ClassVar[int]
    identifierInformation: IdentitfierInformation
    information: DeviceInformation
    userDefinedDeviceName: str
    imageInformation: ImageInformation
    def __init__(
        self,
        identifierInformation: IdentitfierInformation
        | _Mapping[str, _Any]
        | None = ...,
        information: DeviceInformation | _Mapping[str, _Any] | None = ...,
        userDefinedDeviceName: str | None = ...,
        imageInformation: ImageInformation | _Mapping[str, _Any] | None = ...,
    ) -> None: ...

class ImageInformation(Message, _MessageProto):
    __slots__ = ("imageUrl",)
    IMAGEURL_FIELD_NUMBER: _ClassVar[int]
    imageUrl: str
    def __init__(self, imageUrl: str | None = ...) -> None: ...

class IdentitfierInformation(Message, _MessageProto):
    __slots__ = ("phoneInformation", "type", "canonicIds")
    PHONEINFORMATION_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    CANONICIDS_FIELD_NUMBER: _ClassVar[int]
    phoneInformation: PhoneInformation
    type: IdentifierInformationType
    canonicIds: CanonicIds
    def __init__(
        self,
        phoneInformation: PhoneInformation | _Mapping[str, _Any] | None = ...,
        type: IdentifierInformationType | str | None = ...,
        canonicIds: CanonicIds | _Mapping[str, _Any] | None = ...,
    ) -> None: ...

class PhoneInformation(Message, _MessageProto):
    __slots__ = ("canonicIds",)
    CANONICIDS_FIELD_NUMBER: _ClassVar[int]
    canonicIds: CanonicIds
    def __init__(
        self, canonicIds: CanonicIds | _Mapping[str, _Any] | None = ...
    ) -> None: ...

class CanonicIds(Message, _MessageProto):
    __slots__ = ("canonicId",)
    CANONICID_FIELD_NUMBER: _ClassVar[int]
    canonicId: _containers.RepeatedCompositeFieldContainer[CanonicId]
    def __init__(
        self,
        canonicId: _Iterable[CanonicId | _Mapping[str, _Any]] | None = ...,
    ) -> None: ...

class CanonicId(Message, _MessageProto):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: str | None = ...) -> None: ...

class DeviceInformation(Message, _MessageProto):
    __slots__ = ("deviceRegistration", "locationInformation", "accessInformation")
    DEVICEREGISTRATION_FIELD_NUMBER: _ClassVar[int]
    LOCATIONINFORMATION_FIELD_NUMBER: _ClassVar[int]
    ACCESSINFORMATION_FIELD_NUMBER: _ClassVar[int]
    deviceRegistration: DeviceRegistration
    locationInformation: LocationInformation
    accessInformation: _containers.RepeatedCompositeFieldContainer[AccessInformation]
    def __init__(
        self,
        deviceRegistration: DeviceRegistration | _Mapping[str, _Any] | None = ...,
        locationInformation: LocationInformation | _Mapping[str, _Any] | None = ...,
        accessInformation: _Iterable[AccessInformation | _Mapping[str, _Any]]
        | None = ...,
    ) -> None: ...

class DeviceTypeInformation(Message, _MessageProto):
    __slots__ = ("deviceType",)
    DEVICETYPE_FIELD_NUMBER: _ClassVar[int]
    deviceType: SpotDeviceType
    def __init__(self, deviceType: SpotDeviceType | str | None = ...) -> None: ...

class DeviceRegistration(Message, _MessageProto):
    __slots__ = (
        "deviceTypeInformation",
        "encryptedUserSecrets",
        "manufacturer",
        "fastPairModelId",
        "pairDate",
        "model",
    )
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
    def __init__(
        self,
        deviceTypeInformation: DeviceTypeInformation | _Mapping[str, _Any] | None = ...,
        encryptedUserSecrets: EncryptedUserSecrets | _Mapping[str, _Any] | None = ...,
        manufacturer: str | None = ...,
        fastPairModelId: str | None = ...,
        pairDate: int | None = ...,
        model: str | None = ...,
    ) -> None: ...

class EncryptedUserSecrets(Message, _MessageProto):
    __slots__ = (
        "encryptedIdentityKey",
        "ownerKeyVersion",
        "encryptedAccountKey",
        "creationDate",
        "encryptedSha256AccountKeyPublicAddress",
    )
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
    def __init__(
        self,
        encryptedIdentityKey: bytes | None = ...,
        ownerKeyVersion: int | None = ...,
        encryptedAccountKey: bytes | None = ...,
        creationDate: _Common_pb2.Time | _Mapping[str, _Any] | None = ...,
        encryptedSha256AccountKeyPublicAddress: bytes | None = ...,
    ) -> None: ...

class LocationInformation(Message, _MessageProto):
    __slots__ = ("reports",)
    REPORTS_FIELD_NUMBER: _ClassVar[int]
    reports: LocationsAndTimestampsWrapper
    def __init__(
        self,
        reports: LocationsAndTimestampsWrapper | _Mapping[str, _Any] | None = ...,
    ) -> None: ...

class LocationsAndTimestampsWrapper(Message, _MessageProto):
    __slots__ = ("recentLocationAndNetworkLocations",)
    RECENTLOCATIONANDNETWORKLOCATIONS_FIELD_NUMBER: _ClassVar[int]
    recentLocationAndNetworkLocations: RecentLocationAndNetworkLocations
    def __init__(
        self,
        recentLocationAndNetworkLocations: RecentLocationAndNetworkLocations
        | _Mapping[str, _Any]
        | None = ...,
    ) -> None: ...

class RecentLocationAndNetworkLocations(Message, _MessageProto):
    __slots__ = (
        "recentLocation",
        "recentLocationTimestamp",
        "networkLocations",
        "networkLocationTimestamps",
        "minLocationsNeededForAggregation",
    )
    RECENTLOCATION_FIELD_NUMBER: _ClassVar[int]
    RECENTLOCATIONTIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    NETWORKLOCATIONS_FIELD_NUMBER: _ClassVar[int]
    NETWORKLOCATIONTIMESTAMPS_FIELD_NUMBER: _ClassVar[int]
    MINLOCATIONSNEEDEDFORAGGREGATION_FIELD_NUMBER: _ClassVar[int]
    recentLocation: _Common_pb2.LocationReport
    recentLocationTimestamp: _Common_pb2.Time
    networkLocations: _containers.RepeatedCompositeFieldContainer[
        _Common_pb2.LocationReport
    ]
    networkLocationTimestamps: _containers.RepeatedCompositeFieldContainer[
        _Common_pb2.Time
    ]
    minLocationsNeededForAggregation: int
    def __init__(
        self,
        recentLocation: _Common_pb2.LocationReport | _Mapping[str, _Any] | None = ...,
        recentLocationTimestamp: _Common_pb2.Time | _Mapping[str, _Any] | None = ...,
        networkLocations: _Iterable[_Common_pb2.LocationReport | _Mapping[str, _Any]]
        | None = ...,
        networkLocationTimestamps: _Iterable[_Common_pb2.Time | _Mapping[str, _Any]]
        | None = ...,
        minLocationsNeededForAggregation: int | None = ...,
    ) -> None: ...

class AccessInformation(Message, _MessageProto):
    __slots__ = ("email", "hasAccess", "isOwner", "thisAccount")
    EMAIL_FIELD_NUMBER: _ClassVar[int]
    HASACCESS_FIELD_NUMBER: _ClassVar[int]
    ISOWNER_FIELD_NUMBER: _ClassVar[int]
    THISACCOUNT_FIELD_NUMBER: _ClassVar[int]
    email: str
    hasAccess: bool
    isOwner: bool
    thisAccount: bool
    def __init__(
        self,
        email: str | None = ...,
        hasAccess: bool = ...,
        isOwner: bool = ...,
        thisAccount: bool = ...,
    ) -> None: ...

class RequestMetadata(Message, _MessageProto):
    __slots__ = ("responseTime",)
    RESPONSETIME_FIELD_NUMBER: _ClassVar[int]
    responseTime: _Common_pb2.Time
    def __init__(
        self,
        responseTime: _Common_pb2.Time | _Mapping[str, _Any] | None = ...,
    ) -> None: ...

class EncryptionUnlockRequestExtras(Message, _MessageProto):
    __slots__ = ("operation", "securityDomain", "sessionId")
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    SECURITYDOMAIN_FIELD_NUMBER: _ClassVar[int]
    SESSIONID_FIELD_NUMBER: _ClassVar[int]
    operation: int
    securityDomain: SecurityDomain
    sessionId: str
    def __init__(
        self,
        operation: int | None = ...,
        securityDomain: SecurityDomain | _Mapping[str, _Any] | None = ...,
        sessionId: str | None = ...,
    ) -> None: ...

class SecurityDomain(Message, _MessageProto):
    __slots__ = ("name", "unknown")
    NAME_FIELD_NUMBER: _ClassVar[int]
    UNKNOWN_FIELD_NUMBER: _ClassVar[int]
    name: str
    unknown: int
    def __init__(self, name: str | None = ..., unknown: int | None = ...) -> None: ...

class Location(Message, _MessageProto):
    __slots__ = ("latitude", "longitude", "altitude")
    LATITUDE_FIELD_NUMBER: _ClassVar[int]
    LONGITUDE_FIELD_NUMBER: _ClassVar[int]
    ALTITUDE_FIELD_NUMBER: _ClassVar[int]
    latitude: int
    longitude: int
    altitude: int
    def __init__(
        self,
        latitude: int | None = ...,
        longitude: int | None = ...,
        altitude: int | None = ...,
    ) -> None: ...

class RegisterBleDeviceRequest(Message, _MessageProto):
    __slots__ = (
        "fastPairModelId",
        "description",
        "capabilities",
        "e2eePublicKeyRegistration",
        "manufacturerName",
        "ringKey",
        "recoveryKey",
        "unwantedTrackingKey",
        "modelName",
    )
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
    def __init__(
        self,
        fastPairModelId: str | None = ...,
        description: DeviceDescription | _Mapping[str, _Any] | None = ...,
        capabilities: DeviceCapabilities | _Mapping[str, _Any] | None = ...,
        e2eePublicKeyRegistration: E2EEPublicKeyRegistration
        | _Mapping[str, _Any]
        | None = ...,
        manufacturerName: str | None = ...,
        ringKey: bytes | None = ...,
        recoveryKey: bytes | None = ...,
        unwantedTrackingKey: bytes | None = ...,
        modelName: str | None = ...,
    ) -> None: ...

class E2EEPublicKeyRegistration(Message, _MessageProto):
    __slots__ = (
        "rotationExponent",
        "encryptedUserSecrets",
        "publicKeyIdList",
        "pairingDate",
    )
    ROTATIONEXPONENT_FIELD_NUMBER: _ClassVar[int]
    ENCRYPTEDUSERSECRETS_FIELD_NUMBER: _ClassVar[int]
    PUBLICKEYIDLIST_FIELD_NUMBER: _ClassVar[int]
    PAIRINGDATE_FIELD_NUMBER: _ClassVar[int]
    rotationExponent: int
    encryptedUserSecrets: EncryptedUserSecrets
    publicKeyIdList: PublicKeyIdList
    pairingDate: int
    def __init__(
        self,
        rotationExponent: int | None = ...,
        encryptedUserSecrets: EncryptedUserSecrets | _Mapping[str, _Any] | None = ...,
        publicKeyIdList: PublicKeyIdList | _Mapping[str, _Any] | None = ...,
        pairingDate: int | None = ...,
    ) -> None: ...

class PublicKeyIdList(Message, _MessageProto):
    __slots__ = ("publicKeyIdInfo",)
    class PublicKeyIdInfo(Message, _MessageProto):
        __slots__ = ("timestamp", "publicKeyId", "trackableComponent")
        TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
        PUBLICKEYID_FIELD_NUMBER: _ClassVar[int]
        TRACKABLECOMPONENT_FIELD_NUMBER: _ClassVar[int]
        timestamp: _Common_pb2.Time
        publicKeyId: TruncatedEID
        trackableComponent: int
        def __init__(
            self,
            timestamp: _Common_pb2.Time | _Mapping[str, _Any] | None = ...,
            publicKeyId: TruncatedEID | _Mapping[str, _Any] | None = ...,
            trackableComponent: int | None = ...,
        ) -> None: ...

    PUBLICKEYIDINFO_FIELD_NUMBER: _ClassVar[int]
    publicKeyIdInfo: _containers.RepeatedCompositeFieldContainer[
        PublicKeyIdList.PublicKeyIdInfo
    ]
    def __init__(
        self,
        publicKeyIdInfo: _Iterable[
            PublicKeyIdList.PublicKeyIdInfo | _Mapping[str, _Any]
        ]
        | None = ...,
    ) -> None: ...

class TruncatedEID(Message, _MessageProto):
    __slots__ = ("truncatedEid",)
    TRUNCATEDEID_FIELD_NUMBER: _ClassVar[int]
    truncatedEid: bytes
    def __init__(self, truncatedEid: bytes | None = ...) -> None: ...

class UploadPrecomputedPublicKeyIdsRequest(Message, _MessageProto):
    __slots__ = ("deviceEids",)
    class DevicePublicKeyIds(Message, _MessageProto):
        __slots__ = ("canonicId", "clientList", "pairDate")
        CANONICID_FIELD_NUMBER: _ClassVar[int]
        CLIENTLIST_FIELD_NUMBER: _ClassVar[int]
        PAIRDATE_FIELD_NUMBER: _ClassVar[int]
        canonicId: CanonicId
        clientList: PublicKeyIdList
        pairDate: int
        def __init__(
            self,
            canonicId: CanonicId | _Mapping[str, _Any] | None = ...,
            clientList: PublicKeyIdList | _Mapping[str, _Any] | None = ...,
            pairDate: int | None = ...,
        ) -> None: ...

    DEVICEEIDS_FIELD_NUMBER: _ClassVar[int]
    deviceEids: _containers.RepeatedCompositeFieldContainer[
        UploadPrecomputedPublicKeyIdsRequest.DevicePublicKeyIds
    ]
    def __init__(
        self,
        deviceEids: _Iterable[
            UploadPrecomputedPublicKeyIdsRequest.DevicePublicKeyIds
            | _Mapping[str, _Any]
        ]
        | None = ...,
    ) -> None: ...

class DeviceCapabilities(Message, _MessageProto):
    __slots__ = ("isAdvertising", "capableComponents", "trackableComponents")
    ISADVERTISING_FIELD_NUMBER: _ClassVar[int]
    CAPABLECOMPONENTS_FIELD_NUMBER: _ClassVar[int]
    TRACKABLECOMPONENTS_FIELD_NUMBER: _ClassVar[int]
    isAdvertising: bool
    capableComponents: int
    trackableComponents: int
    def __init__(
        self,
        isAdvertising: bool = ...,
        capableComponents: int | None = ...,
        trackableComponents: int | None = ...,
    ) -> None: ...

class DeviceDescription(Message, _MessageProto):
    __slots__ = ("userDefinedName", "deviceType", "deviceComponentsInformation")
    USERDEFINEDNAME_FIELD_NUMBER: _ClassVar[int]
    DEVICETYPE_FIELD_NUMBER: _ClassVar[int]
    DEVICECOMPONENTSINFORMATION_FIELD_NUMBER: _ClassVar[int]
    userDefinedName: str
    deviceType: SpotDeviceType
    deviceComponentsInformation: _containers.RepeatedCompositeFieldContainer[
        DeviceComponentInformation
    ]
    def __init__(
        self,
        userDefinedName: str | None = ...,
        deviceType: SpotDeviceType | str | None = ...,
        deviceComponentsInformation: _Iterable[
            DeviceComponentInformation | _Mapping[str, _Any]
        ]
        | None = ...,
    ) -> None: ...

class DeviceComponentInformation(Message, _MessageProto):
    __slots__ = ("imageUrl",)
    IMAGEURL_FIELD_NUMBER: _ClassVar[int]
    imageUrl: str
    def __init__(self, imageUrl: str | None = ...) -> None: ...
