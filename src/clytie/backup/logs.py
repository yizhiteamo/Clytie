"""单条写入受锁保护的日志分卷；当前文件没有跨调用保存的写句柄。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from clytie.plugin_host.snapshots import atomic_read, atomic_write, canonical

_registry_guard = threading.Lock()
_thread_locks = {}


def _contained(root, path):
    # A resolved root plus lexical descendants with no links needs no handle-based
    # resolution of an actively locked leaf (unreliable on Windows).
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(root):
        raise ValueError("log_path_outside_home")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        ):
            raise ValueError("log_path_link")
        if part == root:
            break
    return path


@contextmanager
def _log_lock(path):
    key = os.path.abspath(path)
    if sys.platform == "win32":
        key = key.casefold()
    with _registry_guard:
        lock = _thread_locks.setdefault(key, threading.Lock())
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
        os.set_inheritable(descriptor, False)
        try:
            if sys.platform == "win32":
                import ctypes
                import msvcrt
                from ctypes import wintypes

                class Overlapped(ctypes.Structure):
                    _fields_ = [
                        ("Internal", ctypes.c_size_t),
                        ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD),
                        ("OffsetHigh", wintypes.DWORD),
                        ("hEvent", wintypes.HANDLE),
                    ]

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.LockFileEx.argtypes = [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(Overlapped),
                ]
                kernel.UnlockFileEx.argtypes = [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(Overlapped),
                ]
                overlap = Overlapped()
                handle = msvcrt.get_osfhandle(descriptor)
                if not kernel.LockFileEx(handle, 2, 0, 1, 0, ctypes.byref(overlap)):
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    yield
                finally:
                    if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlap)):
                        raise ctypes.WinError(ctypes.get_last_error())
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _identity(path):
    value = path.stat()
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
    }


class RotatingLogWriter:
    """每个调用原子串行；普通日志按字节分片，审计记录保持完整且永不删除。"""

    def __init__(self, home, relative_path, *, rotate_bytes=10 * 1024**2, archives=10, audit=False):
        if type(rotate_bytes) is not int or rotate_bytes < 1:
            raise ValueError("invalid_log_rotation_size")
        if type(archives) is not int or archives < 1:
            raise ValueError("invalid_log_archive_count")
        self.home = Path(home).resolve()
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid_log_path")
        self.path = _contained(self.home, self.home / relative)
        self.index_path = self.path.with_name(self.path.name + ".rotation.json")
        self.lock_path = self.path.with_name(self.path.name + ".rotation.lock")
        self.rotate_bytes, self.archives, self.audit = rotate_bytes, archives, audit

    def _load(self):
        _contained(self.home, self.path)
        _contained(self.home, self.index_path)
        if not self.index_path.exists():
            return {"schema_version": 1, "archives": [], "audit": self.audit}
        value = json.loads(atomic_read(self.index_path))
        if value.get("schema_version") != 1 or value.get("audit") != self.audit:
            raise ValueError("log_index_incompatible")
        for row in value["archives"]:
            path = _contained(self.home, self.home / row["path"])
            if path.parent != self.path.parent or not path.name.startswith(
                self.path.name + ".archive."
            ):
                raise ValueError("log_index_path_mismatch")
        if value.get("restore_index"):
            if not self.audit:
                raise ValueError("log_index_incompatible")
            # Imported index contains logical names and authenticated content hashes,
            # never usable source-machine file identities.
            restored = []
            for row in value["archives"]:
                path = _contained(self.home, self.home / row["path"])
                before = _identity(path)
                hasher = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        hasher.update(chunk)
                if (
                    before != _identity(path)
                    or before["size"] != row["size"]
                    or hasher.hexdigest() != row["sha256"]
                ):
                    raise ValueError("audit_restore_changed")
                restored.append(dict(path=row["path"], **before))
            value["archives"] = restored
            value.pop("restore_index")
            self._save(value)
        return value

    def _save(self, index):
        atomic_write(self.index_path, canonical(index), retry_sharing=True)

    def _rotate(self, index):
        archive = self.path.with_name(self.path.name + ".archive." + uuid4().hex)
        _contained(self.home, archive)
        if archive.exists():
            raise FileExistsError("log_archive_collision")
        self.path.rename(archive)
        index["archives"].append(
            {"path": archive.relative_to(self.home).as_posix(), **_identity(archive)}
        )
        self._save(index)
        self._prune(index)

    def _prune(self, index):
        if not self.audit:
            while len(index["archives"]) > self.archives:
                row = index["archives"][0]
                old = _contained(self.home, self.home / row["path"])
                if index.get("deleting") == row["path"] and not old.exists():
                    index["archives"].pop(0)
                    index.pop("deleting")
                    self._save(index)
                    continue
                if _identity(old) != {
                    key: row[key] for key in ("device", "inode", "size", "mtime_ns")
                }:
                    raise ValueError("log_archive_identity_changed")
                index["deleting"] = row["path"]
                self._save(index)
                old.unlink()  # One owned, explicitly identified archive; never the current file.
                index["archives"].pop(0)
                index.pop("deleting")
                self._save(index)

    def write(self, data):
        if not isinstance(data, bytes):
            raise TypeError("log_writer_requires_bytes")
        if not data:
            return 0
        _contained(self.home, self.lock_path)
        with _log_lock(self.lock_path):
            index = self._load()
            self._prune(index)
            offset = 0
            while offset < len(data):
                size = self.path.stat().st_size if self.path.exists() else 0
                if size and (
                    size >= self.rotate_bytes
                    or (self.audit and size + len(data) > self.rotate_bytes)
                ):
                    self._rotate(index)
                    size = 0
                amount = (
                    len(data) if self.audit else min(len(data) - offset, self.rotate_bytes - size)
                )
                with self.path.open("ab") as stream:
                    stream.write(memoryview(data)[offset : offset + amount])
                    stream.flush()
                    os.fsync(stream.fileno())
                offset += amount
            return len(data)

    def status(self):
        _contained(self.home, self.lock_path)
        with _log_lock(self.lock_path):
            index = self._load()
            known = {row["path"] for row in index["archives"]}
            untracked = [
                path
                for path in self.path.parent.iterdir()
                if path.name.startswith(self.path.name + ".archive.")
                and path.relative_to(self.home).as_posix() not in known
            ]
            current_size = self.path.stat().st_size if self.path.exists() else 0
            return {
                **index,
                "current_bytes": current_size,
                "archive_bytes": sum(row["size"] for row in index["archives"]),
                "untracked_archives": len(untracked),
            }

    def seal(self):
        """固定审计边界，只改名并登记；返回卷身份，复制/内容验证在锁外进行。"""
        if not self.audit:
            raise ValueError("only_audit_logs_can_be_sealed")
        _contained(self.home, self.lock_path)
        with _log_lock(self.lock_path):
            index = self._load()
            known = {row["path"] for row in index["archives"]}
            if any(
                path.name.startswith(self.path.name + ".archive.")
                and path.relative_to(self.home).as_posix() not in known
                for path in self.path.parent.iterdir()
            ):
                raise ValueError("audit_untracked_archive")
            if self.path.exists() and self.path.stat().st_size:
                self._rotate(index)
            return tuple(dict(row) for row in index["archives"])


def copy_log_pipe(stream, writer, *, chunk_size=64 * 1024):
    """同步排空专用日志管道至 EOF；调用者管理非 daemon 执行者并等待返回。

    不读取控制 stdin、不关闭进程、不吞掉写入异常。执行者尚未结束且管道
    没有 EOF 时不能宣称排空完成；禁止调用者将此函数直接放在事件循环内。
    """
    if chunk_size < 1:
        raise ValueError("invalid_log_chunk_size")
    total = 0
    read = getattr(stream, "read1", stream.read)
    while True:
        data = read(chunk_size)
        if not data:
            return total
        total += writer.write(data)


@contextmanager
def process_logs(writer, *, stdout=False):
    """进程自持 fd 日志管道；恢复写端后等 EOF，后台启动者可独立退出。"""
    saved, readers, threads, errors = [], [], [], []

    def drain(stream):
        try:
            copy_log_pipe(stream, writer)
        except BaseException as exc:
            errors.append(exc)
            # Keep draining so a writer failure cannot deadlock process shutdown.
            while stream.read(64 * 1024):
                pass
        finally:
            stream.close()

    try:
        for descriptor in [1, 2] if stdout else [2]:
            stream = sys.stdout if descriptor == 1 else sys.stderr
            stream.flush()
            original = os.dup(descriptor)
            saved.append((descriptor, original, stream))
            read_fd, write_fd = os.pipe()
            reader = os.fdopen(read_fd, "rb", buffering=0)
            readers.append(reader)
            thread = threading.Thread(target=drain, args=(reader,), daemon=False)
            threads.append(thread)
            thread.start()
            try:
                os.dup2(write_fd, descriptor)
            finally:
                os.close(write_fd)
        yield
    finally:
        for descriptor, original, stream in reversed(saved):
            try:
                stream.flush()
            finally:
                os.dup2(original, descriptor)
                os.close(original)
        for thread in threads:
            thread.join()
        if errors:
            raise errors[0]
