# tests/test_protobuf_namespace_conflict.py
"""Verify that custom protobuf modules coexist with the official google-protobuf library.

Home Assistant loads the official google-protobuf library (e.g. via the Nest
integration).  This custom integration ships its own .proto definitions whose
``package`` values (``google.rpc`` for ``RpcStatus``, plus custom packages)
must never collide with types already registered in the process-wide default
descriptor pool.

Originally, a vendored copy of ``google.protobuf.Any`` caused a
``duplicate symbol 'google.protobuf.Any'`` crash on Python >= 3.13 when
another integration loaded the official ``any_pb2``.  That vendored copy was
removed in favour of the official ``google.protobuf.any_pb2``; remaining
custom ``_pb2.py`` files use separate descriptor pools.  These tests guard
against regressions.
"""
from __future__ import annotations

import pytest
from google.protobuf import descriptor_pool as _descriptor_pool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_pool() -> _descriptor_pool.DescriptorPool:
    """Return the process-wide default descriptor pool."""
    return _descriptor_pool.Default()


# ---------------------------------------------------------------------------
# Vendored Any_pb2 must NOT exist (it was redundant with the official one)
# ---------------------------------------------------------------------------


class TestVendoredAnyCleaned:
    """The vendored Any_pb2 has been removed – it was identical to the official
    ``google.protobuf.any_pb2`` and caused a duplicate-symbol crash."""

    def test_any_pb2_not_importable(self) -> None:
        """Importing Any_pb2 from ProtoDecoders must raise ImportError."""
        with pytest.raises(ImportError):
            from custom_components.googlefindmy.ProtoDecoders import Any_pb2  # noqa: F401


# ---------------------------------------------------------------------------
# ProtoDecoders – separate pool assertions
# ---------------------------------------------------------------------------


class TestProtoDecodersSeparatePools:
    """Each vendored _pb2 module MUST use its own (non-default) descriptor pool."""

    def test_rpc_status_pb2_uses_separate_pool(self) -> None:
        """RpcStatus_pb2 must NOT register in the default pool."""
        from custom_components.googlefindmy.ProtoDecoders import RpcStatus_pb2

        assert hasattr(RpcStatus_pb2, "_rpc_pool")
        assert RpcStatus_pb2._rpc_pool is not _default_pool(), (
            "RpcStatus_pb2._rpc_pool must differ from the default descriptor "
            "pool – using the default pool would collide with "
            "googleapis-common-protos if another integration installs it"
        )

    def test_common_pb2_uses_separate_pool(self) -> None:
        """Common_pb2 must have its own pool that is NOT the default."""
        from custom_components.googlefindmy.ProtoDecoders import Common_pb2

        assert hasattr(Common_pb2, "_common_pool")
        assert Common_pb2._common_pool is not _default_pool()

    def test_device_update_pb2_shares_common_pool(self) -> None:
        """DeviceUpdate_pb2 depends on Common and must share its pool."""
        from custom_components.googlefindmy.ProtoDecoders import (
            Common_pb2,
            DeviceUpdate_pb2,
        )

        assert hasattr(DeviceUpdate_pb2, "_findmy_pool")
        assert DeviceUpdate_pb2._findmy_pool is Common_pb2._common_pool, (
            "DeviceUpdate_pb2 must share _common_pool with Common_pb2"
        )
        assert DeviceUpdate_pb2._findmy_pool is not _default_pool()

    def test_location_reports_upload_pb2_shares_common_pool(self) -> None:
        """LocationReportsUpload_pb2 depends on Common and must share its pool."""
        from custom_components.googlefindmy.ProtoDecoders import (
            Common_pb2,
            LocationReportsUpload_pb2,
        )

        assert hasattr(LocationReportsUpload_pb2, "_findmy_pool")
        assert LocationReportsUpload_pb2._findmy_pool is Common_pb2._common_pool
        assert LocationReportsUpload_pb2._findmy_pool is not _default_pool()


# ---------------------------------------------------------------------------
# Firebase proto – separate pool assertions
# ---------------------------------------------------------------------------


class TestFirebaseSeparatePools:
    """Firebase _pb2 modules must also avoid the default pool."""

    def test_android_checkin_pb2_uses_separate_pool(self) -> None:
        from custom_components.googlefindmy.Auth.firebase_messaging.proto import (
            android_checkin_pb2,
        )

        assert hasattr(android_checkin_pb2, "_firebase_pool")
        assert android_checkin_pb2._firebase_pool is not _default_pool()

    def test_mcs_pb2_uses_separate_pool(self) -> None:
        from custom_components.googlefindmy.Auth.firebase_messaging.proto import (
            mcs_pb2,
        )

        assert hasattr(mcs_pb2, "_firebase_pool")
        assert mcs_pb2._firebase_pool is not _default_pool()

    def test_checkin_pb2_shares_android_checkin_pool(self) -> None:
        from custom_components.googlefindmy.Auth.firebase_messaging.proto import (
            android_checkin_pb2,
            checkin_pb2,
        )

        assert checkin_pb2._firebase_pool is android_checkin_pb2._firebase_pool


# ---------------------------------------------------------------------------
# Coexistence with the official google-protobuf package
# ---------------------------------------------------------------------------


class TestOfficialProtobufCoexistence:
    """The custom modules must load without disturbing the official library."""

    def test_rpc_status_loads_alongside_official_any(self) -> None:
        """RpcStatus_pb2 must import cleanly when the official any_pb2 is loaded."""
        from google.protobuf import any_pb2  # noqa: F401
        from custom_components.googlefindmy.ProtoDecoders import RpcStatus_pb2  # noqa: F401

    def test_rpc_status_roundtrip(self) -> None:
        """The Status message must serialize and deserialize correctly."""
        from custom_components.googlefindmy.ProtoDecoders.RpcStatus_pb2 import Status

        msg = Status()
        msg.code = 7
        msg.message = "PERMISSION_DENIED"
        data = msg.SerializeToString()

        msg2 = Status()
        msg2.ParseFromString(data)
        assert msg2.code == 7
        assert msg2.message == "PERMISSION_DENIED"

    def test_official_any_instance_is_functional(self) -> None:
        """The official Any message must remain usable alongside our modules."""
        from google.protobuf import any_pb2 as official_any
        from custom_components.googlefindmy.ProtoDecoders import RpcStatus_pb2  # noqa: F401

        msg = official_any.Any()
        msg.type_url = "type.googleapis.com/google.protobuf.Duration"
        msg.value = b"\x08\x01"
        assert msg.type_url == "type.googleapis.com/google.protobuf.Duration"
        assert msg.value == b"\x08\x01"

    def test_default_pool_rejects_duplicate_any_symbol(self) -> None:
        """A second file defining ``google.protobuf.Any`` must be rejected.

        This proves that vendoring ``Any.proto`` under a different file name
        (as the project used to do) would crash at import time because the
        official ``any_pb2`` already registered the symbol in the default pool.
        """
        from google.protobuf import any_pb2  # noqa: F401 – ensure it's loaded

        # Simulate the OLD vendored Any.proto: same package and message,
        # but different file name (ProtoDecoders/Any.proto).
        _vendored_any_serialized = (
            b"\n\x17ProtoDecoders/Any.proto"
            b"\x12\x0fgoogle.protobuf"
            b'"&\n\x03Any'
            b"\x12\x10\n\x08type_url\x18\x01 \x01(\t"
            b"\x12\r\n\x05value\x18\x02 \x01(\x0c"
            b"b\x06proto3"
        )
        with pytest.raises(TypeError, match="(?i)duplicate|conflict|couldn't build"):
            _default_pool().AddSerializedFile(_vendored_any_serialized)


# ---------------------------------------------------------------------------
# google/ project-root directory must not shadow the installed package
# ---------------------------------------------------------------------------


class TestGoogleDirectoryNotShadowing:
    """The google/ type-stubs directory at the project root must not shadow."""

    def test_descriptor_pool_importable(self) -> None:
        """google.protobuf.descriptor_pool must resolve to the installed package."""
        from google.protobuf import descriptor_pool

        # If the project-root google/ dir were shadowing the installed package,
        # descriptor_pool would not be importable (only .pyi stubs live there).
        assert hasattr(descriptor_pool, "DescriptorPool")
        assert hasattr(descriptor_pool, "Default")

    def test_symbol_database_importable(self) -> None:
        """google.protobuf.symbol_database must resolve to the installed package."""
        from google.protobuf import symbol_database

        assert hasattr(symbol_database, "Default")

    def test_builder_importable(self) -> None:
        """google.protobuf.internal.builder must be the installed package."""
        from google.protobuf.internal import builder

        assert hasattr(builder, "BuildMessageAndEnumDescriptors")
        assert hasattr(builder, "BuildTopDescriptorsAndMessages")
