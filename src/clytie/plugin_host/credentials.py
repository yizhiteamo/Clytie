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
