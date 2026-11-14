"""强杀后的 OS 资源释放是独立前提；只测试临时文件。"""

import errno
import os
import threading

import pytest

from clytie.backup import restore
from clytie.plugin_host import locking

from . import test_backup_assets_crash as crash


def test_kill_lock_probe_observes_contention_without_modifying_metadata(tmp_path, monkeypatch):
    target = tmp_path / "target"
    path = restore._target_lock(target)
    original = b"\0fictional lock metadata"
    path.write_bytes(original)
    calls = []
    real_lock = locking._os_lock

    def delayed_release(fd, acquire):
        calls.append(acquire)
        if acquire and calls.count(True) <= 2:
            raise OSError(errno.EACCES, "fictional delayed OS unlock")
        return real_lock(fd, acquire)

    monkeypatch.setattr(locking, "_os_lock", delayed_release)
    observed = crash.wait_for_restore_lock_release(target)
    assert observed == [errno.EACCES, errno.EACCES]
    assert calls == [True, True, True, False]
    assert path.read_bytes() == original


def test_kill_lock_probe_does_not_hide_io_failure(tmp_path, monkeypatch):
    target = tmp_path / "target"
    restore._target_lock(target).write_bytes(b"\0fictional")

    def broken(fd, acquire):
        raise OSError(errno.EIO, "fictional I/O failure")

    monkeypatch.setattr(locking, "_os_lock", broken)
    with pytest.raises(OSError) as caught:
        crash.wait_for_restore_lock_release(target)
    assert caught.value.errno == errno.EIO


def test_real_os_lock_keeps_restore_blocked_until_release(tmp_path, monkeypatch):
    target = tmp_path / "target"
    path = restore._target_lock(target)
    path.write_bytes(b"\0fictional")
    fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    locking._os_lock(fd, True)
    observed = threading.Event()
    released = threading.Event()
    real_lock = locking._os_lock

    def probe(descriptor, acquire):
        try:
            return real_lock(descriptor, acquire)
        except OSError:
            observed.set()
            raise

    monkeypatch.setattr(locking, "_os_lock", probe)

    def release_after_busy():
        assert observed.wait(5)
        real_lock(fd, False)
        released.set()

    release = threading.Thread(target=release_after_busy)
    release.start()
    try:
        assert crash.wait_for_restore_lock_release(target)
    finally:
        release.join(5)
        if not released.is_set():
            real_lock(fd, False)
        os.close(fd)
    assert not release.is_alive()
    assert path.read_bytes() == b"\0fictional"


def test_kill_lock_probe_has_a_bound_and_never_bypasses_a_held_lock(tmp_path):
    target = tmp_path / "target"
    path = restore._target_lock(target)
    path.write_bytes(b"\0fictional")
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    locking._os_lock(descriptor, True)
    try:
        with pytest.raises(pytest.fail.Exception, match="not released"):
            crash.wait_for_restore_lock_release(target, attempts=3)
        with pytest.raises(OSError):
            probe = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
            try:
                locking._os_lock(probe, True)
            finally:
                os.close(probe)
    finally:
        locking._os_lock(descriptor, False)
        os.close(descriptor)
    assert path.read_bytes() == b"\0fictional"
