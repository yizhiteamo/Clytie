"""守护拥有在线捕获路由；管道仅传递稳定元数据文件的身份。"""

import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from clytie.plugin_host.snapshots import atomic_write, canonical, contained, identifier

from .restore import digest
from .scheduling import BackupScheduler


class ManagementWorkers:
    def __init__(self, *workers):
        self.workers = workers

    @property
    def stopping(self):
        return any(worker.stopping for worker in self.workers)

    @stopping.setter
    def stopping(self, value):
        for worker in self.workers:
            worker.stopping = value

    @property
    def still_finishing(self):
        return any(worker.still_finishing for worker in self.workers)

    def status(self):
        return [row for worker in self.workers for row in worker.status()]

    def escalate(self):
        for worker in self.workers:
            worker.escalate()

    async def stop(self, mode):
        await asyncio.gather(*(worker.stop(mode) for worker in self.workers))


def metadata(home, operation_id, name, value):
    from .journal import BackupJournal

    BackupJournal.path_for(home, operation_id)
    path = contained(home, Path(home) / "run/backup-work" / operation_id / (name + ".json"))
    atomic_write(path, canonical(value))
    return dict(path=str(path), sha256=digest(path))


async def capture_request(companion, args):
    from .journal import BackupJournal
    from .runtime_capture import capture_online
    from .space import SpaceGuard
    from .worker import Cancelled
    from .workers import executor_alive

    home = Path(companion.home).resolve()
    epoch = args["epoch"]
    path = BackupJournal.path_for(home, epoch)
    root = contained(home, home / "run/backup-work" / epoch)
    work = contained(root, Path(args["work"]))
    space = await asyncio.to_thread(SpaceGuard(home, epoch, clock=companion.clock).load)

    def check():
        journal = BackupJournal.load(path, companion.clock)
        if journal.value["cancel_requested"]:
            raise Cancelled()
        if executor_alive(journal.value.get("executor")) is not True:
            raise Cancelled()
        if journal.value["phase"] != "capturing":
            raise ValueError("capture_no_longer_active")

    check()
    from clytie.api.auth import APIError

    from .source import SourceError

    try:
        result = await capture_online(
            companion, work, epoch, scope=args["scope"], check=check, space_guard=space
        )
    except SourceError as exc:
        raise APIError(str(exc), 409) from None
    check()
    return await asyncio.to_thread(metadata, home, epoch, "capture-" + companion.cid, result)


class CaptureBridge:
    def __init__(self, supervisor=None):
        self.supervisor = supervisor

    async def __call__(self, journal, request):
        supervisor = self.supervisor
        home = journal.path.parents[2]
        operation_id = journal.value["id"]
        op, args = request["operation"], request["arguments"]
        if op == "context":
            return dict(
                online_ids=sorted(
                    cid
                    for cid in supervisor.children
                    if supervisor.rows.get(cid, {}).get("state")
                    not in {"stopped", "blocked", "restart_stopped", "backoff"}
                )
                if supervisor
                else []
            )
        if op == "capture":
            if supervisor is None or args["epoch"] != operation_id:
                raise ValueError("snapshot_unavailable")
            cid = identifier(args["companion_id"])
            if cid not in supervisor.children:
                raise ValueError("snapshot_unavailable")
            return await supervisor.request(
                cid,
                "backup_capture",
                {**args, "scope": journal.value["arguments"].get("scope", "instance")},
                timeout=None,
            )
        if op == "audit":
            from .logs import RotatingLogWriter

            writer = (
                supervisor.audit.writer
                if supervisor
                else RotatingLogWriter(home, "supervisor/audit.jsonl", audit=True)
            )
            rows = await asyncio.to_thread(writer.seal)
            return await asyncio.to_thread(metadata, home, operation_id, "audit", rows)
        raise ValueError("invalid_capture_request")


def bind(supervisor, backup_workers, *, memory_workers=None):
    if backup_workers.tasks or backup_workers.io_control.sessions:
        raise ValueError("backup_io_bind_while_active")
    backup_workers.io_control = supervisor.backup_io
    backup_workers.capture_handler = CaptureBridge(supervisor)
    supervisor.management_workers = ManagementWorkers(
        *((memory_workers,) if memory_workers is not None else ()), backup_workers
    )
    return supervisor.management_workers


class BackupController:
    def __init__(self, supervisor, workers, instance):
        self.supervisor, self.workers = supervisor, workers
        self.clock, self.home = supervisor.clock, Path(supervisor.home)
        self.scheduler = BackupScheduler(
            self.home,
            clock=self.clock,
            timezone=instance.get("timezone", "UTC"),
            config=instance.get("backup", {}),
        )
        self.task = None
        self.shutdown_id = uuid4().hex

    async def start(self):
        if getattr(self.supervisor, "maintenance", False):
            return
        self.task = asyncio.create_task(self._start_loop())

    async def _start_loop(self):
        await self.workers.recover_pending()
        await self._loop()

    def _revisions(self):
        values = {}
        for cid in self.supervisor.companions:
            path = contained(self.home, self.home / "companions" / cid / "memory/memory.sqlite3")
            if not path.exists():
                continue
            try:
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0) as db:
                    row = db.execute(
                        "SELECT value FROM metadata WHERE key='backup_revision'"
                    ).fetchone()
                if row and type(value := json.loads(row[0])) is int and value > 0:
                    values[cid] = value
            except (OSError, sqlite3.Error, ValueError):
                continue
        return values

    async def _finish_active(self):
        state = await asyncio.to_thread(self.scheduler.status)
        if not state["active"]:
            return
        job = state["jobs"][state["active"]]
        try:
            current = self.workers.query(job["operation_id"])
        except (ValueError, OSError):
            # A claim without an executor journal cannot be replayed after a crash.
            await asyncio.to_thread(self.scheduler.recover_interrupted, job["operation_id"])
            return
        phase = current["phase"]
        if phase not in {"committed", "failed", "cancelled"}:
            return
        result = current.get("result", {})
        await asyncio.to_thread(
            self.scheduler.finish,
            job["operation_id"],
            creation=result.get("creation", "failed") if phase == "committed" else phase,
            # Requested versions are a lower bound captured after the claim.
            covered_versions=job["requested_versions"] if phase == "committed" else {},
            verified=result.get("verification") == "passed",
            committed=phase == "committed",
        )

    async def tick(self):
        await self._key_notice()
        revisions = await asyncio.to_thread(self._revisions)
        for cid, revision in revisions.items():
            await asyncio.to_thread(self.scheduler.mark_dirty, cid, revision, "revision")
        await self._finish_active()
        if self.workers.tasks or self.workers.stopping:
            return
        request = await asyncio.to_thread(self.scheduler.poll)
        if request:
            await self._submit(request)

    async def _key_notice(self):
        from clytie.config import repository_root

        from .keys import BackupKeys

        keys = BackupKeys(self.home, self.clock, repository=repository_root())
        if not keys.path.exists() and not keys.state.exists():
            return
        status = await asyncio.to_thread(keys.status)
        key = "backup_key:instance"
        if status["needs_safekeeping"]:
            self.supervisor.problems[key] = (
                "backup_key_needs_safekeeping" if status["available"] else "backup_key_unavailable"
            )
        else:
            self.supervisor.problems.pop(key, None)

    async def _submit(self, request, *, shutdown=False):
        from .workers import WorkerError

        try:
            execute = self.workers._execute if shutdown else self.workers.execute
            await execute(
                "create",
                dict(
                    scope=request.scope,
                    companion_id=request.companion_id,
                    manual=False,
                    trigger_id=request.trigger_id,
                ),
                request.operation_id,
                **({"shutdown": True} if shutdown else {}),
            )
        except WorkerError:
            await asyncio.to_thread(
                self.scheduler.finish, request.operation_id, creation="failed", verified=False
            )

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except (OSError, ValueError):
                self.supervisor.problems["backup:instance"] = "backup_schedule_unavailable"
            await self.clock.sleep(1)

    async def pause(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def shutdown(self):
        await self.pause()
        if getattr(self.supervisor, "maintenance", False):
            return
        await self._finish_active()
        for cid, revision in (await asyncio.to_thread(self._revisions)).items():
            await asyncio.to_thread(self.scheduler.mark_dirty, cid, revision, "revision")
        pending = await asyncio.to_thread(self.scheduler.shutdown, self.shutdown_id)
        if pending:
            request = await asyncio.to_thread(self.scheduler.poll, trigger="shutdown")
            if request and request.trigger == "shutdown":
                if self.supervisor.escalation.is_set():
                    await asyncio.to_thread(
                        self.scheduler.finish, request.operation_id, creation="cancelled"
                    )
                    return
                self.workers.cancel_since = None
                await self._submit(request, shutdown=True)
                task = self.workers.tasks.get(request.operation_id)
                if task:
                    await asyncio.shield(task)
                await self._finish_active()
