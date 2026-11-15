"""Clytie 插件的唯一公共依赖边界。"""

from .backup import BackupEntry, BackupProvider, BackupRequest, BackupSink, BackupTicket, BackupView
from .cancellation import Cancelled, CancelToken
from .channel import ChannelUnavailable
from .clock import Clock
from .context import CredentialUnavailable, PluginContext, WebSocketClosed
from .plugin import PluginUnavailable
from .types import QUOTA_UNKNOWN, ChannelState

__version__ = "0.11.0"
__all__ = [
    "BackupEntry",
    "BackupProvider",
    "BackupRequest",
    "BackupSink",
    "BackupTicket",
    "BackupView",
    "PluginUnavailable",
    "ChannelUnavailable",
    "ChannelState",
    "QUOTA_UNKNOWN",
    "CancelToken",
    "Cancelled",
    "Clock",
    "PluginContext",
    "CredentialUnavailable",
    "WebSocketClosed",
    "__version__",
]
