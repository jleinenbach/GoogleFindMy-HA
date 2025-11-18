from __future__ import annotations

from typing import Any

from google.protobuf.message import Message

__all__ = ["MessageToDict", "MessageToJson"]

def MessageToDict(
    message: Message,
    *,
    including_default_value_fields: bool = ...,
    preserving_proto_field_name: bool = ...,
    use_integers_for_enums: bool = ...,
    descriptor_pool: Any | None = ...,
    float_precision: int | None = ...,
    always_print_fields_with_no_presence: bool = ...,
) -> dict[str, Any]: ...

def MessageToJson(
    message: Message,
    *,
    including_default_value_fields: bool = ...,
    preserving_proto_field_name: bool = ...,
    use_integers_for_enums: bool = ...,
    indent: int | None = ...,
    descriptor_pool: Any | None = ...,
    float_precision: int | None = ...,
    always_print_fields_with_no_presence: bool = ...,
) -> str: ...
