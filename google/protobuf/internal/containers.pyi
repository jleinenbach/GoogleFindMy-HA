# google/protobuf/internal/containers.pyi
from __future__ import annotations

from typing import (
    Any as _Any,
    Generic as _Generic,
    Iterable as _Iterable,
    Iterator as _Iterator,
    MutableSequence as _MutableSequence,
    TypeVar as _TypeVar,
    overload as _overload,
)

_T = _TypeVar("_T")


class _BaseRepeatedContainer(_MutableSequence[_T], _Generic[_T]):
    """Sequence interface for protobuf repeated fields."""

    def __init__(self, iterable: _Iterable[_T] = ...) -> None: ...
    @_overload
    def __getitem__(self, index: int) -> _T: ...

    @_overload
    def __getitem__(self, index: slice) -> _MutableSequence[_T]: ...

    @_overload
    def __setitem__(self, index: int, value: _T) -> None: ...

    @_overload
    def __setitem__(self, index: slice, value: _Iterable[_T]) -> None: ...

    @_overload
    def __delitem__(self, index: int) -> None: ...

    @_overload
    def __delitem__(self, index: slice) -> None: ...
    def append(self, value: _T) -> None: ...
    def extend(self, values: _Iterable[_T]) -> None: ...
    def insert(self, index: int, value: _T) -> None: ...
    def remove(self, value: _T) -> None: ...
    def pop(self, index: int = ...) -> _T: ...
    def __iter__(self) -> _Iterator[_T]: ...
    def __len__(self) -> int: ...


class RepeatedCompositeFieldContainer(_BaseRepeatedContainer[_T], _Generic[_T]):
    def add(self, **kwargs: _Any) -> _T: ...


class RepeatedScalarFieldContainer(_BaseRepeatedContainer[_T], _Generic[_T]):
    ...


__all__ = [
    "RepeatedCompositeFieldContainer",
    "RepeatedScalarFieldContainer",
]
