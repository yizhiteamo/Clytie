"""不可变的 JSON 容器；序列化后仍是标准数组和对象。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Generic, TypeVar, get_args

from pydantic_core import core_schema

T = TypeVar("T")


def freeze(value: Any) -> Any:
    """复制并冻结嵌套容器，不保留调用者的可变引用。"""
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return FrozenList(value)
    return value


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


class FrozenList(tuple, Generic[T]):
    """以 tuple 为底层的只读序列，不存在 list 基类修改绕过。"""

    __slots__ = ()

    def __new__(cls, values=()):
        return tuple.__new__(cls, (freeze(value) for value in values))

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        args = get_args(source_type)
        item_schema = handler.generate_schema(args[0]) if args else core_schema.any_schema()
        schema = core_schema.list_schema(item_schema)
        return core_schema.no_info_after_validator_function(
            cls,
            schema,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda value: [thaw(item) for item in value], return_schema=schema
            ),
        )


class FrozenDict(tuple, Mapping[str, T], Generic[T]):
    """键固定为字符串的只读映射；底层只保存不可变键值对。"""

    __slots__ = ()

    def __new__(cls, values=()):
        pairs = values.items() if isinstance(values, Mapping) else values
        return tuple.__new__(cls, ((key, freeze(value)) for key, value in pairs))

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in tuple.__iter__(self))

    def __getitem__(self, key: str) -> T:
        for candidate, value in tuple.__iter__(self):
            if candidate == key:
                return value
        raise KeyError(key)

    def items(self):
        return tuple.__iter__(self)

    def __contains__(self, key):
        return any(candidate == key for candidate, _ in tuple.__iter__(self))

    def __eq__(self, other):
        if isinstance(other, Mapping):
            return dict(self.items()) == dict(other.items())
        return False

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(frozenset(tuple.__iter__(self)))

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        args = get_args(source_type)
        value_schema = handler.generate_schema(args[0]) if args else core_schema.any_schema()
        schema = core_schema.dict_schema(core_schema.str_schema(), value_schema)
        return core_schema.no_info_after_validator_function(
            cls,
            schema,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda value: {key: thaw(item) for key, item in value.items()},
                return_schema=schema,
            ),
        )
