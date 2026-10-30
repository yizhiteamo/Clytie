"""OS 进程适配器；管道帧与退出由上层时钟驱动。"""

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path


class Lines:
    def __init__(self, stream):
        self.queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        descriptor = stream.fileno()

        def pump():
            pending = b""
            try:
                while True:
                    chunk = os.read(descriptor, 4096)
                    if not chunk:
                        break
                    pending += chunk
                    if len(pending) > 1024 * 1024:
                        break
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        try:
                            value = json.loads(line)
                            if (
                                not isinstance(value, dict)
                                or "type" not in value
                                or not isinstance(value.get("seq"), int)
                            ):
                                continue
                        except (ValueError, UnicodeError):
                            continue
                        loop.call_soon_threadsafe(self.queue.put_nowait, value)
            except (OSError, RuntimeError):
                pass
            finally:
                try:
                    loop.call_soon_threadsafe(self.queue.put_nowait, None)
                except RuntimeError:
                    pass

        threading.Thread(target=pump, daemon=True).start()

    async def receive(self):
        return await self.queue.get()


def frame(stream, message):
    data = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode()
    stream.write(data)
    stream.flush()


class Job:
    """Windows 关闭句柄时结束所属伴侣；Unix 依赖管道 EOF。"""

    def __init__(self):
        self.handle = None
        if sys.platform != "win32":
            return
        import ctypes as c
        from ctypes import wintypes as w

        class Basic(c.Structure):
            _fields_ = [
                ("process_time", c.c_longlong),
                ("job_time", c.c_longlong),
                ("flags", w.DWORD),
                ("min_ws", c.c_size_t),
                ("max_ws", c.c_size_t),
                ("active", w.DWORD),
                ("affinity", c.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class IO(c.Structure):
            _fields_ = [
                (name, c.c_ulonglong)
                for name in (
                    "read_ops",
                    "write_ops",
                    "other_ops",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class Extended(c.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", IO),
                ("process_memory", c.c_size_t),
                ("job_memory", c.c_size_t),
                ("peak_process", c.c_size_t),
                ("peak_job", c.c_size_t),
            ]

        self.api = c.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000
        if not self.handle or not self.api.SetInformationJobObject(
            self.handle, 9, c.byref(limits), c.sizeof(limits)
        ):
            self.close()
            raise c.WinError(c.get_last_error())

    def assign(self, process):
        if self.handle and not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            import ctypes

            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def creation(*, detached=False):
    if sys.platform == "win32":
        return {
            "creationflags": subprocess.DETACHED_PROCESS
            if detached
            else subprocess.CREATE_NEW_PROCESS_GROUP
        }
    return {"start_new_session": True}


def python_process():
    """Target the interpreter, not the Windows venv redirector."""
    executable = sys.executable
    env = os.environ.copy()
    base = getattr(sys, "_base_executable", executable)
    if sys.platform == "win32" and base != executable:
        env["__PYVENV_LAUNCHER__"] = executable
        executable = base
    return executable, env


class Child:
    def __init__(self, process, log):
        self.process, self.log = process, log
        self.lines = Lines(process.stdout)
        self.waiter = asyncio.create_task(asyncio.to_thread(process.wait))

    async def send(self, message):
        frame(self.process.stdin, message)

    async def receive(self):
        return await self.lines.receive()

    async def wait(self):
        return await asyncio.shield(self.waiter)

    async def kill(self):
        if self.process.poll() is None:
            self.process.kill()
        await self.wait()

    def close(self):
        for stream in (self.process.stdin, self.process.stdout, self.log):
            if stream:
                stream.close()


class ProcessLauncher:
    def __init__(self, home, *, job=None):
        self.home = Path(home)
        self.job = job or Job()
        self.children = []

    async def launch(self, cid):
        directory = self.home / "companions" / cid / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        log = (directory / "process.log").open("ab")
        executable, env = python_process()
        process = subprocess.Popen(
            [executable, "-m", "clytie", "companion", "run", cid, "--home", str(self.home)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            env=env,
            **creation(),
        )
        try:
            self.job.assign(process)
            child = Child(process, log)
            self.children.append(child)
            await child.send({"type": "boot", "seq": 0})
            return child
        except BaseException:
            process.kill()
            process.wait()
            log.close()
            raise

    def close(self):
        self.job.close()
        for child in self.children:
            child.close()


class ProcessBackend:
    def __init__(self, clock):
        self.clock = clock

    async def run(self, args, *, timeout_s, env=None, max_output_bytes=10 * 1024 * 1024):
        from clytie.plugin_host.support import timed
        from clytie.sdk.types import ProcessResult

        if not isinstance(args, list) or not args or not all(isinstance(v, str) for v in args):
            raise ValueError("args must be a nonempty string list")
        if timeout_s <= 0 or max_output_bytes < 0:
            raise ValueError("invalid subprocess budget")
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **(env or {})},
            **kwargs,
        )
        budget = [max_output_bytes]
        truncated = [False]

        async def collect(stream):
            chunks = []
            while True:
                data = await stream.read(4096)
                if not data:
                    return b"".join(chunks)
                take = min(budget[0], len(data))
                chunks.append(data[:take])
                budget[0] -= take
                truncated[0] |= take < len(data)

        stdout, stderr = (
            asyncio.create_task(collect(process.stdout)),
            asyncio.create_task(collect(process.stderr)),
        )
        try:
            await timed(process.wait(), self.clock, timeout_s)
            return ProcessResult(
                returncode=process.returncode,
                stdout=await stdout,
                stderr=await stderr,
                truncated=truncated[0],
            )
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await asyncio.gather(stdout, stderr, return_exceptions=True)
