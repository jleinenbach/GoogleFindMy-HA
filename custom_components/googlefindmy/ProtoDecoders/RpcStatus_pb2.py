# custom_components/googlefindmy/ProtoDecoders/RpcStatus_pb2.py
#
# Vendored google.rpc.Status definition (Python).
# Manually implemented to avoid dependency on googleapis-common-protos.
#
# Reference: https://github.com/googleapis/googleapis/blob/master/google/rpc/status.proto
"""Vendored google.rpc.Status for parsing Google API error responses."""
from __future__ import annotations

from custom_components.googlefindmy.ProtoDecoders.Any_pb2 import (
    _ANY_DESCRIPTOR,
)
from custom_components.googlefindmy.ProtoDecoders.Any_pb2 import (
    Any as _Any,
)
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection

_STATUS_DESCRIPTOR = _descriptor.Descriptor(
    name='Status',
    full_name='google.rpc.Status',
    filename=None,
    file=None,
    containing_type=None,
    create_key=None,
    fields=[
        _descriptor.FieldDescriptor(
            name='code',
            full_name='google.rpc.Status.code',
            index=0,
            number=1,
            type=5,  # TYPE_INT32
            cpp_type=1,
            label=1,  # LABEL_OPTIONAL
            has_default_value=False,
            default_value=0,
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
            name='message',
            full_name='google.rpc.Status.message',
            index=1,
            number=2,
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
            name='details',
            full_name='google.rpc.Status.details',
            index=2,
            number=3,
            type=11,  # TYPE_MESSAGE
            cpp_type=10,
            label=3,  # LABEL_REPEATED
            has_default_value=False,
            default_value=[],
            message_type=_ANY_DESCRIPTOR,
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


# Register the message type
Status = _reflection.GeneratedProtocolMessageType(
    'Status',
    (_message.Message,),
    {
        'DESCRIPTOR': _STATUS_DESCRIPTOR,
        '__module__': 'custom_components.googlefindmy.ProtoDecoders.RpcStatus_pb2',
    }
)

_STATUS_DESCRIPTOR._concrete_class = Status

# Re-export Any for convenience
Any = _Any
