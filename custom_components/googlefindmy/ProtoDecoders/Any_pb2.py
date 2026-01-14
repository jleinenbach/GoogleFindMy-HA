# custom_components/googlefindmy/ProtoDecoders/Any_pb2.py
#
# Vendored google.protobuf.Any definition (Python).
# Manually implemented to avoid dependency on googleapis-common-protos.
#
# Reference: https://github.com/protocolbuffers/protobuf/blob/master/src/google/protobuf/any.proto
"""Vendored google.protobuf.Any for parsing Google API error responses."""
from __future__ import annotations

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection

_ANY_DESCRIPTOR = _descriptor.Descriptor(
    name='Any',
    full_name='google.protobuf.Any',
    filename=None,
    file=None,
    containing_type=None,
    create_key=None,
    fields=[
        _descriptor.FieldDescriptor(
            name='type_url',
            full_name='google.protobuf.Any.type_url',
            index=0,
            number=1,
            type=9,  # TYPE_STRING
            cpp_type=9,
            label=1,  # LABEL_OPTIONAL
            has_default_value=False,
            default_value=b"".decode('utf-8'),
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=None,
            create_key=None,
        ),
        _descriptor.FieldDescriptor(
            name='value',
            full_name='google.protobuf.Any.value',
            index=1,
            number=2,
            type=12,  # TYPE_BYTES
            cpp_type=9,
            label=1,  # LABEL_OPTIONAL
            has_default_value=False,
            default_value=b"",
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=None,
            create_key=None,
        ),
    ],
    extensions=[],
    nested_types=[],
    enum_types=[],
    serialized_options=None,
    is_extendable=False,
    syntax='proto3',
    extension_ranges=[],
    oneofs=[],
    serialized_start=None,
    serialized_end=None,
)


# Create the Any message class using reflection
# (standard pattern for vendored protobufs without protoc)
Any = _reflection.GeneratedProtocolMessageType(
    'Any',
    (_message.Message,),
    {
        'DESCRIPTOR': _ANY_DESCRIPTOR,
        '__module__': 'custom_components.googlefindmy.ProtoDecoders.Any_pb2',
    }
)

_ANY_DESCRIPTOR._concrete_class = Any
