"""Companion-owned temporary files. Closing the handle also removes the file."""

import asyncio
import base64
import binascii
import hashlib
import re
import tempfile
from dataclasses import dataclass
from typing import BinaryIO
from uuid import uuid4

from clytie.api.auth import APIError

from .snapshots import contained, identifier
from .support import finish_cleanup

CHUNK_BYTES = 768 * 1024


@dataclass
class Upload:
    plugin: str
    size: int
    sha256: str
    file: BinaryIO
    expires: float
    received: int = 0
    sequence: int = 0
    complete: bool = False
    timer: asyncio.Task | None = None


class Uploads:
    def __init__(self, home, cid, clock):
        self.directory = contained(home, home / "run" / "tmp" / "uploads" / identifier(cid))
        self.clock = clock
        self.entries = {}
        self.lock = asyncio.Lock()
        self.closed = False

    def _drop(self, key):
        item = self.entries.pop(key)
        if item.timer and item.timer is not asyncio.current_task():
            item.timer.cancel()
        item.file.close()

    def _lookup(self, key):
        item = self.entries.get(key)
        if item and self.clock.monotonic() >= item.expires:
            self._drop(key)
            item = None
        if not item:
            raise APIError("upload_unavailable", 404)
        return item

    async def _expire(self, key, deadline):
        await self.clock.sleep(max(0, deadline - self.clock.monotonic()))
        async with self.lock:
            if key in self.entries:
                self._drop(key)

    async def start(self, plugin, size, sha256, maximum):
        if type(size) is not int or size < 0 or not isinstance(sha256, str):
            raise APIError("invalid_request")
        if not re.fullmatch("[0-9a-f]{64}", sha256):
            raise APIError("invalid_request")
        if size > maximum:
            raise APIError("upload_too_large", 413)
        async with self.lock:
            if self.closed:
                raise APIError("upload_unavailable", 404)
            for key, item in tuple(self.entries.items()):
                if self.clock.monotonic() >= item.expires:
                    self._drop(key)
            if sum(not item.complete for item in self.entries.values()) >= 2:
                raise APIError("upload_limit", 409)
            self.directory.mkdir(parents=True, exist_ok=True)
            file = tempfile.TemporaryFile(dir=self.directory)
            key = uuid4().hex
            item = self.entries[key] = Upload(
                plugin, size, sha256, file, self.clock.monotonic() + 600
            )
            item.timer = asyncio.create_task(self._expire(key, item.expires))
            return {"upload_id": key}

    async def chunk(self, key, sequence, content):
        try:
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise APIError("invalid_request") from None
        if len(raw) > CHUNK_BYTES:
            raise APIError("upload_too_large", 413)
        async with self.lock:
            item = self._lookup(key)
            if item.complete or str(item.sequence) != sequence:
                raise APIError("upload_sequence", 409)
            if not raw or item.received + len(raw) > item.size:
                raise APIError("upload_size_mismatch")
            try:
                written, cancelled = await finish_cleanup(asyncio.to_thread(item.file.write, raw))
                if written != len(raw):
                    raise OSError("short write")
            except OSError:
                self._drop(key)
                raise
            item.received += written
            item.sequence += 1
            if cancelled:
                raise asyncio.CancelledError
            return {}

    async def complete(self, key):
        async with self.lock:
            item = self._lookup(key)
            if item.received != item.size:
                raise APIError("upload_size_mismatch", 409)
            if not item.complete:

                def digest():
                    item.file.seek(0)
                    checksum = hashlib.sha256()
                    for block in iter(lambda: item.file.read(CHUNK_BYTES), b""):
                        checksum.update(block)
                    return checksum.hexdigest()

                actual, cancelled = await finish_cleanup(asyncio.to_thread(digest))
                if actual != item.sha256:
                    self._drop(key)
                    raise APIError("upload_digest_mismatch", 409)
                item.complete = True
                if cancelled:
                    raise asyncio.CancelledError
            return {"upload_id": key}

    async def access(self, plugin, key, *, release):
        async with self.lock:
            try:
                item = self._lookup(key)
            except (APIError, TypeError):
                raise KeyError("upload_unavailable") from None
            if not item.complete or plugin != item.plugin:
                raise KeyError("upload_unavailable")
            if release:
                self._drop(key)
                return None

            def read():
                item.file.seek(0)
                return item.file.read()

            data, cancelled = await finish_cleanup(asyncio.to_thread(read))
            if cancelled:
                raise asyncio.CancelledError
            return data

    async def close(self):
        async with self.lock:
            self.closed = True
            for key in tuple(self.entries):
                self._drop(key)


class UploadView:
    def __init__(self, uploads, plugin):
        self.uploads = uploads
        self.plugin = plugin

    async def read(self, upload_id):
        return await self.uploads.access(self.plugin, upload_id, release=False)

    async def release(self, upload_id):
        await self.uploads.access(self.plugin, upload_id, release=True)
