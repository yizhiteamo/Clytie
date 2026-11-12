"""Instance-serialized backup workers independent of HTTP request lifetimes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from clytie.plugin_host.locking import OperationBusy, _os_lock, file_lock, process_identity
from clytie.plugin_host.snapshots import contained
from clytie.plugin_host.support import timed
from clytie.supervisor.processes import python_process

from .io_control import IoControl
from .journal import TERMINAL, BackupJournal


class WorkerError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def executor_alive(executor):
    if not executor:
        return False
    try:
        return process_identity(executor["pid"])["started_at"] == executor["started_at"]
    except (ProcessLookupError, FileNotFoundError):
        return False
    except OSError as exc:
        if getattr(exc, "winerror", None) == 87:
            return False
        return None
    except (KeyError, TypeError, ValueError):
        return None


def worker_locked(home):
    path = contained(home, Path(home) / "run/backup-worker.lock")
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        try:
            _os_lock(fd, True)
        except OSError:
            return True
        _os_lock(fd, False)
        return False
    finally:
        os.close(fd)


class BackupWorkers:
    def __init__(self, home, clock, *, timeout=120, startup_timeout=10, capture_handler=None):
        self.home, self.clock = Path(home).resolve(), clock
        self.timeout, self.startup_timeout = timeout, startup_timeout
        self.tasks, self.controls, self.journals, self.processes = {}, {}, {}, {}
        self.stopping, self.cancel_since, self._closing = False, None, None
        self._cancel_tasks = set()
        self._admissions = set()
        from .controller import CaptureBridge

        self.capture_handler = capture_handler or CaptureBridge()
        supervisor = getattr(self.capture_handler, "supervisor", None)
        self.io_control = supervisor.backup_io if supervisor is not None else IoControl(clock)

    @property
    def still_finishing(self):
        return (
            self.cancel_since is not None
            and bool(self.tasks)
            and self.clock.monotonic() - self.cancel_since >= self.timeout
        )

    def _gate(self):
        return file_lock(
            contained(self.home, self.home / "run/backup-admission.lock"),
            {**process_identity(), "operation": "backup_admission"},
        )

    def _paths(self):
        directory = contained(self.home, self.home / "supervisor/backup-operations")
        return sorted(directory.glob("*.json")) if directory.exists() else []

    def status(self):
        result = []
        for path in self._paths():
            try:
                result.append(BackupJournal.load(contained(self.home, path), self.clock).public())
            except (OSError, ValueError):
                result.append(
                    dict(id=path.stem, phase="recovery_blocked", reason="journal_unreadable")
                )
        return result

    def query(self, operation_id):
        return BackupJournal.load(
            BackupJournal.path_for(self.home, operation_id), self.clock
        ).public()

    def _pending(self):
        return [
            journal
            for path in self._paths()
            if (journal := BackupJournal.load(path, self.clock)).value["phase"] not in TERMINAL
        ]

    def _track(self, journal, *, recover_only=False):
        key = journal.value["id"]
        started = asyncio.get_running_loop().create_future()
        self.journals[key] = journal
        task = asyncio.create_task(self._run(journal, started, recover_only=recover_only))
        self.tasks[key] = task
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return started

    async def execute(self, operation, arguments, operation_id=None):
        task = asyncio.create_task(self._execute(operation, arguments, operation_id))
        self._admissions.add(task)
        task.add_done_callback(self._admissions.discard)
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.shield(task)

    async def _execute(self, operation, arguments, operation_id=None, *, shutdown=False):
        if self.stopping and not shutdown:
            raise WorkerError("supervisor_stopping")
        operation_id = operation_id or uuid4().hex
        path = BackupJournal.path_for(self.home, operation_id)
        if path.exists():
            journal = BackupJournal.load(path, self.clock)
            if journal.value["operation"] != operation:
                raise WorkerError("operation_mismatch")
            return journal.public()
        if (
            operation == "restore_apply"
            and isinstance(arguments, dict)
            and set(arguments) == {"plan_id"}
        ):
            from .previews import PreviewCatalog

            preview = await asyncio.to_thread(
                PreviewCatalog(self.home, self.clock).get, "instance", arguments["plan_id"]
            )
            if preview["state"] == "consumed":
                operation_id = preview["operation_id"]
        path = BackupJournal.path_for(self.home, operation_id)
        if path.exists():
            return BackupJournal.load(path, self.clock).public()
        if not isinstance(arguments, dict):
            raise WorkerError("arguments_invalid")
        allowed = {
            "restore_plan": {"source", "key_path", "target"},
            "restore_apply": {"plan_id"},
            "recover": {"plan_id"},
            "cleanup_plan": {"category", "old_id", "preserved"},
            "cleanup_apply": {"plan_id"},
        }
        valid_arguments = operation not in allowed or set(arguments) == allowed[operation]
        if operation == "cleanup_plan" and arguments.get("category") == "regular":
            valid_arguments = set(arguments) == {"category", "backup_ids"}
        if operation == "restore_plan" and set(arguments) == (
            allowed[operation] | {"source_repository", "source_repository_confirmed"}
        ):
            valid_arguments = True
        if not valid_arguments:
            raise WorkerError("arguments_invalid")
        try:
            with self._gate():
                if self.stopping and not shutdown:
                    raise WorkerError("supervisor_stopping")
                if worker_locked(self.home) or self._pending():
                    raise WorkerError("backup_busy")
                if operation == "restore_apply":
                    from .restore import RestoreManager

                    operation_id, _ = await asyncio.to_thread(
                        RestoreManager(self.home, self.clock).admit,
                        arguments["plan_id"],
                        operation_id,
                    )
                journal, fresh = BackupJournal.accept(
                    self.home, self.clock, operation_id, operation, arguments
                )
                if not fresh:
                    return journal.public()
                started = self._track(journal)
        except OperationBusy:
            raise WorkerError("backup_busy") from None
        try:
            await timed(asyncio.shield(started), self.clock, self.startup_timeout)
        except TimeoutError:
            # A missing transport acknowledgement is not proof of non-delivery.
            await self.cancel(operation_id)
        return self.query(operation_id)

    async def _run(self, journal, started, *, recover_only):
        key, process = journal.value["id"], None
        session, exit_confirmed = None, False
        bridges = set()

        async def send(reply):
            encoded = json.dumps(reply).encode() + b"\n"
            if len(encoded) > 64 * 1024:
                raise ValueError("capture_response_too_large")
            if process.stdin.is_closing():
                raise RuntimeError("backup_control_disconnected")
            process.stdin.write(encoded)
            await process.stdin.drain()

        async def bridge(response):
            reply = dict(type="bridge_reply", id=response.get("id"))
            try:
                if self.capture_handler is None:
                    raise ValueError("capture_bridge_unavailable")
                reply["result"] = await self.capture_handler(journal, response)
            except Exception:
                reply["error"] = "snapshot_unavailable"
            try:
                await send(reply)
            except (OSError, RuntimeError):
                self.io_control.disconnect(session)

        try:
            executable, env = python_process()
            env.pop("CLYTIE_HOME", None)
            kwargs = (
                {"creationflags": subprocess.DETACHED_PROCESS}
                if sys.platform == "win32"
                else {"start_new_session": True}
            )
            process = await asyncio.create_subprocess_exec(
                executable,
                "-m",
                "clytie.backup.worker",
                str(self.home),
                key,
                *(["recover-only"] if recover_only else []),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                limit=128 * 1024,
                **kwargs,
            )
            self.processes[key] = process
            session = self.io_control.register()
            self.io_control.authorize(session, key)

            def control():
                if process.returncode is None and not process.stdin.is_closing():
                    try:
                        process.stdin.write(b"cancel\n")
                    except (OSError, RuntimeError):
                        pass

            self.controls[key] = control
            if self.cancel_since is not None:
                control()
            while line := await process.stdout.readline():
                try:
                    response = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(response, dict):
                    continue
                if response.get("type") == "ready" and response.get("id") == key:
                    fresh = BackupJournal.load(journal.path, self.clock)
                    child = process_identity(process.pid)
                    if fresh.value.get("executor") != child:
                        control()
                        continue
                    process.stdin.write(b"start\n")
                    await process.stdin.drain()
                elif response.get("type") == "started" and response.get("id") == key:
                    fresh = BackupJournal.load(journal.path, self.clock)
                    if fresh.value.get("delivery") == "received" and not started.done():
                        started.set_result(True)
                elif response.get("type") == "bridge":
                    if len(bridges) >= 8:
                        await send(
                            dict(
                                type="bridge_reply",
                                id=response.get("id"),
                                error="snapshot_unavailable",
                            )
                        )
                        continue
                    task = asyncio.create_task(bridge(response))
                    bridges.add(task)
                    task.add_done_callback(bridges.discard)
                    task.add_done_callback(
                        lambda done: done.exception() if not done.cancelled() else None
                    )
                elif response.get("type") == "backup_io":
                    self.io_control.dispatch(session, response, send)
                # Result transport is only a notification. Durable journal is authority.
            self.io_control.disconnect(session)
            await process.wait()
            exit_confirmed = True
        except (OSError, ValueError, RuntimeError):
            if process is None:
                try:
                    fresh = BackupJournal.load(journal.path, self.clock)
                    if fresh.value["phase"] == "accepted":
                        fresh.phase("failed", reason="worker_not_created")
                except (OSError, ValueError):
                    pass
            elif process.returncode is None:
                # Keep tracking an executor even when its output channel failed.
                if not process.stdin.is_closing():
                    process.stdin.close()
                self.io_control.disconnect(session)
                await process.wait()
                exit_confirmed = True
        finally:
            if session is not None:
                self.io_control.disconnect(session)
                if exit_confirmed or (process is not None and process.returncode is not None):
                    await self.io_control.ended(session, confirmed=True)
            # Captures retain their own views until their actual completion, even
            # after the worker's pipe closes. Never pretend that cancelling this
            # receiver has released a companion's snapshot thread.
            await asyncio.gather(*tuple(bridges), return_exceptions=True)
            if exit_confirmed or (process is not None and process.returncode is not None):
                self.io_control.finish_operation(key)
            if process is not None and process.stdin is not None:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (OSError, RuntimeError):
                    pass
            if not started.done():
                started.set_result(False)
            self.controls.pop(key, None)
            self.processes.pop(key, None)
            self.tasks.pop(key, None)

    async def cancel(self, operation_id):
        path = BackupJournal.path_for(self.home, operation_id)
        callback = self.controls.get(operation_id)
        if callback:
            callback()
        for attempt in range(50):
            try:
                journal = BackupJournal.load(path, self.clock)
                journal.request_cancel()
                return journal.public()
            except OperationBusy:
                if attempt == 49:
                    raise WorkerError("backup_busy") from None
                await self.clock.sleep(0.01)

    def _track_external(self, journal):
        key = journal.value["id"]
        self.journals[key] = journal
        task = asyncio.create_task(self._watch_external(journal))
        self.tasks[key] = task
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

    async def _watch_external(self, journal):
        key = journal.value["id"]
        try:
            while True:
                current = BackupJournal.load(journal.path, self.clock)
                alive = executor_alive(current.value.get("executor"))
                if current.value["phase"] in TERMINAL and alive is False:
                    return
                if (
                    current.value["phase"] not in TERMINAL
                    and alive is False
                    and not worker_locked(self.home)
                ):
                    started = asyncio.get_running_loop().create_future()
                    await self._run(current, started, recover_only=True)
                    return
                await self.clock.sleep(0.05)
        finally:
            self.tasks.pop(key, None)

    async def recover_pending(self):
        for pending in self._pending():
            key = pending.value["id"]
            if key in self.tasks:
                continue
            alive = executor_alive(pending.value.get("executor"))
            if alive is not False or worker_locked(self.home):
                self._track_external(pending)
                continue
            try:
                with self._gate():
                    if worker_locked(self.home):
                        continue
                    current = BackupJournal.load(pending.path, self.clock)
                    if current.value["phase"] in TERMINAL:
                        continue
                    started = self._track(current, recover_only=True)
                try:
                    await timed(asyncio.shield(started), self.clock, self.startup_timeout)
                except TimeoutError:
                    await self.cancel(key)
                    continue
                task = self.tasks.get(key)
                if task:
                    await asyncio.shield(task)
            except OperationBusy:
                continue
        return self.status()

    def escalate(self):
        if not self.stopping or self.cancel_since is not None:
            return
        self.cancel_since = self.clock.monotonic()
        for callback in tuple(self.controls.values()):
            callback()
        for key in tuple(self.tasks):
            task = asyncio.create_task(self.cancel(key))
            self._cancel_tasks.add(task)
            task.add_done_callback(self._cancel_tasks.discard)
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

    async def stop(self, mode):
        self.stopping = True
        if mode == "cancel":
            self.escalate()
        if self._closing is None:
            self._closing = asyncio.create_task(self._drain())
        await asyncio.shield(self._closing)

    async def _drain(self):
        await asyncio.gather(*tuple(self._admissions), return_exceptions=True)
        await asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True)
        await asyncio.gather(*tuple(self._cancel_tasks), return_exceptions=True)
