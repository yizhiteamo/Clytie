"""上下文基础协议；可选权限属性由 A2 宿主按授权动态提供。"""

from random import Random
from typing import Protocol

from .clock import Clock
from .types import JsonObject


class PluginContext(Protocol):
    @property
    def plugin_id(self) -> str: ...

    @property
    def companion_id(self) -> str: ...

    @property
    def config(self) -> JsonObject: ...

    @property
    def log(self) -> object: ...

    @property
    def clock(self) -> Clock: ...

    @property
    def random(self) -> Random: ...

    @property
    def storage(self) -> object: ...

    @property
    def events(self) -> object: ...
