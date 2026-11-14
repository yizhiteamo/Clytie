"""临时材料删除回执故障：保留事实，不把文件系统错误吞成成功。"""

import errno
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from clytie.backup.temporary import TemporaryArtifacts


def artifacts(tmp_path):
    manager = TemporaryArtifacts(tmp_path, uuid4().hex)
    root = manager.root / "verified"
    root.mkdir(parents=True)
    first, second = root / "first", root / "second"
    first.write_bytes(b"fictional first")
    second.write_bytes(b"fictional second")
    manager.begin()
    manager.register([root])
    return manager, first, second


def test_receipt_failure_after_unlink_records_progress_and_safe_reason(tmp_path, monkeypatch):
    manager, first, second = artifacts(tmp_path)
    original = manager._save
    failed = []

    def fail_once(state):
        if not failed and state["files"][str(first)]["state"] == "deleted":
            failed.append(True)
            raise PermissionError(errno.EACCES, "fictional private path must not be recorded")
        original(state)

    monkeypatch.setattr(manager, "_save", fail_once)
    assert manager.cleanup() == "cleanup_blocked"
    state = manager.state()
    assert not first.exists()
    assert second.exists()
    assert state["files"][str(first)]["state"] == "deleted"
    assert state["failures"] == [
        dict(
            code="temporary_io_error",
            stage="record_file_deleted",
            errno=errno.EACCES,
            winerror=None,
        )
    ]
    assert "fictional private path" not in manager.path.read_text(encoding="utf-8")
    # Recovery uses the durable deletion fact; it does not unlink the same path again.
    assert manager.cleanup() == "cleaned"
    assert manager.state()["failures"] == state["failures"]


@pytest.mark.skipif(os.name != "nt", reason="Windows receipt replacement sharing semantics")
def test_transient_receipt_sharing_retries_receipt_not_deletion(tmp_path, monkeypatch):
    manager, first, second = artifacts(tmp_path)
    original_replace, original_unlink = Path.replace, Path.unlink
    failed, deleted = [], []

    def replace(path, target):
        if Path(target) == manager.path and not failed:
            state = json.loads(path.read_bytes())
            if state["files"][str(first)]["state"] == "deleted":
                failed.append(True)
                error = PermissionError(errno.EACCES, "fictional sharing violation")
                error.winerror = 32
                raise error
        return original_replace(path, target)

    def unlink(path, *args, **kwargs):
        deleted.append(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    assert manager.cleanup() == "cleaned"
    assert failed == [True]
    assert deleted.count(first) == deleted.count(second) == 1


@pytest.mark.skipif(os.name != "nt", reason="Native Windows sharing handles")
def test_native_reader_blocks_deleted_receipt_replace_then_bounded_retry_succeeds(
    tmp_path, monkeypatch
):
    import ctypes
    from ctypes import wintypes

    manager, first, second = artifacts(tmp_path)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    original_save, original_replace = manager._save, Path.replace
    held, observed = [], []

    def release():
        if held:
            assert kernel.CloseHandle(held.pop())

    def save(state):
        if not observed and not held and state["files"][str(first)]["state"] == "deleted":
            # An actual reader permits read/write but not delete/replace sharing.
            handle = kernel.CreateFileW(str(manager.path), 1 << 31, 3, None, 3, 0x80, None)
            assert handle != wintypes.HANDLE(-1).value
            held.append(handle)
        original_save(state)

    def replace(path, target):
        try:
            return original_replace(path, target)
        except PermissionError as exc:
            if Path(target) == manager.path and held:
                observed.append(exc.winerror)
                release()
            raise

    monkeypatch.setattr(manager, "_save", save)
    monkeypatch.setattr(Path, "replace", replace)
    try:
        assert manager.cleanup() == "cleaned"
        assert len(observed) == 1 and observed[0] in {5, 32}
        assert not first.exists() and not second.exists()
    finally:
        release()


def test_unlink_error_and_identity_change_remain_distinct_from_receipt_failure(
    tmp_path, monkeypatch
):
    manager, first, second = artifacts(tmp_path)
    first.write_bytes(b"modified")
    assert manager.cleanup() == "cleanup_blocked"
    assert manager.state()["failures"][-1]["stage"] == "validate_file"
    assert manager.state()["failures"][-1]["code"] == "temporary_validation_failed"
    first.write_bytes(b"fictional first")
    original = Path.unlink

    def denied(path, *args, **kwargs):
        if path == first:
            raise PermissionError(errno.EACCES, "fictional secret")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    assert manager.cleanup() == "cleanup_blocked"
    assert manager.state()["failures"][-1]["stage"] == "delete_file"
    assert first.exists() and second.exists()


def test_receipt_store_failure_propagates_without_deleting(tmp_path, monkeypatch):
    manager, first, second = artifacts(tmp_path)

    def denied(state):
        raise OSError(errno.ENOSPC, "fictional full device")

    monkeypatch.setattr(manager, "_save", denied)
    with pytest.raises(OSError) as caught:
        manager.cleanup()
    assert caught.value.errno == errno.ENOSPC
    assert first.exists() and second.exists()
