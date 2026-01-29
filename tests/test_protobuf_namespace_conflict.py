# tests/test_protobuf_namespace_conflict.py
"""Verify that custom protobuf modules coexist with the official google-protobuf library.

Home Assistant loads the official google-protobuf library (e.g. via the Nest
integration).  This custom integration ships its own .proto definitions
(``Common``, ``DeviceUpdate``, ``LocationReportsUpload``, Firebase protos)
that must never collide with types already registered in the process-wide
default descriptor pool.

Originally, vendored copies of ``google.protobuf.Any`` and
``google.rpc.Status`` caused duplicate-symbol crashes on Python >= 3.13 when
another integration loaded the official libraries.  Those copies were removed
in favour of the official packages (``protobuf`` and
``googleapis-common-protos``).  These tests guard against regressions.
"""
from __future__ import annotations

import importlib

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
    """The vendored Any_pb2 has been removed -- it was identical to the official
    ``google.protobuf.any_pb2`` and caused a duplicate-symbol crash."""

    def test_any_pb2_not_importable(self) -> None:
        """Importing Any_pb2 from ProtoDecoders must raise ImportError."""
        with pytest.raises(ImportError):
            from custom_components.googlefindmy.ProtoDecoders import Any_pb2  # noqa: F401


# ---------------------------------------------------------------------------
# RpcStatus: prefer official googleapis-common-protos, keep vendored fallback
# ---------------------------------------------------------------------------


class TestRpcStatusResolution:
    """nova_request must prefer the official google.rpc.status_pb2 when
    googleapis-common-protos is installed, and fall back to the vendored
    RpcStatus_pb2 otherwise."""

    def test_nova_request_rpc_status_available(self) -> None:
        """_RPC_STATUS_AVAILABLE must be True regardless of which provider is used."""
        from custom_components.googlefindmy.NovaApi import nova_request

        assert nova_request._RPC_STATUS_AVAILABLE is True
        assert nova_request.RpcStatus is not None

    def test_rpc_status_has_required_fields(self) -> None:
        """The resolved RpcStatus class must have code, message, details fields."""
        from custom_components.googlefindmy.NovaApi.nova_request import RpcStatus

        msg = RpcStatus()
        msg.code = 7
        msg.message = "PERMISSION_DENIED"
        data = msg.SerializeToString()

        msg2 = RpcStatus()
        msg2.ParseFromString(data)
        assert msg2.code == 7
        assert msg2.message == "PERMISSION_DENIED"

    def test_prefers_official_when_available(self) -> None:
        """When googleapis-common-protos is installed, the official Status is used."""
        official_available = importlib.util.find_spec("google.rpc") is not None
        if not official_available:
            pytest.skip("googleapis-common-protos not installed")

        from google.rpc.status_pb2 import Status as OfficialStatus
        from custom_components.googlefindmy.NovaApi.nova_request import RpcStatus

        assert RpcStatus is OfficialStatus

    def test_vendored_fallback_loads(self) -> None:
        """The vendored RpcStatus_pb2 fallback must remain importable."""
        from custom_components.googlefindmy.ProtoDecoders import RpcStatus_pb2

        msg = RpcStatus_pb2.Status()
        msg.code = 3
        msg.message = "INVALID_ARGUMENT"
        assert msg.code == 3

    def test_vendored_rpc_status_uses_separate_pool(self) -> None:
        """The vendored RpcStatus_pb2 must NOT use the default pool."""
        from custom_components.googlefindmy.ProtoDecoders import RpcStatus_pb2

        assert hasattr(RpcStatus_pb2, "_rpc_pool")
        assert RpcStatus_pb2._rpc_pool is not _default_pool()


# ---------------------------------------------------------------------------
# ProtoDecoders -- separate pool assertions
# ---------------------------------------------------------------------------


class TestProtoDecodersSeparatePools:
    """Each custom _pb2 module MUST use its own (non-default) descriptor pool."""

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
# Firebase proto -- separate pool assertions
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
        from google.protobuf import any_pb2  # noqa: F401 -- ensure it's loaded

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
# Standalone main.py: all proto dependencies must be importable
# ---------------------------------------------------------------------------


class TestStandaloneProtoDependencies:
    """The standalone main.py entry point (browser-based secrets extraction)
    requires protobuf at runtime.  Verify all proto modules are importable
    without Home Assistant."""

    def test_protobuf_package_importable(self) -> None:
        """The protobuf package itself must be importable."""
        import google.protobuf

        assert google.protobuf is not None

    def test_official_any_pb2_importable(self) -> None:
        """google.protobuf.any_pb2 must be importable (replaces vendored Any)."""
        from google.protobuf import any_pb2  # noqa: F401

    def test_all_proto_decoders_importable(self) -> None:
        """All ProtoDecoders modules must import without errors."""
        modules = [
            "custom_components.googlefindmy.ProtoDecoders.Common_pb2",
            "custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2",
            "custom_components.googlefindmy.ProtoDecoders.LocationReportsUpload_pb2",
            "custom_components.googlefindmy.ProtoDecoders.RpcStatus_pb2",
        ]
        for mod_name in modules:
            mod = importlib.import_module(mod_name)
            assert mod.DESCRIPTOR is not None, f"{mod_name} has no DESCRIPTOR"

    def test_all_firebase_protos_importable(self) -> None:
        """All Firebase proto modules must import without errors."""
        modules = [
            "custom_components.googlefindmy.Auth.firebase_messaging.proto.android_checkin_pb2",
            "custom_components.googlefindmy.Auth.firebase_messaging.proto.mcs_pb2",
            "custom_components.googlefindmy.Auth.firebase_messaging.proto.checkin_pb2",
        ]
        for mod_name in modules:
            mod = importlib.import_module(mod_name)
            assert mod.DESCRIPTOR is not None, f"{mod_name} has no DESCRIPTOR"

    def test_protobuf_in_requirements_txt(self) -> None:
        """protobuf must be declared in requirements.txt for standalone pip install."""
        from pathlib import Path

        req_file = (
            Path(__file__).resolve().parents[1]
            / "custom_components"
            / "googlefindmy"
            / "requirements.txt"
        )
        content = req_file.read_text()
        assert "protobuf" in content, (
            "protobuf must be listed in requirements.txt so standalone users "
            "who run 'pip install -r requirements.txt' get it installed"
        )


# ---------------------------------------------------------------------------
# google/ project-root directory must not shadow the installed package
# ---------------------------------------------------------------------------


class TestGoogleDirectoryNotShadowing:
    """The google/ type-stubs directory at the project root must not shadow."""

    def test_descriptor_pool_importable(self) -> None:
        """google.protobuf.descriptor_pool must resolve to the installed package."""
        from google.protobuf import descriptor_pool

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
