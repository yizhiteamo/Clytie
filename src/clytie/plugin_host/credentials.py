"""按宿主配置授权读取凭据；不缓存内容，不暴露路径或底层异常。"""

import asyncio
import os
from pathlib import Path, PureWindowsPath

from clytie.sdk import CredentialUnavailable

from .snapshots import contained


class Credentials:
    def __init__(self, home, plugin_id, declarations):
        self.home = Path(home)
        self.plugin_id = plugin_id
        self.declarations = declarations

    async def read(self, ref):
        entry = self.declarations.get(ref) if isinstance(ref, str) else None
        if entry is None:
            raise CredentialUnavailable("unknown_ref")
        if self.plugin_id not in entry["plugins"]:
            raise CredentialUnavailable("not_permitted")
        return await asyncio.to_thread(self._read, entry["secret"])

    def _read(self, relative):
        try:
            root = contained(self.home, self.home / "secrets")
            if PureWindowsPath(relative).drive or Path(relative).is_absolute():
                raise ValueError("invalid credential path")
            path = contained(root, root / relative.replace("\\", "/"))
            before = path.stat()
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(path, flags), "r", encoding="utf-8") as stream:
                opened = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("credential changed")
                value = stream.read().strip()
                contained(root, path)
        except FileNotFoundError:
            raise CredentialUnavailable("missing") from None
        except (OSError, ValueError, RuntimeError):
            raise CredentialUnavailable("unreadable") from None
        if not value:
            raise CredentialUnavailable("empty")
        return value


class ChannelCredentials(Credentials):
    """平台单元只读取宿主绑定账号的密钥，绑定在生命周期内固定。"""

    def __init__(self, home, plugin_id, declarations, account=None):
        super().__init__(home, plugin_id, declarations)
        self._account_bound = account is not None
        self._account_secret = account.get("secret") if account is not None else None

    async def account_secret(self):
        if not self._account_bound:
            raise CredentialUnavailable("unknown_ref")
        if self._account_secret is None:
            raise CredentialUnavailable("missing")
        return await asyncio.to_thread(self._read, self._account_secret)
