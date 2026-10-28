"""插件入口边界；启动和注册方法留待宿主规格明确。"""

from typing import Protocol


class Plugin(Protocol):
    """A1 不发明加载器调用方法；由清单 entry 定位入口。"""
