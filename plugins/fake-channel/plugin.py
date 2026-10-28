"""只收集内存中意图的离线参考平台。"""

from clytie.sdk.testing import FakeChannel


class Plugin(FakeChannel):
    """复用 SDK 假平台；所有事件均由测试显式注入。"""
