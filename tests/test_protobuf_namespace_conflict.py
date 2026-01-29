# tests/test_protobuf_namespace_conflict.py
"""Verify that custom protobuf modules coexist with the official google-protobuf library.

Home Assistant loads the official google-protobuf library (e.g. via the Nest
integration).  This custom integration ships its own .proto definitions whose
``package`` is ``google.protobuf`` (for ``Any``) and ``google.rpc`` (for
``RpcStatus``).  On Python >= 3.13 the protobuf runtime is very strict about
duplicate symbol registration in the default descriptor pool.

Commit edc1a4f introduced **separate descriptor pools** for every vendored
``_pb2.py`` file so that the custom definitions never collide with the
official ones.  These tests guard against regressions.
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
# ProtoDecoders – separate pool assertions
# ---------------------------------------------------------------------------


class TestProtoDecodersSeparatePools:
    """Each vendored _pb2 module MUST use its own (non-default) descriptor pool."""

    def test_any_pb2_uses_separate_pool(self) -> None:
        """Any_pb2 must NOT register in the default pool."""
        from custom_components.googlefindmy.ProtoDecoders import Any_pb2

        assert hasattr(Any_pb2, "_any_pool"), (
            "Any_pb2 must expose _any_pool for downstream dependants"
        )
        assert Any_pb2._any_pool is not _default_pool(), (
            "Any_pb2._any_pool must differ from the default descriptor pool – "
            "using the default pool causes 'duplicate symbol google.protobuf.Any' "
            "when another integration (e.g. Nest) loads the official any_pb2"
        )

    def test_rpc_status_pb2_shares_any_pool(self) -> None:
        """RpcStatus_pb2 depends on Any and must share its pool."""
        from custom_components.googlefindmy.ProtoDecoders import (
            Any_pb2,
            RpcStatus_pb2,
        )

        assert hasattr(RpcStatus_pb2, "_rpc_pool")
        assert RpcStatus_pb2._rpc_pool is Any_pb2._any_pool, (
            "RpcStatus_pb2 must share _any_pool with Any_pb2 so that the "
            "google.protobuf.Any dependency resolves within the same pool"
        )
        assert RpcStatus_pb2._rpc_pool is not _default_pool()

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

    def test_official_any_pb2_loads_after_custom(self) -> None:
        """Loading the official any_pb2 after our custom one must not raise."""
        # Import custom first
        from custom_components.googlefindmy.ProtoDecoders import Any_pb2  # noqa: F401

        # Then import official
        from google.protobuf import any_pb2 as official_any  # noqa: F401

    def test_official_any_pb2_loads_before_custom(self) -> None:
        """Loading the official any_pb2 before our custom one must not raise."""
        from google.protobuf import any_pb2 as official_any  # noqa: F401
        from custom_components.googlefindmy.ProtoDecoders import Any_pb2  # noqa: F401

    def test_custom_any_and_official_any_are_distinct_types(self) -> None:
        """The two Any message classes must NOT be the same Python type."""
        from google.protobuf import any_pb2 as official_any
        from custom_components.googlefindmy.ProtoDecoders import Any_pb2 as custom_any

        assert type(official_any.Any()) is not type(custom_any.Any()), (
            "Official and custom Any must be distinct types to avoid cross-talk"
        )

    def test_custom_any_instance_is_functional(self) -> None:
        """The custom Any message must support basic field access."""
        from custom_components.googlefindmy.ProtoDecoders import Any_pb2

        msg = Any_pb2.Any()
        msg.type_url = "type.googleapis.com/test"
        msg.value = b"\x01\x02\x03"
        assert msg.type_url == "type.googleapis.com/test"
        assert msg.value == b"\x01\x02\x03"

    def test_official_any_instance_is_functional(self) -> None:
        """The official Any message must remain usable alongside the custom one."""
        from google.protobuf import any_pb2 as official_any
        from custom_components.googlefindmy.ProtoDecoders import Any_pb2  # noqa: F401

        msg = official_any.Any()
        msg.type_url = "type.googleapis.com/google.protobuf.Duration"
        msg.value = b"\x08\x01"
        assert msg.type_url == "type.googleapis.com/google.protobuf.Duration"
        assert msg.value == b"\x08\x01"


# ---------------------------------------------------------------------------
# Regression: default pool MUST reject the duplicate
# ---------------------------------------------------------------------------


class TestDefaultPoolRejectsDuplicate:
    """Adding our Any.proto to the default pool must fail (regression guard)."""

    def test_default_pool_rejects_custom_any_serialized_file(self) -> None:
        """Proves the separate pool is *necessary* – the default pool conflicts."""
        from custom_components.googlefindmy.ProtoDecoders import Any_pb2

        # The official any_pb2 is already registered in the default pool.
        # Ensure it is loaded:
        from google.protobuf import any_pb2 as _official  # noqa: F401

        serialized = Any_pb2.DESCRIPTOR.serialized_pb
        with pytest.raises(TypeError, match="(?i)duplicate|conflict|couldn't build"):
            _default_pool().AddSerializedFile(serialized)


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
