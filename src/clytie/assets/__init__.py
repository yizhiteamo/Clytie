"""本体资产库；公开插件仅获得宿主绑定的 SDK 读写视图。"""

from .store import (
    AssetError,
    AssetRegistration,
    AssetSnapshot,
    AssetStore,
    ResolvedAsset,
    validate_registry,
)

__all__ = [
    "AssetError",
    "AssetRegistration",
    "AssetSnapshot",
    "AssetStore",
    "ResolvedAsset",
    "validate_registry",
]
