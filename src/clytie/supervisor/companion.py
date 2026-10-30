"""单伴侣运行时：复用宿主、Agent 和账本，不重放旧任务。"""

import asyncio

from clytie.agent import Agent
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
        self.agent = self.kernel = None
        self.channels, self.feeds = {}, []
        self.escalated = asyncio.Event()
        self.config = {}
        self._closing = None
        self.orphan = False
        self.report = None

    async def start(self):
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
        try:
            ledger = await self.runtime.open()
        except OperationBusy:
            raise StartupBlocked("lock_held") from None
        except LedgerError:
            raise StartupBlocked("migration_blocked") from None
        # Restart correction precedes plugin start; reactions are initialized by Agent.start.
        interrupted = ledger.restart()
        self.host.configure_report(report)
        try:
            self.host.memory_store(self.cid)
        except OperationBusy:
            raise StartupBlocked("lock_held") from None
        except MemoryStartupError:
            raise StartupBlocked("migration_blocked") from None
        required = {config["model"]["plugin"], *(c["plugin"] for c in config["channels"])}
        selected = required | {
            pid for pid, v in config.get("plugins", {}).items() if v.get("enabled", True)
        }
        for pid in sorted(selected):
            plan = self.host.plan_enable(self.cid, pid)
            if not plan.ok:
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
            try:
                await channel.connect()
            except PluginUnavailable as exc:
                reason = startup_reason(exc.code)
                if reason:
                    raise StartupBlocked(reason) from None
                raise
        for channel in self.channels.values():
            self.feeds.append(asyncio.create_task(self.consume(channel)))

    async def consume(self, channel):
        async for event in channel.events():
            await self.agent.receive(event)

    def health(self):
        if not self.kernel:
            return {}
        records = self.kernel.ledger.rows("record")
        intents = self.kernel.ledger.rows("intent")
        notices = set(self.agent.boundary.warnings)
        if self.report:
            from dataclasses import replace

            from clytie.memory.status import notices as memory_notices

            only = replace(
                self.report,
                companions=tuple(c for c in self.report.companions if c["id"] == self.cid),
            )
            notices.update(n["code"] for n in memory_notices(only, clock=self.clock))
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
        return dict(
            inflight_tasks=sum(
                r["kind"] == "task" and r["exec_status"] not in EXEC_TERMINAL for r in records
            ),
            sending=sum(r["status"] == "sending" for r in intents),
            channels=states,
            model=self.agent.model_health,
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

    def silence(self, on):
        self.kernel.delivery.silenced = bool(on)
        if on:
            for row in self.kernel.ledger.rows("intent"):
                if row["status"] not in INTENT_TERMINAL and row["status"] != "sending":
                    self.kernel.ledger.fail_intent(row["intent_id"], "silenced")

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

    async def _stop(self, mode, *, orphan=False):
        if not self.agent:
            await self.host.close()
            self.runtime.close()
            return dict(state="stopped", stopped=[], not_stopped={}, reason=None)
        agent, kernel = self.agent, self.kernel
        agent.shutting_down = True
        if orphan:
            kernel.delivery.session_ending = True
        for scheduler in self.host.schedulers.values():
            await scheduler.close()
        for row in kernel.ledger.rows("intent"):
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
                kernel.ledger.fail_intent(row["intent_id"], code)

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
            for record in kernel.ledger.rows("record"):
                if record["exec_status"] in EXEC_TERMINAL:
                    continue
                trace = record["trace_id"]
                token = kernel.tokens.setdefault(trace, CancelToken())
                token.cancel("cancelled")
                record["cancelled"] = True
                with kernel.ledger.transaction():
                    kernel.ledger.put("record", trace, record)
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
            for record in kernel.ledger.rows("record"):
                if record["exec_status"] not in EXEC_TERMINAL:
                    record["cancelled"] = True
                    with kernel.ledger.transaction():
                        kernel.ledger.put("record", record["trace_id"], record)
                    kernel.ledger.seal(
                        record["trace_id"], "cancelled", error={"code": "cancelled", "message": ""}
                    )
        if not self.orphan:
            try:
                await timed(delivery_idle(), self.clock, wait)
            except TimeoutError:
                pass
        for row in kernel.ledger.rows("intent"):
            if row["status"] not in INTENT_TERMINAL and row["status"] != "sending":
                kernel.ledger.fail_intent(row["intent_id"], "shutdown_before_send")
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
        result = await self.host.close()
        for channel in self.channels.values():
            try:
                await channel.close()
            except Exception:
                pass
        self.runtime.close()
        row = result.get(self.cid, {"stopped": [], "not_stopped": {}})
        row["not_stopped"] = {
            pid: value.get("error", value.get("outcome", "stop_failed"))
            if isinstance(value, dict)
            else value
            for pid, value in row["not_stopped"].items()
        }
        return dict(state="not_stopped" if row["not_stopped"] else "stopped", reason=None, **row)
