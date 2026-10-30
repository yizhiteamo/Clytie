"""前端、守护进程与伴侣进程入口；管理操作委托内部服务。"""

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from clytie.clock import SystemClock
from clytie.config import load_home, thaw
from clytie.plugin_host.locking import OperationBusy, _os_lock, _owner
from clytie.plugin_host.support import timed

from .common import read
from .companion import Companion, StartupBlocked
from .processes import Lines, ProcessBackend, ProcessLauncher, creation, frame
from .service import Supervisor


def status(home):
    home = Path(home)
    path = home / "run/supervisor.lock"
    running = False
    operation = None
    if path.exists():
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            try:
                _os_lock(fd, True)
            except OSError:
                running = True
                operation = _owner(fd).get("operation")
            else:
                _os_lock(fd, False)
        finally:
            os.close(fd)
    value = read(home / "run/health.json", {})
    if running and operation in {"stopping", "supervisor"}:
        value["state"] = "stopping" if operation == "stopping" else "running"
    return dict(running=running, stale=not running, health=value)


async def companion_main(home, cid):
    clock = SystemClock()
    lines = Lines(sys.stdin.buffer)
    first = await lines.receive()
    if first is None:
        return 0
    runtime = Companion(home, cid, clock)
    protocol = sys.stdout.buffer
    sys.stdout = sys.stderr
    seq = 0

    def emit(kind, **fields):
        nonlocal seq
        seq += 1
        frame(protocol, dict(type=kind, seq=seq, **fields))

    try:
        await runtime.start()
    except StartupBlocked as exc:
        emit("fatal", reason=exc.reason)
        runtime.runtime.close()
        return 1
    except Exception:
        logging.exception("companion.start_failed")
        runtime.runtime.close()
        return 1
    emit("ready")

    async def heartbeat():
        while True:
            emit("heartbeat", **runtime.health())
            await clock.sleep(runtime.config.get("heartbeat_seconds", 5))

    heartbeat_task = asyncio.create_task(heartbeat())
    stopping = None
    try:
        while True:
            read_task = asyncio.create_task(lines.receive())
            if stopping:
                done, _ = await asyncio.wait(
                    {read_task, stopping}, return_when=asyncio.FIRST_COMPLETED
                )
                if stopping in done:
                    read_task.cancel()
                    await asyncio.gather(read_task, return_exceptions=True)
                    emit("stop_result", result=stopping.result())
                    return 0
            msg = await read_task
            if msg is None:
                runtime.session_ending()
                try:
                    await timed(runtime.stop("cancel", orphan=True), clock, 5)
                except BaseException:
                    pass
                # No event-loop shutdown wait for plugins that ignore cancellation.
                protocol.flush()
                os._exit(0)
            kind = msg["type"]
            if kind == "stop" and stopping is None:
                stopping = asyncio.create_task(runtime.stop(msg.get("task_mode", "cancel")))
            elif kind == "escalate":
                runtime.escalated.set()
            elif kind == "session_ending":
                runtime.session_ending()
                runtime.escalated.set()
            elif kind == "silence":
                runtime.silence(msg["on"])
                frame(protocol, dict(type="silence_ack", seq=msg["seq"]))
            else:
                runtime.log.warning("companion.unknown_type")
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


async def daemon_main(home):
    clock = SystemClock()
    report = load_home(home)
    if report.global_errors or not report.instance:
        frame(
            sys.stdout.buffer,
            dict(
                type="error",
                seq=0,
                code="config_invalid",
                fields=[e.pointer for e in report.global_errors],
            ),
        )
        return 1
    launcher = ProcessLauncher(home)
    instance = thaw(report.instance)
    supervisor = Supervisor(
        home, instance["companions"], launcher, clock, config=instance.get("supervisor", {})
    )
    log_seq = 0

    def display(entry):
        nonlocal log_seq
        log_seq += 1
        try:
            frame(sys.stdout.buffer, dict(type="log", seq=log_seq, entry=entry))
        except (OSError, BrokenPipeError):
            pass

    supervisor.audit.on_entry = display
    from clytie.config import repository_root
    from clytie.plugin_host import PluginHost
    from clytie.plugin_host.instance import InstanceNotifiers

    notifiers = InstanceNotifiers(
        PluginHost(repository_root(), home, clock=clock), subprocess_backend=ProcessBackend(clock)
    )
    supervisor.notifications.cooldown = instance.get("notify", {}).get("cooldown_hours", 6) * 3600
    lines = Lines(sys.stdin.buffer)
    loop = asyncio.get_running_loop()

    def ending(*_):
        loop.call_soon_threadsafe(lines.queue.put_nowait, dict(type="session_ending", seq=0))

    signal.signal(signal.SIGTERM, ending)
    from .signals import SessionEvents

    session_events = SessionEvents(ending)
    try:
        await supervisor.start()
    except OperationBusy:
        frame(sys.stdout.buffer, dict(type="error", seq=0, code="already_running"))
        session_events.close()
        launcher.close()
        return 1
    await notifiers.start(
        instance.get("notify", {}).get("plugins", []), instance.get("plugins", {})
    )
    supervisor.notifications.plugins = notifiers.notifiers
    supervisor.finalize = notifiers.close
    frame(sys.stdout.buffer, dict(type="ready", seq=0))
    stopping = None
    try:
        while True:
            task = asyncio.create_task(lines.receive())
            if stopping:
                done, _ = await asyncio.wait({task, stopping}, return_when=asyncio.FIRST_COMPLETED)
                if stopping in done:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    try:
                        frame(
                            sys.stdout.buffer,
                            dict(type="stop_result", seq=1, result=stopping.result()),
                        )
                    except (OSError, BrokenPipeError):
                        pass
                    return 0
            msg = await task
            if msg is None or msg["type"] == "stop":
                if stopping is None:
                    stopping = asyncio.create_task(supervisor.stop("cancel"))
                if msg is None:
                    await stopping
                    return 0
            elif msg["type"] == "session_ending":
                ending_stop = asyncio.create_task(supervisor.session_ending())
                if stopping is None:
                    stopping = ending_stop
            elif msg["type"] == "escalate":
                await supervisor.escalate()
            else:
                supervisor.log.warning("supervisor.frontend_unknown_type")
    finally:
        session_events.close()
        await notifiers.close()
        launcher.close()


def frontend(home):
    # Redirected Windows stdout may use an ANSI codepage unable to encode this notice.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("关闭这个窗口会安全停止所有伴侣", flush=True)
    clock = SystemClock()
    while status(home)["running"]:
        if status(home)["health"].get("state") != "stopping":
            print("已在运行", flush=True)
            return 1
        print("上一次还在安全停止中，请稍候", flush=True)
        asyncio.run(clock.sleep(1))
    directory = home / "logs/supervisor"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "process.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "clytie", "_supervisor", "--home", str(home)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            **creation(detached=True),
        )
        count = 0

        def stop(*_, ending=False):
            nonlocal count
            count += 1
            try:
                frame(
                    process.stdin,
                    dict(
                        type="session_ending" if ending else "stop" if count == 1 else "escalate",
                        seq=count,
                    ),
                )
            except (OSError, BrokenPipeError):
                pass

        signal.signal(signal.SIGINT, stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, stop)
        signal.signal(signal.SIGTERM, lambda *_: stop(ending=True))
        handler = None
        if sys.platform == "win32":
            import ctypes

            callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)

            @callback
            def handler(event):
                stop(ending=event in {5, 6})
                return True

            ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
        try:
            for line in process.stdout:
                # Only protocol status, never child plugin stdout or content.
                print(line.decode("utf-8").rstrip(), flush=True)
            return process.wait()
        finally:
            process.stdin.close()
            process.stdout.close()
            if handler:
                ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, False)


def internal(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--home", required=True)
    if argv[0] == "companion":
        parser.add_argument("verb", choices=["run"])
        parser.add_argument("cid")
    args = parser.parse_args(argv[1:])
    home = Path(args.home).resolve()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    if argv[0] == "companion":
        return asyncio.run(companion_main(home, args.cid))
    return asyncio.run(daemon_main(home))


def run(args):
    from clytie.api import local_supervisor_start, local_supervisor_status

    if args.action == "start":
        return local_supervisor_start(args.home)
    import json

    value = local_supervisor_status(args.home)
    if args.json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print("运行中" if value["running"] else "未运行（可能过时）")
        print(json.dumps(value["health"], ensure_ascii=False))
    return 0
