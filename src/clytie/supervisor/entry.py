"""前端、守护进程与伴侣进程入口；管理操作委托内部服务。"""

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from clytie.clock import SystemClock
from clytie.config import load_home, thaw
from clytie.plugin_host.locking import OperationBusy, _os_lock, _owner
from clytie.plugin_host.support import timed

from .common import read
from .companion import Companion, StartupBlocked
from .processes import Lines, ProcessBackend, ProcessLauncher, creation, frame, python_process
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


async def orphan_exit(runtime, clock, protocol):
    runtime.session_ending()
    try:
        await timed(runtime.stop("cancel", orphan=True), clock, 5)
    except BaseException:
        pass
    # Do not let event-loop shutdown await a plugin that ignores cancellation.
    protocol.flush()
    os._exit(0)


async def companion_main(home, cid):
    from .heartbeat import Heartbeat

    clock = SystemClock()
    protocol = sys.stdout.buffer
    sys.stdout = sys.stderr
    output_lock = threading.Lock()
    seq = 0

    def emit(kind, **fields):
        nonlocal seq
        with output_lock:
            seq += 1
            frame(protocol, dict(type=kind, seq=seq, **fields))

    heartbeat = Heartbeat(clock, emit)
    stop_requested, orphan = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    startup = None

    def observed(message):
        if message is None or message.get("type") in {"stop", "session_ending"}:
            stop_requested.set()
            if message is None:
                orphan.set()
            if startup and not startup.done() and heartbeat.phase != "migrating":
                loop.call_soon_threadsafe(startup.cancel)

    lines = Lines(sys.stdin.buffer, observed)
    first = await lines.receive()
    if first is None:
        return 0
    runtime = Companion(home, cid, clock)
    runtime.on_phase = lambda phase: heartbeat.update(phase=phase)
    runtime.stop_requested = stop_requested.is_set
    heartbeat.start()

    async def refresh():
        while True:
            heartbeat.update(
                runtime.health(),
                interval=runtime.config.get("heartbeat_seconds", 5),
                hang=runtime.config.get("hang_seconds", 30),
            )
            await clock.sleep(runtime.config.get("heartbeat_seconds", 5))

    heartbeat_task = asyncio.create_task(refresh())
    startup = asyncio.create_task(runtime.start())
    try:
        started = await startup
    except asyncio.CancelledError:
        started = False
    except StartupBlocked as exc:
        emit("fatal", reason=exc.reason)
        heartbeat.close()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        await runtime.stop()
        return 1
    except Exception:
        logging.exception("companion.start_failed")
        heartbeat.close()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        await runtime.stop()
        return 1
    if not started or stop_requested.is_set():
        heartbeat.update(phase="stopping")
        try:
            if orphan.is_set():
                await orphan_exit(runtime, clock, protocol)
            result = await runtime.stop("cancel", orphan=orphan.is_set())
            if not orphan.is_set():
                emit("stop_result", result=result)
        finally:
            heartbeat.close()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        return 0
    emit("ready")
    heartbeat.update(runtime.health(), phase="running")
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
                await orphan_exit(runtime, clock, protocol)
            kind = msg["type"]
            if kind == "stop" and stopping is None:
                heartbeat.update(phase="stopping")
                stopping = asyncio.create_task(runtime.stop(msg.get("task_mode", "cancel")))
            elif kind == "escalate":
                runtime.escalated.set()
            elif kind == "session_ending":
                runtime.session_ending()
                runtime.escalated.set()
            elif kind == "silence":
                runtime.silence(msg["on"])
                with output_lock:
                    frame(protocol, dict(type="silence_ack", seq=msg["seq"]))
            else:
                runtime.log.warning("companion.unknown_type")
    finally:
        heartbeat.close()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


class FrontendSignals:
    """Console adapters share a serialized pipe; close is never an interrupt toggle."""

    def __init__(self, stream):
        self.stream, self.seq, self.interrupts = stream, 0, 0
        self.lock = threading.Lock()

    def send(self, source):
        with self.lock:
            self.seq += 1
            if source == "interrupt":
                self.interrupts += 1
            kind = (
                "session_ending"
                if source == "session_ending"
                else "escalate"
                if source == "interrupt" and self.interrupts > 1
                else "stop"
            )
            try:
                frame(self.stream, dict(type=kind, seq=self.seq, source=source))
            except (OSError, BrokenPipeError):
                pass

    def interrupt(self, *_):
        self.send("interrupt")

    def windows(self, event):
        if event not in {0, 1, 2, 5, 6}:
            return False
        self.send(
            "interrupt" if event in {0, 1} else "session_ending" if event in {5, 6} else "close"
        )
        return True


class FrontendCommands:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.stopping = None

    async def receive(self, message):
        svc = self.supervisor
        if message is None or message["type"] == "stop":
            interrupt = message is not None and message.get("source") == "interrupt"
            if self.stopping is None:
                self.stopping = asyncio.create_task(
                    svc.stop(None if interrupt else "cancel", source="terminal")
                )
            elif not interrupt:
                await svc.escalate()
            if message is None:
                await self.stopping
                return True
        elif message["type"] == "session_ending":
            # session_ending also escalates an already-running wait-mode stop.
            self.stopping = asyncio.create_task(svc.session_ending(source="terminal"))
        elif message["type"] == "escalate":
            await svc.escalate()
        else:
            svc.log.warning("supervisor.frontend_unknown_type")
        return False


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

    async def prepare():
        await notifiers.start(
            instance.get("notify", {}).get("plugins", []), instance.get("plugins", {})
        )
        supervisor.notifications.plugins = notifiers.notifiers
        supervisor.finalize = notifiers.close

    try:
        await supervisor.start(prepare)
    except OperationBusy:
        frame(sys.stdout.buffer, dict(type="error", seq=0, code="already_running"))
        session_events.close()
        launcher.close()
        return 1
    frame(sys.stdout.buffer, dict(type="ready", seq=0))
    control = FrontendCommands(supervisor)
    try:
        while True:
            task = asyncio.create_task(lines.receive())
            if control.stopping:
                done, _ = await asyncio.wait(
                    {task, control.stopping}, return_when=asyncio.FIRST_COMPLETED
                )
                if control.stopping in done:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    try:
                        frame(
                            sys.stdout.buffer,
                            dict(type="stop_result", seq=1, result=control.stopping.result()),
                        )
                    except (OSError, BrokenPipeError):
                        pass
                    return 0
            if await control.receive(await task):
                return 0
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
        executable, env = python_process()
        process = subprocess.Popen(
            [executable, "-m", "clytie", "_supervisor", "--home", str(home)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            env=env,
            **creation(detached=True),
        )
        controls = FrontendSignals(process.stdin)
        signal.signal(signal.SIGINT, controls.interrupt)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, controls.interrupt)
        signal.signal(signal.SIGTERM, lambda *_: controls.send("session_ending"))
        handler = None
        if sys.platform == "win32":
            import ctypes

            callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)

            @callback
            def handler(event):
                return controls.windows(event)

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
        health = value["health"]
        print(f"{health.get('undelivered', 0)} 条通知未送达")
        for cid, row in health.get("companions", {}).items():
            print(f"{cid}: {row.get('state', 'unknown')}")
            print(
                f"还有 {row.get('sending', 0)} 条消息正在发送 / "
                f"{row.get('inflight_tasks', 0)} 个任务在执行"
            )
            if row.get("silence_status") == "pending_migrating":
                print("静默待确认（迁移中，尚未连接平台）")
            if row.get("migration_stalled"):
                print("迁移暂时无心跳（startup_stalled，等待中，不强制终止）")
        print(json.dumps(health, ensure_ascii=False))
    return 0
