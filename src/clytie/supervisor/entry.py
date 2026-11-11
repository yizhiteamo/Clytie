"""前端、守护进程与伴侣进程入口；管理操作委托内部服务。"""

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
from collections import deque
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


class _SilenceApplications:
    """静默按收到顺序应用；控制元数据有界，确认只在真实应用之后发送。"""

    def __init__(self, runtime, acknowledge, stopping):
        self.runtime, self.acknowledge, self.stopping = runtime, acknowledge, stopping
        self.pending = deque()
        self.task = None
        self.applying = self.closed = False

    def submit(self, message):
        if self.closed or self.stopping() or len(self.pending) + int(self.applying) >= 64:
            self.runtime.log.warning("companion.silence_busy")
            return
        self.pending.append((message["seq"], message["on"]))
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._apply())
            self.runtime.management_tasks.add(self.task)
            self.task.add_done_callback(self.runtime.management_tasks.discard)

    def close(self):
        self.closed = True
        self.pending.clear()

    async def _apply(self):
        while self.pending and not self.closed and not self.stopping():
            seq, on = self.pending.popleft()
            self.applying = True
            try:
                await self.runtime.silence(on)
            except Exception as exc:
                self.runtime.log.warning("companion.silence_failed", exc_type=type(exc).__name__)
            else:
                self.acknowledge(seq)
            finally:
                self.applying = False
        self.pending.clear()

    async def finish(self):
        self.close()
        if self.task is not None:
            await asyncio.shield(self.task)


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
            await runtime.refresh_health()
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
    requests = runtime.management_requests

    def silence_ack(seq):
        with output_lock:
            frame(protocol, dict(type="silence_ack", seq=seq))

    silence = _SilenceApplications(
        runtime, silence_ack, lambda: stopping is not None or stop_requested.is_set()
    )

    async def handle_request(msg):
        from clytie.api.auth import APIError
        from clytie.api.operations import companion_operation

        try:
            if stopping or stop_requested.is_set():
                raise APIError("companion_busy", 409)
            value = await companion_operation(runtime, msg["op"], msg["args"])
            emit("response", id=msg["id"], ok=True, result=value)
        except APIError as exc:
            emit("response", id=msg["id"], ok=False, error={"code": exc.code, "status": exc.status})
        except Exception:
            emit(
                "response",
                id=msg["id"],
                ok=False,
                error={"code": "operation_failed", "status": 400},
            )

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
            if kind == "request":
                task = asyncio.create_task(handle_request(msg))
                requests.add(task)
                task.add_done_callback(requests.discard)
            elif kind == "stop" and stopping is None:
                silence.close()
                heartbeat.update(phase="stopping")
                stopping = asyncio.create_task(runtime.stop(msg.get("task_mode", "cancel")))
            elif kind == "management_cancel":
                journal = runtime.management_journals.get(msg.get("operation_id"))
                if journal:
                    journal.cancel()
            elif kind == "escalate":
                runtime.escalated.set()
                runtime.cancel_management()
            elif kind == "session_ending":
                silence.close()
                runtime.session_ending()
                runtime.escalated.set()
            elif kind == "silence":
                silence.submit(msg)
            else:
                runtime.log.warning("companion.unknown_type")
    finally:
        await silence.finish()
        for task in requests:
            task.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
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


class StartupHandshake:
    """A short-lived launch pipe: EOF cancels only before API acknowledgement."""

    def __init__(self, loop, queue):
        self.loop, self.queue = loop, queue
        self.ready = threading.Event()
        self.cancelled = threading.Event()
        self.confirmed = False

    def observe(self, message):
        if self.confirmed:
            return
        if message is None:
            self.cancelled.set()
            self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
        elif message.get("type") == "startup_confirmed" and self.ready.is_set():
            self.confirmed = True


async def daemon_main(home, detached=False, maintenance=False):
    clock = SystemClock()
    from clytie.backup.restore import RestoreError, assert_start_allowed

    try:
        assert_start_allowed(home, maintenance=maintenance)
    except (RestoreError, OSError) as exc:
        frame(
            sys.stdout.buffer,
            dict(
                type="error",
                seq=0,
                code=exc.code if isinstance(exc, RestoreError) else "restore_recovery_blocked",
            ),
        )
        return 1
    report = load_home(home)
    if not maintenance and (report.global_errors or not report.instance):
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
    instance = thaw(report.instance) if report.instance else {}
    supervisor = Supervisor(
        home,
        [] if maintenance else instance["companions"],
        launcher,
        clock,
        config={} if maintenance else instance.get("supervisor", {}),
    )
    supervisor.maintenance = maintenance
    supervisor.old_instance_options = instance.get("backup", {}).get("old_instances", {})
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

    notifiers = (
        None
        if maintenance
        else InstanceNotifiers(
            PluginHost(repository_root(), home, clock=clock),
            subprocess_backend=ProcessBackend(clock),
        )
    )
    supervisor.notifications.cooldown = instance.get("notify", {}).get("cooldown_hours", 6) * 3600
    detached_events = asyncio.Queue()
    loop = asyncio.get_running_loop()
    handshake = StartupHandshake(loop, detached_events)
    lines = Lines(sys.stdin.buffer, handshake.observe if detached else None)
    supervisor.startup_cancelled = handshake.cancelled.is_set

    def ending(*_):
        handshake.cancelled.set()
        loop.call_soon_threadsafe(
            (detached_events if detached else lines.queue).put_nowait,
            dict(type="session_ending", seq=0),
        )

    signal.signal(signal.SIGTERM, ending)
    from .signals import SessionEvents

    session_events = SessionEvents(ending)

    from clytie.api.auth import APIError
    from clytie.api.management import Management
    from clytie.api.server import Server

    management = Management(supervisor, report)
    server = None
    control = FrontendCommands(supervisor)
    startup_complete = asyncio.Event()

    def stop_requested(mode):
        supervisor.stopping = True
        management.memory_workers.stopping = True
        if control.stopping is None:

            async def stop_when_ready():
                await startup_complete.wait()
                return await supervisor.stop(mode, source="internal")

            control.stopping = asyncio.create_task(stop_when_ready())
        queue = detached_events if detached else lines.queue
        queue.put_nowait(dict(type="api_stop", seq=0))

    supervisor.request_stop = stop_requested

    async def prepare():
        nonlocal server
        server = Server(home, clock, management, supervisor.audit, thaw(instance.get("api", {})))
        try:
            management.api_port = supervisor.api_port = await server.start()
            handshake.ready.set()
        except (OSError, APIError):
            if detached:
                raise APIError("api_bind_failed", 503) from None
            supervisor.problems["api_unavailable:instance"] = "管理接口未启动：api_bind_failed"
            frame(sys.stdout.buffer, dict(type="warning", seq=0, code="api_bind_failed"))
        if not server.auth.admin and not detached:
            frame(
                sys.stdout.buffer,
                dict(
                    type="notice",
                    seq=0,
                    message="还没有设置管理口令：网页登录不可用，危险操作没有口令保护。运行 clytie admin set-password 设置",
                ),
            )
        if notifiers is not None:
            await notifiers.start(
                instance.get("notify", {}).get("plugins", []), instance.get("plugins", {})
            )
            supervisor.notifications.plugins = notifiers.notifiers
            supervisor.finalize = notifiers.close

    try:
        await supervisor.start(prepare)
    except (OperationBusy, APIError) as exc:
        if control.stopping is not None:
            control.stopping.cancel()
            await asyncio.gather(control.stopping, return_exceptions=True)
        if server:
            await server.close()
        frame(
            sys.stdout.buffer,
            dict(
                type="error",
                seq=0,
                code="already_running" if isinstance(exc, OperationBusy) else exc.code,
            ),
        )
        session_events.close()
        launcher.close()
        return 1
    startup_complete.set()
    from clytie.backup.controller import BackupController

    supervisor.backup_controller = BackupController(supervisor, management.backup_workers, instance)
    if not supervisor.stopping:
        await supervisor.backup_controller.start()
    frame(sys.stdout.buffer, dict(type="ready", seq=0))

    async def reminders():
        while True:
            if supervisor.silenced and not detached:
                info = read(home / "supervisor/silence.json", {})
                frame(
                    sys.stdout.buffer,
                    dict(
                        type="notice",
                        seq=0,
                        message="全部静默中（自" + info.get("since", "") + "起）",
                    ),
                )
            await clock.sleep(60)

    reminder = asyncio.create_task(reminders())
    try:
        while True:
            task = asyncio.create_task(detached_events.get() if detached else lines.receive())
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
            message = await task
            if message and message.get("type") == "api_stop":
                continue
            if await control.receive(message):
                return 0
    finally:
        reminder.cancel()
        await asyncio.gather(reminder, return_exceptions=True)
        if server:
            await server.close()
        session_events.close()
        if notifiers is not None:
            await notifiers.close()
        launcher.close()


def frontend(home, *, maintenance=False):
    # Redirected Windows stdout may use an ANSI codepage unable to encode this notice.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(
        "这是调试模式窗口。命令行操作会直接作用于伴侣的数据，请确认了解每条命令的作用后再执行。关闭这个窗口会安全停止所有伴侣。",
        flush=True,
    )
    clock = SystemClock()
    while status(home)["running"]:
        if status(home)["health"].get("state") != "stopping":
            print("已在运行", flush=True)
            return 1
        print("上一次还在安全停止中，请稍候", flush=True)
        asyncio.run(clock.sleep(1))
    directory = home / "logs/supervisor"
    directory.mkdir(parents=True, exist_ok=True)
    executable, env = python_process()
    process = subprocess.Popen(
        [executable, "-m", "clytie", "_supervisor", "--home", str(home)]
        + (["--maintenance"] if maintenance else []),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
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
    parser.add_argument("--detach", action="store_true")
    parser.add_argument("--maintenance", action="store_true")
    if argv[0] == "companion":
        parser.add_argument("verb", choices=["run"])
        parser.add_argument("cid")
    args = parser.parse_args(argv[1:])
    home = Path(args.home).resolve()
    from clytie.backup.logs import RotatingLogWriter, process_logs
    from clytie.plugin_host.snapshots import identifier

    companion = argv[0] == "companion"
    relative = (
        f"companions/{identifier(args.cid)}/logs/process.log"
        if companion
        else "logs/supervisor/process.log"
    )
    writer = RotatingLogWriter(home, relative)
    with process_logs(writer, stdout=not companion and args.detach):
        report = load_home(home)
        options = (report.instance or {}).get("backup", {}).get("logs", {})
        writer.rotate_bytes = options.get("rotate_mib", 10) * 1024**2
        writer.archives = options.get("archives", 10)
        logging.basicConfig(stream=sys.stderr, level=logging.INFO)
        if companion:
            return asyncio.run(companion_main(home, args.cid))
        return asyncio.run(daemon_main(home, detached=args.detach, maintenance=args.maintenance))


def run(args):
    from clytie.api import local_supervisor_start, local_supervisor_status

    if args.action == "start":
        return local_supervisor_start(args.home, maintenance=getattr(args, "maintenance", False))
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
