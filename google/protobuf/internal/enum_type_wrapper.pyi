# google/protobuf/internal/enum_type_wrapper.pyi
from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, TypeVar

from custom_components.googlefindmy.protobuf_typing import EnumTypeWrapperMeta

_EnumT = TypeVar("_EnumT", bound=int)


class EnumTypeWrapper(EnumTypeWrapperMeta[_EnumT]):
    DESCRIPTOR: Any

    def __call__(self, value: int, *args: Any, **kwargs: Any) -> _EnumT: ...

    def Name(self, number: int) -> str: ...

    def Value(self, name: str) -> _EnumT: ...

    def keys(self) -> Iterable[str]: ...

    def values(self) -> Iterable[_EnumT]: ...

    def items(self) -> Iterable[tuple[str, _EnumT]]: ...

    def __iter__(self) -> Iterator[_EnumT]: ...

    def __getattr__(self, name: str) -> _EnumT: ...


_EnumTypeWrapper = EnumTypeWrapper
