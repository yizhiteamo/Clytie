"""无参数构造、先启动后装配的插件入口协议。"""

from typing import Protocol

from .context import PluginContext


class Plugin(Protocol):
    def __init__(self) -> None: ...
    async def start(self, ctx: PluginContext) -> None: ...
    async def stop(self) -> None: ...
