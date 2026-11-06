"""Issue #61: readiness reads, bounded by the original startup deadline."""

import asyncio
import errno
import io
import json
import shutil
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from clytie.api import client as module
from clytie.clock import SystemClock
from clytie.sdk.testing import MockClock
from clytie.sdk.types import ApiResponse
from clytie.supervisor.common import read, write
from clytie.supervisor.entry import status


class Pipe(io.BytesIO):
    def close(self):
        if not self.closed:
            self.sent = self.getvalue()
        super().close()


def startup(tmp_path, monkeypatch, reader):
    pipe = Pipe()
    process = SimpleNamespace(pid=42, stdin=pipe, returncode=None)
    process.poll = lambda: process.returncode
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(module, "status", lambda home: {"running": False})
    monkeypatch.setattr(module, "read", reader)
    write(tmp_path / "run/api.json", {"pid": process.pid, "port": 8765})
    clock = MockClock()
    return process, clock, asyncio.create_task(module.detached_start(tmp_path, clock))


async def test_read_denial_then_readiness_sends_one_ack(tmp_path, monkeypatch):
    calls = []

    def denied_once(path, default=None):
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError(errno.EACCES, "fictional denial")
        return read(path, default)

    requests = []

    async def ready(self, method, path):
        requests.append((method, path))
        return ApiResponse(status=200, json={"ok": True})

    monkeypatch.setattr(module.Client, "request", ready)
    process, clock, task = startup(tmp_path, monkeypatch, denied_once)
    await clock.drain()
    assert not task.done()
    assert not requests and not process.stdin.getvalue()
    clock.advance(0.1)
    await clock.drain()
    assert (await task)["ok"]
    assert len(calls) == 2 and requests == [("GET", "/api/v1/status")]
    assert json.loads(process.stdin.sent)["type"] == "startup_confirmed"
    assert process.stdin.closed


async def test_persistent_read_denial_times_out_without_reset_or_ack(tmp_path, monkeypatch):
    calls = []

    def denied(path, default=None):
        calls.append(path)
        raise PermissionError(errno.EACCES, "fictional permanent denial")

    process, clock, task = startup(tmp_path, monkeypatch, denied)
    await clock.drain()
    clock.advance(29.9)
    await clock.drain()
    assert not task.done() and len(calls) == 2
    clock.advance(0.1)
    await clock.drain()
    result = await task
    assert result["error"]["code"] == "startup_timeout"
    assert "状态文件不可读" in result["error"]["message"]
    assert "fictional permanent denial" not in json.dumps(result)
    assert len(calls) == 2 and clock.monotonic() == 30
    assert process.stdin.closed and not process.stdin.sent


async def test_read_denial_does_not_extend_remaining_http_budget(tmp_path, monkeypatch):
    calls = 0
    entered, cancelled = asyncio.Event(), asyncio.Event()

    def denied_once(path, default=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError(errno.EACCES, "fictional denial")
        return read(path, default)

    async def slow(self, method, path):
        entered.set()
        try:
            await clock.sleep(100)
        finally:
            cancelled.set()

    monkeypatch.setattr(module.Client, "request", slow)
    process, clock, task = startup(tmp_path, monkeypatch, denied_once)
    await clock.drain()
    clock.advance(29.9)
    await clock.drain()
    assert entered.is_set()
    clock.advance(0.1)
    await clock.drain()
    assert (await task)["error"]["code"] == "startup_timeout"
    assert cancelled.is_set() and not process.stdin.sent and process.stdin.closed


@pytest.mark.parametrize("number", [errno.EIO, errno.ENOTDIR])
async def test_other_read_io_errors_fail_explicitly_without_retry(tmp_path, monkeypatch, number):
    calls = []

    def broken(path, default=None):
        calls.append(path)
        raise OSError(number, "fictional I/O failure")

    process, clock, task = startup(tmp_path, monkeypatch, broken)
    await clock.drain()
    result = await task
    assert result["error"]["code"] == "startup_failed"
    assert "状态文件" in result["error"]["message"]
    assert "fictional I/O failure" not in json.dumps(result)
    assert len(calls) == 1 and clock.monotonic() == 0
    assert process.stdin.closed and not process.stdin.sent


@pytest.mark.parametrize("finish", ["exit", "cancel"])
async def test_read_denial_preserves_exit_and_cancellation_cleanup(tmp_path, monkeypatch, finish):
    def denied(path, default=None):
        raise PermissionError(errno.EACCES, "fictional denial")

    process, clock, task = startup(tmp_path, monkeypatch, denied)
    await clock.drain()
    assert not task.done()
    if finish == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        process.returncode = 1
        clock.advance(0.1)
        await clock.drain()
        assert (await task)["error"]["code"] == "startup_failed"
    assert process.stdin.closed and not process.stdin.sent


@contextmanager
def deny_windows_sharing(path):
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_read = 1 << 31
    handle = api.CreateFileW(str(path), generic_read, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        api.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing semantics")
async def test_real_windows_read_denial_then_release_without_permission_changes(tmp_path):
    from clytie.api.client import _await_start

    path = tmp_path / "run/api.json"
    write(path, {"pid": 42, "port": 8765})
    clock, pipe = MockClock(), Pipe()

    async def ready(*args):
        return ApiResponse(status=200, json={"ok": True})

    process = SimpleNamespace(pid=42, poll=lambda: None, stdin=pipe)
    client = SimpleNamespace(home=tmp_path, request=ready)
    try:
        with deny_windows_sharing(path):
            # Real kernel sharing denial, not a raised mock exception. Python's
            # path read reports EACCES; that alone cannot identify CI's cause.
            with pytest.raises(PermissionError) as caught:
                read(path)
            assert caught.value.errno == errno.EACCES
            task = asyncio.create_task(_await_start(process, client, clock))
            await clock.drain()
            assert not task.done() and not pipe.getvalue()
        clock.advance(0.1)
        await clock.drain()
        assert (await task)["ok"]
        assert json.loads(pipe.getvalue())["type"] == "startup_confirmed"
    finally:
        pipe.close()


@pytest.mark.parametrize("failure", ["denied", "io_error", "cancel"])
async def test_real_read_failure_closes_pipe_and_safely_exits(tmp_path, monkeypatch, failure):
    """Inject only the parent's read; run the real daemon and fake plugins unchanged."""
    home = tmp_path / "fictional-home"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples/home", home)
    config_path = home / "clytie.local.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    # Use an approved stop budget shorter than the OS observation bound. A forced
    # stop is still a test failure, not a way to make the exit assertion pass.
    config["supervisor"] = {"stop": {"timeout_seconds": 10}}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        config["api"] = {"port": sock.getsockname()[1]}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    target = home / "run/api.json"
    injected = []

    def failed_read(path, default=None):
        if path == target and target.exists():
            injected.append(path)
            if failure == "io_error":
                raise OSError(errno.EIO, "fictional read failure")
            raise PermissionError(errno.EACCES, "fictional read denial")
        return read(path, default)

    processes = []
    popen = subprocess.Popen

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module, "read", failed_read)
    monkeypatch.setattr(subprocess, "Popen", launch)
    clock, os_clock = MockClock(), SystemClock()
    task = asyncio.create_task(module.detached_start(home, clock))
    try:
        # OS startup is real; business time stays frozen until publication.
        end = os_clock.monotonic() + 30
        while not target.exists() and not task.done() and os_clock.monotonic() < end:
            await os_clock.sleep(0.05)
        assert target.exists(), "real daemon did not publish readiness"
        clock.advance(0.1)
        await clock.drain()
        assert injected
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if failure == "denied":
                assert not task.done()
                clock.advance(29.9)
                await clock.drain()
            result = await task
            expected = "startup_timeout" if failure == "denied" else "startup_failed"
            assert not result["ok"] and result["error"]["code"] == expected
        assert len(processes) == 1 and processes[0].stdin.closed
        # Closing the unconfirmed launch pipe must drive the real safe-stop path.
        try:
            assert await asyncio.to_thread(processes[0].wait, timeout=30) == 0
        except (subprocess.TimeoutExpired, AssertionError):
            print("fictional daemon health:", read(home / "run/health.json", {}))
            for log in (
                home / "logs/supervisor/process.log",
                home / "companions/companion_a/logs/process.log",
            ):
                if log.exists():
                    print(log.relative_to(home), log.read_text(encoding="utf-8", errors="replace"))
            raise
        assert not status(home)["running"]
        audit = [
            json.loads(line) for line in (home / "supervisor/audit.jsonl").read_bytes().splitlines()
        ]
        assert any(row["operation"] == "stop" for row in audit), audit
        assert not any(row["operation"] == "force_stop" for row in audit), audit
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for process in processes:
            if process.poll() is None:
                process.stdin.close()
                try:
                    await asyncio.to_thread(process.wait, timeout=30)
                except subprocess.TimeoutExpired:
                    # Test failure cleanup targets only this test's known process.
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=10)
