# custom_components/googlefindmy/__init__.pyi
from __future__ import annotations

from typing import Any

from .ProtoDecoders import Common_pb2, DeviceUpdate_pb2, LocationReportsUpload_pb2

__all__ = [
    "Common_pb2",
    "DeviceUpdate_pb2",
    "LocationReportsUpload_pb2",
]


def __getattr__(name: str) -> Any: ...
