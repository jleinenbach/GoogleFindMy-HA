# google/protobuf/descriptor.pyi
from __future__ import annotations

from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from collections.abc import Sequence as _Sequence

class DescriptorBase:
    """Minimal structural stub for protobuf descriptor types."""

    name: str
    full_name: str


class EnumDescriptor(DescriptorBase):
    values_by_name: _Mapping[str, EnumValueDescriptor]
    values: _Sequence[EnumValueDescriptor]


class EnumValueDescriptor(DescriptorBase):
    index: int
    number: int


class FieldDescriptor(DescriptorBase):
    number: int
    type: int
    label: int


class Descriptor(DescriptorBase):
    fields: _Sequence[FieldDescriptor]
    enum_types: _Sequence[EnumDescriptor]


class FileDescriptor(DescriptorBase):
    package: str
    dependencies: _Sequence[FileDescriptor]
    message_types_by_name: _Mapping[str, Descriptor]
    enum_types_by_name: _Mapping[str, EnumDescriptor]
    serialized_pb: bytes

    def GetMessages(self, packages: _Iterable[str]) -> _Mapping[str, Descriptor]: ...


class ServiceDescriptor(DescriptorBase):
    methods: _Sequence[MethodDescriptor]


class MethodDescriptor(DescriptorBase):
    input_type: Descriptor
    output_type: Descriptor


__all__ = [
    "Descriptor",
    "DescriptorBase",
    "EnumDescriptor",
    "EnumValueDescriptor",
    "FieldDescriptor",
    "FileDescriptor",
    "MethodDescriptor",
    "ServiceDescriptor",
]
