"""只管理进程与无内容健康状态，不访问账本。"""

import asyncio
import inspect
import re
from datetime import datetime
from pathlib import Path
from typing import Protocol

from clytie.backup.io_budget import IoBudgetError
from clytie.backup.io_control import IoControl
from clytie.plugin_host.locking import file_lock, process_identity
from clytie.plugin_host.support import HostLog, timed

from .common import Audit, finish_shutdown, read, write
from .notifications import Notifications

FATAL = {
    "management_recovery_blocked",
    "config_invalid",
    "migration_blocked",
    "lock_held",
    "auth_invalid",
    "quota_exhausted",
    "required_plugin_failed",
}


class ExternalComponent(Protocol):
    async def status(self): ...
    async def start(self): ...
    async def stop(self): ...


class Launcher(Protocol):
    async def launch(self, cid): ...


class Supervisor:
    def __init__(
        self, home, companions, launcher, clock, *, config=None, notifications=None, external=()
    ):
        self.home, self.clock = Path(home), clock
        self.launcher, self.companions = launcher, list(companions)
        self.config, self.log = config or {}, HostLog()
        self.audit = Audit(home, clock)
        self.notifications = notifications or Notifications(home, clock, self.log, self.audit)
        self.external = external
        self.rows, self.children, self.monitors = {}, {}, {}
        self.conversations = {}
        self.safety_notifications = set()
        self.lock = None
        self.stopping = False
        self.seq = 0
        self.acks = {}
        self.stop_results = {}
        self.ready_events = {cid: asyncio.Event() for cid in self.companions}
        self.last_write = None
        self.last_content = None
        self.problems = {}
        self.pending_health = set(self.companions)
        self.silenced = read(self.home / "supervisor/silence.json", {"on": False})["on"]
        self.notifications.silenced = self.silenced
        self.ticker = None
        self.escalation = asyncio.Event()
        self._closing = None
        self.finalize = None
        self.management_workers = None
        self.requests = {}
        self.suspended = set()
        self.request_stop = None
        self.startup_cancelled = lambda: False
        self.backup_io = IoControl(clock, home=home)
        self.backup_io_sessions = {}

    def setting(self, group, key, default):
        return self.config.get(group, {}).get(key, default)

    async def start(self, prepare=None):
        from clytie.backup.old_instances import startup_gate

        self.lock = file_lock(
            self.home / "run/supervisor.lock", {**process_identity(), "operation": "supervisor"}
        )
        with startup_gate(self.home):
            self.update_lock = self.lock.__enter__()
        try:
            # This only projects heavy-I/O admission. Unrelated companions may
            # still boot and serve ordinary conversation while evidence is blocked.
            await self.backup_io.recheck()
            if prepare:
                await prepare()
        except BaseException:
            self.lock.__exit__(None, None, None)
            self.lock = None
            raise
        self.audit.append("start")
        for cid in self.companions:
            self.rows[cid] = dict(
                state="starting",
                failures=0,
                restart_count=0,
                restart_reason=None,
                uptime=0,
                inflight_tasks=0,
                sending=0,
                channels={},
                model="unknown",
                quota="unknown",
                silenced=self.silenced,
                warnings=[],
            )
            if self.startup_cancelled():
                self.rows[cid]["state"] = "stopped"
                self.monitors[cid] = asyncio.create_task(self.clock.sleep(0))
            else:
                self.monitors[cid] = asyncio.create_task(self.monitor(cid))
        self.ticker = asyncio.create_task(self.tick())

    async def command(self, child, kind, **fields):
        self.seq += 1
        await child.send(dict(type=kind, seq=self.seq, **fields))
        return self.seq

    async def authorize_backup_capture(self, cid, operation_id, *, child=None):
        child = self.children[cid] if child is None else child
        session = self.backup_io_sessions[cid]
        identity = await asyncio.to_thread(child.identity)
        if self.children.get(cid) is not child or self.backup_io_sessions.get(cid) != session:
            raise IoBudgetError("backup_io_executor_lost")
        await self.backup_io.authorize_execution(session, operation_id, identity, role="capture")

    async def _backup_io_reply(self, child, response):
        await self.command(
            child,
            response["type"],
            **{key: value for key, value in response.items() if key != "type"},
        )

    async def _backup_io_exit_confirmed(self, cid, *, session=None):
        session = self.backup_io_sessions.get(cid) if session is None else session
        if session is None:
            return True
        try:
            await self.backup_io.ended(session, confirmed=True)
            return True
        except IoBudgetError as exc:
            # Actual process exit is already known. A failed durable release is
            # separate evidence; never turn it into a second kill or abandon the
            # other companions and daemon resources during shutdown.
            self.rows[cid]["last_heavy_io_release"] = {"state": "unverified", "reason": exc.code}
            self.log.warning(
                "supervisor.backup_io_release_unverified", companion_id=cid, reason=exc.code
            )
            self.audit.append("backup_io_release_unverified", cid, result="failed", reason=exc.code)
            return False
        finally:
            if self.backup_io_sessions.get(cid) == session:
                self.backup_io_sessions.pop(cid)

    async def monitor(self, cid):
        row = self.rows[cid]
        while not self.stopping and cid not in self.suspended:
            row.update(state="starting", phase="starting", migration_stalled=False)
            startup_elapsed = 0
            accounted = self.clock.monotonic()
            fatal, ready = None, None
            reason = None
            last = self.clock.monotonic()
            io_session, exit_confirmed = None, False
            try:
                child = await self.launcher.launch(cid)
                self.children[cid] = child
                io_session = self.backup_io.register(storage_owner=True)
                self.backup_io_sessions[cid] = io_session
                # Only parent-selected existing management journals are authorized.
                # A new launch/session never regains the prior executor's stage.
                for operation in getattr(child, "recovery_operations", ()):
                    await self.backup_io.authorize_execution(
                        io_session,
                        operation,
                        await asyncio.to_thread(child.identity),
                        role="recovery",
                    )
                while True:
                    now = self.clock.monotonic()
                    if row["phase"] == "starting" and ready is None:
                        startup_elapsed += now - accounted
                    accounted = now
                    hang = self.config.get("hang_seconds", 30)
                    startup_limit = self.config.get("startup_seconds", 300)
                    budget = max(0, last + hang - now)
                    if row["phase"] == "migrating" or self.stopping or cid in self.suspended:
                        budget = hang
                    elif ready is None:
                        budget = min(budget, max(0, startup_limit - startup_elapsed))
                    try:
                        msg = await timed(child.receive(), self.clock, budget)
                    except TimeoutError:
                        now = self.clock.monotonic()
                        if self.stopping or cid in self.suspended:
                            last = now
                            continue
                        if row["phase"] == "migrating":
                            row["migration_stalled"] = True
                            self.problems[f"startup_stalled:{cid}"] = f"{cid} startup_stalled"
                            continue
                        await child.kill()
                        reason = (
                            "startup_timeout"
                            if ready is None and startup_elapsed + now - accounted >= startup_limit
                            else "heartbeat_timeout"
                        )
                        break
                    now = self.clock.monotonic()
                    if row["phase"] == "starting" and ready is None:
                        startup_elapsed += now - accounted
                    accounted = now
                    if msg is None:
                        break
                    kind = msg.get("type")
                    if kind == "ready":
                        ready = self.clock.monotonic()
                        last = ready
                        if not self.stopping and cid not in self.suspended:
                            row["state"] = "running"
                        row["phase"] = "running"
                        self.ready_events[cid].set()
                    elif kind == "heartbeat":
                        last = self.clock.monotonic()
                        phase = msg.get("phase", row["phase"])
                        if phase in {"starting", "migrating", "running", "stopping"}:
                            row["phase"] = phase
                        if phase == "running":
                            self.pending_health.discard(cid)
                            self.problems = {
                                k: v
                                for k, v in self.problems.items()
                                if not k.startswith(f"auth_invalid:{cid}:")
                            }
                        row["migration_stalled"] = False
                        self.problems.pop(f"startup_stalled:{cid}", None)
                        for key in ("inflight_tasks", "sending"):
                            row[key] = max(0, int(msg.get(key, 0)))
                        row["channels"] = {
                            key: value
                            for key, value in msg.get("channels", {}).items()
                            if value
                            in {"online", "connecting", "offline", "auth_invalid", "unknown"}
                        }
                        row["model"] = (
                            msg.get("model")
                            if msg.get("model") in {"ok", "auth_invalid", "unknown"}
                            else "unknown"
                        )
                        row["silenced"] = bool(msg.get("silenced", False))
                        from clytie.admission import admission_status

                        admission = msg.get("admission")
                        if not isinstance(admission, dict):
                            admission = {}
                        row["admission"] = admission_status(
                            admission.get("mode"), admission.get("rejected")
                        )
                        row["warnings"] = list(msg.get("warnings", []))
                        for field in ("safety_belt", "attention"):
                            row[field] = self._conversation_status(msg.get(field), field)
                        belt_config = msg.get("safety_belt_config")
                        fields = ("replies", "window_minutes", "pause_minutes")
                        if isinstance(belt_config, dict) and all(
                            type(belt_config.get(k)) is int for k in fields
                        ):
                            row["safety_belt_config"] = {k: belt_config[k] for k in fields}
                        row["storage_observed_at"] = msg.get("storage_observed_at")
                        summary = msg.get("appearance_check")
                        if isinstance(summary, str) and re.fullmatch(
                            r"通过|失败|跳过|有 [1-9][0-9]* 条提示", summary
                        ):
                            row["appearance_check"] = summary
                        else:
                            row.pop("appearance_check", None)
                        row["quota"] = msg.get("quota", "unknown")
                        if (
                            row["phase"] == "running"
                            and "silenced" in msg
                            and row["silenced"] == self.silenced
                        ):
                            row["silence_status"] = "confirmed"
                        if not self.stopping and cid not in self.suspended and ready is not None:
                            row["state"] = (
                                "degraded" if row["model"] == "auth_invalid" else "running"
                            )
                        if row["model"] == "auth_invalid":
                            plugin = msg.get("model_plugin", "model")
                            self.problems[f"auth_invalid:{cid}:{plugin}"] = f"{cid} {plugin}"
                        for plugin, status in row["channels"].items():
                            if status == "auth_invalid":
                                self.problems[f"auth_invalid:{cid}:{plugin}"] = f"{cid} {plugin}"
                        if msg.get("quota_exhausted"):
                            resets = (
                                [
                                    w.get("resets_at")
                                    for w in row["quota"]
                                    if isinstance(w, dict) and w.get("resets_at")
                                ]
                                if isinstance(row["quota"], list)
                                else []
                            )
                            self.problems[f"quota_exhausted:{cid}"] = cid + (
                                " " + min(resets) if resets else ""
                            )
                        else:
                            self.problems.pop(f"quota_exhausted:{cid}", None)
                    elif kind == "safety_belt":
                        group = msg.get("conversation_id")
                        if self._valid_trip(group, msg):
                            task = asyncio.create_task(
                                self.notifications.safety_trip(
                                    cid, group, msg["count"], msg["until"]
                                )
                            )
                            self.safety_notifications.add(task)
                            task.add_done_callback(self.safety_notifications.discard)
                    elif kind == "backup_io":

                        async def reply(response, child=child):
                            await self._backup_io_reply(child, response)

                        self.backup_io.dispatch(io_session, msg, reply)
                    elif kind == "storage_pressure":
                        try:
                            self.backup_io.pressure(io_session, msg.get("sample"))
                        except IoBudgetError:
                            self.log.warning("supervisor.backup_pressure_invalid")
                    elif kind == "fatal":
                        fatal = msg.get("reason")
                        if fatal not in FATAL:
                            fatal = None
                    elif kind == "response":
                        pending = self.requests.get((cid, msg.get("id")))
                        if pending and not pending.done():
                            pending.set_result(msg)
                    elif kind == "stop_result":
                        self.stop_results[cid] = msg["result"]
                    elif kind == "silence_ack":
                        pending = self.acks.get(cid)
                        if pending and msg.get("seq") == pending[0]:
                            pending[1].set()
                        row["silence_status"] = "confirmed"
                    else:
                        self.log.warning("supervisor.unknown_type")
                    if ready is not None:
                        row["uptime"] = self.clock.monotonic() - ready
                        if row["uptime"] >= self.setting("restart", "stable_seconds", 600):
                            row["failures"] = 0
                    if (
                        not self.stopping
                        and cid not in self.suspended
                        and ready is None
                        and row["phase"] == "starting"
                        and startup_elapsed >= startup_limit
                    ):
                        await child.kill()
                        reason = "startup_timeout"
                        break
                self.backup_io.disconnect(io_session)
                code = await child.wait()
                exit_confirmed = True
                from clytie.api.auth import APIError

                for (owner, _), pending in tuple(self.requests.items()):
                    if owner == cid and not pending.done():
                        pending.set_exception(APIError("companion_unavailable", 409))
            except asyncio.CancelledError:
                raise
            except IoBudgetError as exc:
                code = 1
                reason = exc.code
                fatal = "management_recovery_blocked"
                row["heavy_io_reason"] = exc.code
                child = self.children.get(cid)
                if child:
                    await child.kill()
                    await child.wait()
                    exit_confirmed = True
            except Exception:
                code = 1
                reason = "process_failure"
                child = self.children.get(cid)
                if child:
                    await child.kill()
                    await child.wait()
                    exit_confirmed = True
            finally:
                if io_session is not None:
                    self.backup_io.disconnect(io_session)
                    if exit_confirmed:
                        await self._backup_io_exit_confirmed(cid, session=io_session)
            if self.stopping or cid in self.suspended:
                return
            if fatal:
                self.pending_health.discard(cid)
                row["state"] = "blocked"
                self.ready_events[cid].set()
                row["restart_reason"] = fatal
                self.audit.append("blocked", cid, reason=fatal)
                action = {
                    "auth_invalid": "重新登录后重启伴侣",
                    "quota_exhausted": "额度恢复后重启伴侣",
                    "config_invalid": "修正配置后重启伴侣",
                    "migration_blocked": "处理迁移或恢复后重启伴侣",
                    "management_recovery_blocked": "核验管理操作恢复状态后重启伴侣",
                    "lock_held": "停止占用进程后重启伴侣",
                    "required_plugin_failed": "修复必需插件后重启伴侣",
                }[fatal]
                self.problems[f"blocked:{cid}:{fatal}"] = f"{cid} {fatal} {action}"
                return
            if code == 0 and ready is not None and reason is None:
                row["state"] = "stopped"
                return
            row["state"] = "crashed"
            row["failures"] += 1
            row["restart_reason"] = reason or "process_exit"
            self.audit.append("crashed", cid, reason=row["restart_reason"])
            if row["failures"] >= self.setting("restart", "max_failures", 5):
                self.pending_health.discard(cid)
                row["state"] = "restart_stopped"
                self.ready_events[cid].set()
                self.problems[f"restart_stopped:{cid}"] = (
                    f"{cid} {row['failures']} {row['restart_reason']}"
                )
                return
            row["state"] = "backoff"
            delays = self.setting("restart", "backoff_seconds", [10, 30, 60, 120, 300])
            await self.clock.sleep(delays[min(row["failures"] - 1, len(delays) - 1)])
            row["restart_count"] += 1
            self.audit.append("restart", cid, reason=row["restart_reason"])

    async def tick(self):
        while True:
            try:
                if (self.home / "supervisor/old-instances.json").exists() and (
                    self.clock.monotonic() >= getattr(self, "_old_instances_next", 0)
                ):
                    await self.refresh_old_instances()
                await self.notifications.update(self.problems, pending=self.pending_health)
                await self.flush_health()
            except OSError as exc:
                # A transient Windows file sharing violation must not kill monitoring.
                self.log.warning("supervisor.health_write_failed", error=type(exc).__name__)
            await self.clock.sleep(1)

    def _valid_trip(self, group, value):
        if not isinstance(group, str) or self.conversations.get(group) != "group":
            return False
        if type(value.get("count")) is not int or value["count"] < 0:
            return False
        try:
            return datetime.fromisoformat(value["until"]).tzinfo is not None
        except (TypeError, ValueError, KeyError):
            return False

    def _conversation_status(self, values, field):
        result = {}
        if isinstance(values, dict):
            for key, row in values.items():
                if key not in self.conversations or not isinstance(row, dict):
                    continue
                if field == "safety_belt" and self._valid_trip(key, row):
                    result[key] = dict(
                        count=row["count"],
                        until=row["until"],
                        owner_private_missing=row.get("owner_private_missing") is True,
                    )
                elif field == "attention" and all(
                    type(row.get(k)) is int and row[k] >= 0 for k in ("count", "tokens")
                ):
                    result[key] = dict(count=row["count"], tokens=row["tokens"])
        return result

    async def refresh_old_instances(self):
        from clytie.backup.old_instances import OldInstances
        from clytie.backup.restore import read_state

        options = getattr(self, "old_instance_options", {})
        summary = await asyncio.to_thread(
            OldInstances(self.home, self.clock).status,
            warn_count=options.get("warn_count", 3),
            warn_bytes=options.get("warn_gib", 20) * 1024**3,
        )
        self.old_instances_summary = summary
        self._old_instances_next = self.clock.monotonic() + 60
        key = "old_instances:instance"
        restored = read_state(self.home)
        if restored and restored.get("switched"):
            key += ":" + restored["operation_id"]
        for previous in list(self.problems):
            if previous.startswith("old_instances:instance") and previous != key:
                self.problems.pop(previous)
        if summary["count"]:
            self.problems[key] = (
                f"old_instances count={summary['count']} known_bytes={summary['known_bytes']} "
                f"unknown={summary['unknown']} warning={summary['warning']}"
            )
        else:
            self.problems.pop(key, None)
        return summary

    async def flush_health(self):
        external = []
        for component in self.external:
            external.append(await component.status())
        content = dict(
            old_instances=getattr(self, "old_instances_summary", None),
            companions=self.rows,
            external=external,
            undelivered=self.notifications.undelivered,
            state="stopping" if self.stopping else "running",
            api_port=getattr(self, "api_port", None),
            warnings=list(self.problems.values()),
            management={
                "operations": self.management_workers.status(),
                "still_finishing": self.management_workers.still_finishing,
            }
            if self.management_workers
            else None,
        )
        import json

        snapshot = json.dumps(content, sort_keys=True)
        now = self.clock.monotonic()
        if snapshot != self.last_content and (
            self.last_write is None or now - self.last_write >= 5
        ):
            write(
                self.home / "run/health.json",
                dict(updated_at=self.clock.now().isoformat(), **content),
            )
            self.last_write, self.last_content = now, snapshot

    async def silence(self, on, by="internal"):
        write(
            self.home / "supervisor/silence.json",
            dict(on=bool(on), since=self.clock.now().isoformat(), by=by),
        )
        self.silenced = self.notifications.silenced = bool(on)
        self.audit.append("silence", result="on" if on else "off", source=by)

        async def apply(cid, child):
            ack = asyncio.Event()
            self.seq += 1
            seq = self.seq
            self.acks[cid] = (seq, ack)
            try:
                await child.send(dict(type="silence", seq=seq, on=bool(on)))
                if self.rows[cid].get("phase") == "migrating":
                    self.rows[cid]["silence_status"] = "pending_migrating"
                    return cid, "pending_migrating"
                await timed(ack.wait(), self.clock, 5)
            except (TimeoutError, OSError):
                if self.rows[cid].get("phase") == "migrating":
                    self.rows[cid]["silence_status"] = "pending_migrating"
                    return cid, "pending_migrating"
                await self.restart(cid, "silence_timeout")
                await self.ready_events[cid].wait()
            finally:
                self.acks.pop(cid, None)
            self.rows[cid]["silence_status"] = "confirmed"
            return cid, "confirmed"

        results = await asyncio.gather(
            *(
                apply(cid, child)
                for cid, child in tuple(self.children.items())
                if self.rows[cid]["state"] in {"running", "degraded", "starting"}
            )
        )
        return dict(results)

    async def request(self, cid, op, args, *, timeout=30, before_send=None, after_receive=None):
        from uuid import uuid4

        from clytie.api.auth import APIError, RequestNotDelivered

        if self.rows.get(cid, {}).get("state") not in {"running", "degraded"}:
            raise RequestNotDelivered("companion_busy", 409)
        child = self.children.get(cid)
        if child is None:
            raise RequestNotDelivered("companion_busy", 409)
        if before_send is not None:
            prepared = before_send(child)
            if inspect.isawaitable(prepared):
                await prepared
        key = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.requests[cid, key] = future
        try:
            await self.command(child, "request", id=key, op=op, args=args)
            try:
                result = (
                    await asyncio.shield(future)
                    if timeout is None
                    else await timed(asyncio.shield(future), self.clock, timeout)
                )
            except TimeoutError:
                raise APIError("companion_timeout", 504) from None
            if after_receive is not None:
                settled = after_receive()
                if inspect.isawaitable(settled):
                    await settled
            if not result["ok"]:
                error = result["error"]
                raise APIError(error["code"], error.get("status", 400))
            return result["result"]
        finally:
            self.requests.pop((cid, key), None)
            if not future.done():
                future.cancel()

    async def stop_companion(self, cid, mode=None):
        return await finish_shutdown(self._stop_companion(cid, mode))

    async def _stop_companion(self, cid, mode=None):
        self.suspended.add(cid)
        row = self.rows[cid]
        child = self.children.get(cid)
        if child is None or row["state"] in {"stopped", "blocked", "restart_stopped", "backoff"}:
            self.monitors[cid].cancel()
            await asyncio.gather(self.monitors[cid], return_exceptions=True)
            row["state"] = "stopped"
            return self.stop_results.get(cid, {"stopped": [], "not_stopped": {}})
        row["state"] = "stopping"
        self.stop_results.pop(cid, None)
        mode = mode or self.setting("stop", "task_mode", "cancel")
        try:
            await self.command(child, "stop", task_mode=mode)
            if mode == "wait":
                waiter = asyncio.create_task(child.wait())
                escalation = asyncio.create_task(self.escalation.wait())
                try:
                    done, _ = await asyncio.wait(
                        {waiter, escalation}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if waiter not in done:
                        await timed(
                            asyncio.shield(waiter),
                            self.clock,
                            self.setting("stop", "timeout_seconds", 120),
                        )
                finally:
                    escalation.cancel()
                    await asyncio.gather(escalation, return_exceptions=True)
            else:
                await timed(child.wait(), self.clock, self.setting("stop", "timeout_seconds", 120))
            await self.monitors[cid]
            return self.stop_results.get(
                cid,
                {"stopped": [], "not_stopped": {}, "reason": "process_exited_without_stop_result"},
            )
        except TimeoutError:
            await child.kill()
            await child.wait()
            await self._backup_io_exit_confirmed(cid)
            result = {"stopped": [], "not_stopped": {}, "reason": "timeout_killed"}
            self.audit.append("force_stop", cid, reason="timeout_killed", source="companion_stop")
            self.problems[f"stop_killed:{cid}"] = f"{cid} {row['sending']}"
            self.stop_results[cid] = result
            row["stop_result"] = result
            await self.notifications.update(self.problems, pending=self.pending_health)
            return result
        finally:
            self.monitors[cid].cancel()
            await asyncio.gather(self.monitors[cid], return_exceptions=True)
            row["state"] = "stopped"
            await self.flush_health()

    async def start_companion(self, cid):
        if self.rows[cid]["state"] in {"running", "degraded", "starting"}:
            return
        self.suspended.discard(cid)
        await self.restart(cid)

    async def restart(self, cid, reason="manual"):
        if self.stopping:
            return
        monitor = self.monitors.get(cid)
        if monitor:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        child = self.children.get(cid)
        if child:
            await child.kill()
            await child.wait()
        self.problems = {k: v for k, v in self.problems.items() if k.split(":")[1] != cid}
        self.pending_health.add(cid)
        self.ready_events[cid].clear()
        self.rows[cid].update(state="starting", failures=0, restart_reason=reason)
        self.rows[cid]["restart_count"] += 1
        self.monitors[cid] = asyncio.create_task(self.monitor(cid))

    async def escalate(self):
        self.escalation.set()
        if self.management_workers:
            self.management_workers.escalate()
        for child in tuple(self.children.values()):
            try:
                await self.command(child, "escalate")
            except (OSError, BrokenPipeError):
                pass

    async def session_ending(self, source="internal"):
        for child in tuple(self.children.values()):
            try:
                await self.command(child, "session_ending")
            except (OSError, BrokenPipeError):
                pass
        self.audit.append("session_ending", source=source)
        self.escalation.set()
        return await self.stop("cancel", source=source)

    async def stop(self, task_mode=None, *, source="internal"):
        self.stopping = True
        if self.management_workers:
            self.management_workers.stopping = True
        if self._closing is None:
            self._closing = asyncio.create_task(
                self._stop(task_mode or self.setting("stop", "task_mode", "cancel"), source)
            )

        async def join():
            return await asyncio.shield(self._closing)

        return await finish_shutdown(join())

    async def _stop(self, mode, source):
        self.stopping = True
        backup_controller = getattr(self, "backup_controller", None)
        if backup_controller:
            await backup_controller.pause()
        management_stop = None
        if self.management_workers:
            self.management_workers.stopping = True
            management_stop = asyncio.create_task(self.management_workers.stop(mode))
        for event in self.ready_events.values():
            event.set()
        if self.lock:
            self.update_lock(operation="stopping")

        async def one(cid):
            row = self.rows[cid]
            child = self.children.get(cid)
            if not child or row["state"] in {"stopped", "blocked", "restart_stopped", "backoff"}:
                self.monitors[cid].cancel()
                row["state"] = "stopped"
                return dict(state="stopped", stopped=[], not_stopped={}, reason=None)
            row["state"] = "stopping"
            try:
                await self.command(child, "stop", task_mode=mode)
                waiter = asyncio.create_task(child.wait())
                if mode == "wait":
                    escalation = asyncio.create_task(self.escalation.wait())
                    try:
                        done, _ = await asyncio.wait(
                            {waiter, escalation}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if waiter not in done:
                            await timed(
                                asyncio.shield(waiter),
                                self.clock,
                                self.setting("stop", "timeout_seconds", 120),
                            )
                    finally:
                        escalation.cancel()
                        await asyncio.gather(escalation, return_exceptions=True)
                else:
                    await timed(
                        asyncio.shield(waiter),
                        self.clock,
                        self.setting("stop", "timeout_seconds", 120),
                    )
                await self.monitors[cid]
                result = self.stop_results.get(
                    cid,
                    dict(
                        state="not_stopped",
                        stopped=[],
                        not_stopped={},
                        reason="process_exited_without_stop_result",
                    ),
                )
                row["state"] = "stopped"
                row["stop_result"] = result
                return result
            except Exception as exc:
                await child.kill()
                await child.wait()
                await self._backup_io_exit_confirmed(cid)
                reason = "timeout_killed" if isinstance(exc, TimeoutError) else "stop_failed"
                self.audit.append("force_stop", cid, reason=reason, source=source)
                self.problems[f"stop_killed:{cid}"] = f"{cid} {row['sending']}"
                row["state"] = "stopped"
                return dict(state="not_stopped", stopped=[], not_stopped={}, reason=reason)

        result = dict(
            zip(self.companions, await asyncio.gather(*(one(cid) for cid in self.companions)))
        )
        await self.notifications.update(self.problems, pending=self.pending_health)
        if management_stop:
            await management_stop
        if backup_controller:
            await backup_controller.shutdown()
        for task in (*self.monitors.values(), self.ticker):
            if task:
                task.cancel()
        await asyncio.gather(
            *self.monitors.values(), *([self.ticker] if self.ticker else []), return_exceptions=True
        )
        if self.safety_notifications:
            await asyncio.gather(*tuple(self.safety_notifications), return_exceptions=True)
        if self.finalize:
            await self.finalize()
        self.audit.append("stop", source=source)
        await self.flush_health()
        if self.lock:
            self.lock.__exit__(None, None, None)
            self.lock = None
        return result
