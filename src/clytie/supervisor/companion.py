"""单伴侣运行时：复用宿主、Agent 和账本，不重放旧任务。"""

import asyncio
from copy import deepcopy

from clytie.agent import Agent
from clytie.backup.coordination import run_sync
from clytie.config import load_home, repository_root, thaw
from clytie.kernel.ledger import EXEC_TERMINAL, INTENT_TERMINAL, LedgerError
from clytie.kernel.migration import LedgerRuntime
from clytie.kernel.service import Kernel
from clytie.memory.errors import MemoryStartupError
from clytie.plugin_host import PluginHost
from clytie.plugin_host.locking import OperationBusy
from clytie.plugin_host.support import HostLog, timed
from clytie.sdk import CancelToken, PluginUnavailable
from clytie.sdk.channel import channel_state

from .common import finish_shutdown, read


class StartupBlocked(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def startup_reason(code):
    if code in {"auth_invalid", "quota_exhausted"}:
        return code
    return "required_plugin_failed" if code and code not in {"transient", "rate_limited"} else None


class Companion:
    def __init__(self, home, cid, clock, *, repo_root=None, host=None):
        self.home, self.cid, self.clock = home, cid, clock
        self.root = repo_root or repository_root()
        self.log = HostLog()
        self.host = host or PluginHost(self.root, home, clock=clock)
        self.runtime = LedgerRuntime(home, cid, clock, log=self.log)
        self.runtime.owner_managed = True
        self.storage_owner = None
        self.stop_finalization = None
        self._health_data = {"inflight_tasks": 0, "sending": 0, "storage_observed_at": None}
        self._memory_notices = set()
        self.agent = self.kernel = None
        self.channels, self.feeds = {}, []
        self.escalated = asyncio.Event()
        self.config = {}
        self._closing = None
        self.orphan = False
        self.report = None
        self.management_requests = set()
        self.management_tasks = set()
        self.management_journals = {}
        self.on_phase = lambda phase: None
        self.stop_requested = lambda: False

    async def start(self):
        try:
            return await self._start()
        except BaseException:
            await self.stop()
            raise

    async def _start(self):
        report = load_home(self.home, repo_root=self.root)
        from dataclasses import replace

        status = next((row for row in report.companions if row["id"] == self.cid), None)
        if (
            report.global_errors
            or not status
            or not status["valid"]
            or self.cid not in report.plugin_configs
        ):
            raise StartupBlocked("config_invalid")
        report = replace(
            report,
            errors=(),
            companions=(status,),
            plugin_configs={self.cid: report.plugin_configs[self.cid]},
        )
        self.report = report
        self.config = thaw(report.instance.get("supervisor", {}))
        config = thaw(report.plugin_configs[self.cid]["config"])
        self.on_phase("migrating")
        try:
            # Runtime atomically acquires the ledger lock against cleanup, then
            # releases the instance startup gate before any migration awaits.
            await self.runtime.open()
        except OperationBusy:
            raise StartupBlocked("lock_held") from None
        except LedgerError:
            raise StartupBlocked("migration_blocked") from None
        if self.stop_requested():
            return False
        self.host.configure_report(report)
        ledger = await self._open_storage()
        # 重启修正先于插件启动；状态回应仍由 Agent.start 初始化。
        interrupted = await run_sync(ledger, ledger.restart)
        self.on_phase("starting")
        if self.stop_requested():
            return False
        required = {config["model"]["plugin"], *(c["plugin"] for c in config["channels"])}
        selected = required | {
            pid for pid, v in config.get("plugins", {}).items() if v.get("enabled", True)
        }
        for pid in sorted(selected):
            if self.stop_requested():
                return False
            plan = self.host.plan_enable(self.cid, pid)
            if not plan.ok:
                if pid in self.host._excluded:
                    self.host.states.setdefault(self.cid, {})[pid] = {"status": "uninstalled"}
                if pid in required:
                    raise StartupBlocked("required_plugin_failed")
                continue
            await self.host.apply(plan.plan_id)
            if not self.host.is_active(self.cid, pid) and pid in required:
                state = self.host.states.get(self.cid, {}).get(pid, {}).get("status")
                if state in {
                    "recovery_incomplete",
                    "migration_pending",
                    "migration_failed",
                    "state_missing",
                }:
                    raise StartupBlocked("migration_blocked")
                runtime = self.host.runtimes.get((self.cid, pid))
                reason = startup_reason(runtime.failure_code if runtime else None)
                if reason:
                    raise StartupBlocked(reason)
                raise RuntimeError("required_plugin_failed")
        if self.stop_requested():
            return False
        self.channels = {
            r.key: r.implementation for r in self.host.registrations(self.cid, "channel")
        }
        self.kernel = Kernel(
            ledger,
            self.clock,
            self.channels,
            config=config,
            max_task_seconds=report.instance.get("limits", {}).get("max_task_seconds", 3600),
        )
        self.kernel.delivery.silenced = read(self.home / "supervisor/silence.json", {"on": False})[
            "on"
        ]
        try:
            self.agent = Agent(
                self.kernel,
                self.host,
                config,
                thaw(report.instance),
                self.home / "companions" / self.cid / "persona",
            )
        except OperationBusy:
            raise StartupBlocked("lock_held") from None
        except MemoryStartupError:
            raise StartupBlocked("migration_blocked") from None
        await self.agent.start()
        await self.kernel.reactions.restart(interrupted)
        for channel in self.channels.values():
            if self.stop_requested():
                return False
            self.kernel.delivery.silenced = read(
                self.home / "supervisor/silence.json", {"on": False}
            )["on"]
            try:
                await channel.connect()
            except PluginUnavailable as exc:
                reason = startup_reason(exc.code)
                if reason:
                    raise StartupBlocked(reason) from None
                raise
        if self.stop_requested():
            return False
        for channel in self.channels.values():
            self.feeds.append(asyncio.create_task(self.consume(channel)))
        await self.refresh_health()
        return True

    async def consume(self, channel):
        async for event in channel.events():
            await self.agent.receive(event)

    def health(self):
        if not self.kernel:
            return {}
        notices = set(self.agent.boundary.warnings) | self._memory_notices
        notices.update(self.agent.appearance_warnings)
        channel_plugins = {r.key: r.plugin_id for r in self.host.registrations(self.cid, "channel")}
        states = {}
        priority = {"unknown": 0, "online": 1, "connecting": 2, "offline": 3, "auth_invalid": 4}
        for key, channel in self.channels.items():
            state = (
                "auth_invalid"
                if self.kernel.delivery.channel_errors.get(key) == "auth_invalid"
                else channel_state(channel).status
            )
            pid = channel_plugins.get(key, key)
            if priority[state] >= priority[states.get(pid, "unknown")]:
                states[pid] = state
        status = dict(
            **deepcopy(self._health_data),
            channels=states,
            model=self.agent.model_health,
            admission=self.agent.admission.status(),
            quota=(
                [
                    dict(
                        remaining_ratio=w.remaining_ratio,
                        resets_at=w.resets_at.isoformat() if w.resets_at else None,
                    )
                    for w in self.agent.protection.snapshot.windows
                ]
                if self.agent.protection.snapshot
                else "unknown"
            ),
            model_plugin=self.agent.config["model"]["plugin"],
            silenced=self.kernel.delivery.silenced,
            warnings=sorted(
                notices
                | {
                    row.get("status", "plugin_unavailable")
                    for row in self.host.states.get(self.cid, {}).values()
                    if row.get("status") not in {"active", "disabled"}
                }
            ),
            quota_exhausted=self.agent.model_error_code == "quota_exhausted",
        )
        if self.agent.appearance_summary is not None:
            status["appearance_check"] = self.agent.appearance_summary
        return status

    async def silence(self, on):
        self.kernel.delivery.silenced = bool(on)
        if on:
            await run_sync(self.kernel.ledger, self._silence_pending)

    def session_ending(self):
        if self.kernel:
            self.kernel.delivery.session_ending = True

    async def stop(self, mode="cancel", *, orphan=False):
        if orphan:
            self.orphan = True
            self.escalated.set()
            self.session_ending()
        if self._closing is None:
            self._closing = asyncio.create_task(self._stop(mode, orphan=orphan))

        async def join():
            return await asyncio.shield(self._closing)

        return await finish_shutdown(join())

    def cancel_management(self):
        for journal in tuple(self.management_journals.values()):
            journal.cancel()

    async def _stop_persist(self, function, *args, trace=None, **kwargs):
        ledger = self.kernel.ledger if self.kernel else self.runtime.ledger
        if trace is not None and self.kernel and trace in self.kernel.finalizations:
            return await self.kernel.persist(trace, function, *args, **kwargs)
        if self.stop_finalization is not None:
            return await self.stop_finalization.persist(ledger, function, *args, **kwargs)
        return await run_sync(ledger, function, *args, **kwargs)

    async def _stop(self, mode, *, orphan=False):
        if mode == "cancel" or self.escalated.is_set():
            self.cancel_management()
        if not self.agent:
            await self.host.event_bus.close()
            await self.host.close()
            await self._close_storage()
            return dict(state="stopped", stopped=[], not_stopped={}, reason=None)
        agent, kernel = self.agent, self.kernel
        agent.shutting_down = True
        if orphan:
            kernel.delivery.session_ending = True
        for scheduler in self.host.schedulers.values():
            await scheduler.close()
        for row in await self._stop_persist(kernel.ledger.rows, "intent"):
            if (
                row["status"] not in INTENT_TERMINAL
                and row["status"] != "sending"
                and not kernel.delivery.group_started(row)
            ):
                code = (
                    row["error"]["code"]
                    if row["status"] == "retry_wait" and row["error"]
                    else "shutdown_before_send"
                )
                await self._stop_persist(
                    kernel.ledger.fail_intent, row["intent_id"], code, trace=row["trace_id"]
                )

        kernel.reactions.begin_stop()

        async def delivery_idle():
            while (kernel.delivery.active_tokens or kernel.delivery.workers) and not self.orphan:
                await self.clock.sleep(0.01)

        wait = self.config.get("stop", {}).get("delivery_wait_seconds", 30)
        if not self.orphan:
            try:
                await timed(delivery_idle(), self.clock, wait)
            except TimeoutError:
                pass
        drain = asyncio.create_task(agent.drain())
        if mode == "wait" and not self.escalated.is_set():
            escalation = asyncio.create_task(self.escalated.wait())
            try:
                await asyncio.wait({drain, escalation}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                escalation.cancel()
                await asyncio.gather(escalation, return_exceptions=True)
        if mode == "cancel" or self.escalated.is_set():
            # The host owns this path; do not apply conversation visibility to shutdown.
            for record in await self._stop_persist(kernel.ledger.rows, "record"):
                if record["exec_status"] in EXEC_TERMINAL:
                    continue
                trace = record["trace_id"]
                token = kernel.tokens.setdefault(trace, CancelToken())
                token.cancel("cancelled")
                record = await self._stop_persist(self._mark_cancelled, trace, trace=trace)
                pending = kernel.queued.get(trace)
                if record["exec_status"] == "pending" and pending:
                    pending.cancel()
            try:
                await timed(
                    asyncio.shield(drain), self.clock, kernel.grace if not self.orphan else 0
                )
            except TimeoutError:
                for task in tuple(kernel.record_tasks.values()) + tuple(kernel.tasks.values()):
                    task.cancel()
            for record in await self._stop_persist(kernel.ledger.rows, "record"):
                if record["exec_status"] not in EXEC_TERMINAL:
                    await self._stop_persist(
                        self._mark_cancelled, record["trace_id"], trace=record["trace_id"]
                    )
                    await self._stop_persist(
                        kernel.ledger.seal,
                        record["trace_id"],
                        "cancelled",
                        error={"code": "cancelled", "message": ""},
                        trace=record["trace_id"],
                    )
        if not self.orphan:
            try:
                await timed(delivery_idle(), self.clock, wait)
            except TimeoutError:
                pass
        for row in await self._stop_persist(kernel.ledger.rows, "intent"):
            if row["status"] not in INTENT_TERMINAL and row["status"] != "sending":
                await self._stop_persist(
                    kernel.ledger.fail_intent,
                    row["intent_id"],
                    "shutdown_before_send",
                    trace=row["trace_id"],
                )
        jobs = tuple(self.host.memory_jobs.get(self.cid, ()))
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        remaining = tuple(agent.background)
        for task in remaining:
            task.cancel()
        # The parent enforces the overall deadline for uncooperative plugins.
        await asyncio.gather(*remaining, return_exceptions=True)
        if not drain.done():
            drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)
        for task in self.feeds:
            task.cancel()
        await asyncio.gather(*self.feeds, return_exceptions=True)
        # Accepted management work must finish before plugin shutdown / database close.
        await asyncio.gather(*tuple(self.management_requests), return_exceptions=True)
        await asyncio.gather(*tuple(self.management_tasks), return_exceptions=True)
        await kernel.bus.close()
        await self.host.event_bus.close()
        result = await self.host.close()
        for channel in self.channels.values():
            try:
                await channel.close()
            except Exception:
                pass
        await self._close_storage()
        row = result.get(self.cid, {"stopped": [], "not_stopped": {}})
        row["not_stopped"] = {
            pid: value.get("error", value.get("outcome", "stop_failed"))
            if isinstance(value, dict)
            else value
            for pid, value in row["not_stopped"].items()
        }
        return dict(state="not_stopped" if row["not_stopped"] else "stopped", reason=None, **row)

    async def _open_storage(self):
        from clytie.api.management_journal import ManagementBusy, ManagementCancelled
        from clytie.storage.owner import StorageFailed, StorageOwner
        from clytie.storage.runtime import CompanionResources, bind_handles
        from clytie.storage.startup_recovery import recover_before_owner

        if self.cid in self.host.memory_resources:
            raise RuntimeError("runtime_memory_already_open")
        try:
            await recover_before_owner(self)
        except ManagementCancelled:
            raise asyncio.CancelledError from None
        except ManagementBusy:
            reason = (
                "management_recovery_blocked"
                if (self.home / "companions" / self.cid / "management/blocked.json").exists()
                else "lock_held"
            )
            raise StartupBlocked(reason) from None
        self.runtime.management_recovery_prepared = True
        if self.stop_requested():
            raise asyncio.CancelledError
        failure = {}
        opening = asyncio.create_task(
            StorageOwner.start(
                lambda: CompanionResources(self.host, self.runtime, failure),
                companion_id=self.cid,
                clock=self.clock,
                coordination_path=self.home / "companions" / self.cid / "backup-capture.lock",
            )
        )
        cancelled = False
        try:
            try:
                self.storage_owner = await asyncio.shield(opening)
            except asyncio.CancelledError:
                cancelled = True
                # 真实迁移/创建者未退出前不释放生命周期锁或遗弃资源。
                self.storage_owner = await asyncio.shield(opening)
        except StorageFailed:
            if failure.get("reason"):
                raise StartupBlocked(failure["reason"]) from None
            raise
        ledger = bind_handles(self.storage_owner, self.host, self.runtime)
        from clytie.storage.finalization import StopFinalization

        self.stop_finalization = StopFinalization(ledger)
        self.host.stop_finalizations[self.cid] = self.stop_finalization
        await self.runtime.attach_owned(ledger)
        if cancelled:
            raise asyncio.CancelledError
        return ledger

    async def _close_storage(self):
        if self.storage_owner is not None:
            # 未退出的插件仍可能持有停止回调责任；等待外层期限处理。
            if self.cid in self.host.memory_resources:
                return
            if self.stop_finalization is not None:
                await self.stop_finalization.finish()
                self.host.stop_finalizations.pop(self.cid, None)
            if self.runtime.maintenance:
                task = self.runtime.maintenance
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.runtime.maintenance = None
            await self.storage_owner.close()
            self.host.storage_owners.pop(self.cid, None)
        self.runtime.close()

    def _mark_cancelled(self, trace):
        with self.kernel.ledger.transaction():
            record = self.kernel.ledger.get("record", trace)
            record["cancelled"] = True
            self.kernel.ledger.put("record", trace, record)
            return record

    def _silence_pending(self):
        for row in self.kernel.ledger.rows("intent"):
            if row["status"] not in INTENT_TERMINAL and row["status"] != "sending":
                self.kernel.ledger.fail_intent(row["intent_id"], "silenced")

    def _read_health(self):
        counts = self.kernel.ledger.health_counts()
        notices = set()
        if self.report:
            from clytie.memory.status import notices as memory_notices

            notices.update(n["code"] for n in memory_notices(self.report, clock=self.clock))
        counts["storage_observed_at"] = self.clock.now().isoformat()
        return counts, notices

    async def refresh_health(self):
        from clytie.storage.owner import StorageBusy, StorageFailed

        if not self.kernel:
            return
        try:
            counts, notices = await run_sync(self.kernel.ledger, self._read_health)
        except (StorageBusy, StorageFailed):
            return
        self._health_data, self._memory_notices = counts, notices
