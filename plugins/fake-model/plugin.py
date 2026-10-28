"""只返回虚构固定文本的离线参考模型。"""

from clytie.sdk.testing import FakeModel


class Plugin(FakeModel):
    """复用 SDK 假模型；不读取凭据，不调用外部进程或网络。"""
