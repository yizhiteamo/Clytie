"""Policy and orchestration for the local API; the CLI contains no business rules."""

import asyncio
import hashlib
from datetime import datetime

from clytie.config import load_home, thaw
from clytie.kernel.query import local_task
from clytie.plugin_host.locking import OperationBusy, file_lock, process_identity
from clytie.plugin_host.snapshots import identifier
from clytie.plugin_host.support import timed
from clytie.supervisor.common import read, write

from .auth import APIError
from .config_write import ConfigWriter
from .management_workers import Workers
from .operations import load_plan


class Management:
    def __init__(self, supervisor, report):
        self.svc, self.report = supervisor, report
        self.home, self.clock = supervisor.home, supervisor.clock
        self.writer = ConfigWriter(self.home, self.clock)
        self.plans, self.unsaved = {}, {}
        self.serial = asyncio.Lock()
        self.api_port = None
        self.operations = set()
        self.memory_workers = Workers(
            self.home, self.clock, timeout=supervisor.setting("stop", "timeout_seconds", 120)
        )
        supervisor.management_workers = self.memory_workers

    def running(self, cid):
        return self.svc.rows.get(cid, {}).get("state") in {"running", "degraded"}

    def check_cid(self, cid):
        identifier(cid)
        if cid not in self.svc.companions:
            raise APIError("not_found", 404)

    def memory_plan(self, data):
        return load_plan(self.home, data.get("plan_id"))

    async def policy(self, route, params, data):
        cid = params.get("cid")
        if cid:
            self.check_cid(cid)
        confirm = False
        needs_password = False
        description = ""
        if route in {"/supervisor/stop", "/silence", "/admin/logout-all"}:
            confirm = True
            description = {
                "/supervisor/stop": "将安全停止所有伴侣及守护进程",
                "/silence": "将"
                + ("开启" if data.get("on") else "解除")
                + "全部静默，影响所有伴侣的对外发送",
                "/admin/logout-all": "将注销所有网页会话",
            }[route]
        elif route in {"/companions/<cid>/stop", "/companions/<cid>/restart"}:
            confirm = True
            count = self.svc.rows.get(cid, {}).get("inflight_tasks", 0)
            mode = data.get("task_mode", self.svc.setting("stop", "task_mode", "cancel"))
            description = f"将{route.rsplit('/', 1)[-1]}伴侣 {cid}，{count} 个任务将" + (
                "等待完成" if mode == "wait" else "被取消"
            )
        elif route == "/companions/<cid>/boundary/release":
            identifier(data.get("actor_id", ""))
            confirm = True
            description = f"将解除伴侣 {cid} 对 {data['actor_id']} 的暂停并清除其边界计数"
        elif route == "/companions/<cid>/memory/recovery-code":
            confirm = needs_password = True
            description = f"将为伴侣 {cid} 生成新的恢复码；旧包仍需要旧恢复码或对应口令"
        elif route == "/memory/import/apply":
            plan = self.memory_plan(data)
            confirm = True
            needs_password = plan["kind"] == "undo" or plan.get("mode") == "restore"
            cid = plan["companion_id"]
            self.check_cid(cid)
            if needs_password and self.running(cid):
                raise APIError("companion_running", 409)
            description = f"将执行伴侣 {cid} 的计划 {plan['id']}，影响 {len(plan['records'])} 条记忆；此操作会写入审计"
        elif route == "/companions/<cid>/plugins/apply":
            plan = self.plans.get(data.get("plan_id"))
            if not plan or plan["companion_id"] != cid:
                raise APIError("plan_stale", 409)
            confirm = plan["operation"] in {"disable", "uninstall"}
            needs_password = plan["operation"] == "uninstall"
            description = (
                f"将对伴侣 {cid} 执行 {plan['operation']} 插件 {plan['target']}；涉及插件："
                + ", ".join(plan["steps"])
            )
            if plan.get("stopped_companions"):
                description += (
                    "；" + ", ".join(plan["stopped_companions"]) + " 下次启动时这个插件不会加载"
                )
        return confirm, needs_password, description

    async def transfer(self, cid, op, args):
        self.check_cid(cid)
        phase = self.svc.rows[cid].get("phase")
        if phase in {"starting", "migrating", "stopping"} and self.svc.rows[cid]["state"] not in {
            "stopped",
            "blocked",
            "restart_stopped",
        }:
            raise APIError("companion_busy", 409)
        return await self.svc.request(
            cid,
            op,
            args,
            timeout=None
            if op == "plugins/apply"
            else 600
            if op
            in {
                "memory/export",
                "memory/import/plan",
                "memory/import/apply",
                "memory/import/undo",
                "memory/import/content",
                "plugins/apply",
            }
            else 30,
        )

    def configuration(self):
        return load_home(self.home)

    async def uninstall_context(self, cid, target):
        report = self.configuration()
        stopped = []
        required = []
        for other in self.svc.companions:
            if other == cid:
                continue
            row = self.svc.rows[other]
            if row["state"] in {"starting", "stopping", "backoff"}:
                raise APIError("companion_busy", 409)
            if self.running(other):
                plugins = await self.transfer(other, "plugins", {})
                if any(p["id"] == target and p["enabled"] for p in plugins):
                    raise APIError("plugin_in_use", 409, {"companions": [other]})
            else:
                config = thaw(report.plugin_configs.get(other, {}).get("config", {}))
                mandatory = {
                    config.get("model", {}).get("plugin"),
                    *(c["plugin"] for c in config.get("channels", [])),
                }
                if target in mandatory:
                    required.append(other)
                elif config.get("plugins", {}).get(target, {}).get("enabled", False):
                    stopped.append(other)
        if required:
            raise APIError("required_by_stopped_companion", 409, {"companions": required})
        return stopped

    def fingerprint(self):
        paths = [
            self.home / "clytie.local.yaml",
            *(self.home / "companions" / cid / "companion.yaml" for cid in self.svc.companions),
        ]
        return hashlib.sha256(b"\0".join(p.read_bytes() for p in paths)).hexdigest()

    async def call(self, route, params, data, query, method):
        cid = params.get("cid")
        if route == "/status":
            self.unsaved = {
                c: generation
                for c, generation in self.unsaved.items()
                if self.svc.rows[c].get("restart_count", 0) == generation
            }
            return {
                "companions": self.svc.rows,
                "silence": read(
                    self.home / "supervisor/silence.json", {"on": False, "since": None}
                ),
                "undelivered": self.svc.notifications.undelivered,
                "api_port": self.api_port,
                "management": {
                    "operations": self.memory_workers.status(),
                    "still_finishing": self.memory_workers.still_finishing,
                },
                "warnings": [
                    {"companion_id": c, "code": "applied_not_saved"} for c in self.unsaved
                ],
            }
        if route == "/companions/<cid>/memory/operations/<operation_id>":
            from .management_journal import Journal

            key = params["operation_id"]
            if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
                raise APIError("invalid_request")
            self.check_cid(cid)
            path = self.home / "companions" / cid / "management/operations" / (key + ".json")
            try:
                return Journal.load(path, self.clock).public()
            except (OSError, ValueError):
                raise APIError("not_found", 404) from None
        if route == "/supervisor/stop":
            mode = data.get("task_mode")
            if mode not in {None, "wait", "cancel"}:
                raise APIError("invalid_request")
            self.svc.request_stop(mode)
            return {"accepted": True}
        if route == "/supervisor/escalate":
            await self.svc.escalate()
            return {}
        if route == "/silence":
            if not isinstance(data.get("on"), bool):
                raise APIError("invalid_request")
            return await self.svc.silence(data["on"])
        if route in {
            "/companions/<cid>/start",
            "/companions/<cid>/stop",
            "/companions/<cid>/restart",
        }:
            async with self.serial:
                action = route.rsplit("/", 1)[-1]
                if action in {"stop", "restart"}:
                    mode = data.get("task_mode")
                    if mode not in {None, "wait", "cancel"}:
                        raise APIError("invalid_request")
                    result = await self.svc.stop_companion(cid, mode)
                if action in {"start", "restart"}:
                    await self.svc.start_companion(cid)
                    self.unsaved.pop(cid, None)
                    result = {"accepted": True}
                return result
        if route == "/companions/<cid>/tasks/<task_id>":
            if self.running(cid):
                return await self.transfer(
                    cid, "tasks", {"task_id": params["task_id"], "full": query.get("full") == "1"}
                )
            return local_task(
                params["task_id"], home=self.home, companion_id=cid, full=query.get("full") == "1"
            )
        if route.startswith("/memory/import/plans/"):
            plan = load_plan(self.home, params["plan_id"])
            if method == "GET":
                return plan
            cid = plan["companion_id"]
            op = "import/content"
            data = {**data, "plan_id": plan["id"]}
        elif route == "/memory/import/apply":
            plan = self.memory_plan(data)
            cid = plan["companion_id"]
            op = "import/apply"
            data = {
                k: v
                for k, v in data.items()
                if k in {"plan_id", "passphrase", "recovery_code", "operation_id"}
            }
        elif "/memory/" in route:
            op = route.split("/memory/", 1)[1]
        else:
            op = None
        if op:
            self.check_cid(cid)
            key = data.pop("operation_id", None)
            if self.running(cid):
                if op == "import/plan" and data.get("mode") == "restore":
                    raise APIError("companion_running", 409)
                if op == "import/undo":
                    raise APIError("companion_running", 409)

                async def online(journal, arguments, control):
                    def cancel():
                        child = self.svc.children.get(cid)
                        if child is not None:
                            task = asyncio.create_task(
                                self.svc.command(
                                    child, "management_cancel", operation_id=journal.value["id"]
                                )
                            )
                            task.add_done_callback(
                                lambda done: done.exception() if not done.cancelled() else None
                            )

                    control(cancel)
                    return await self.svc.request(
                        cid,
                        "managed_memory/" + op,
                        {"operation_id": journal.value["id"], "arguments": arguments},
                        timeout=None,
                    )

                return await self.memory_workers.execute(cid, op, data, key, runner=online)
            if self.svc.rows[cid]["state"] in {"starting", "stopping", "backoff"}:
                raise APIError("companion_busy", 409)
            return await self.memory_workers.execute(cid, op, data, key)
        if route in {"/companions/<cid>/boundary", "/companions/<cid>/boundary/release"}:
            op = "boundary/release" if method == "POST" else "boundary"
            if self.running(cid):
                return await self.transfer(cid, op, data)
            if self.svc.rows[cid]["state"] in {"starting", "stopping", "backoff"}:
                raise APIError("companion_busy", 409)
            path = self.home / "companions" / cid / "boundary/state.json"
            state = read(path, {"actors": {}, "records": []})
            if method == "POST":
                row = state["actors"].get(data["actor_id"])
                if row:
                    row["counts"] = {}
                    row.pop("until", None)
                write(path, state)
                return {"released": data["actor_id"]}
            return [
                {"actor_id": a, "nickname": None, "until": r["until"]}
                for a, r in state["actors"].items()
                if r.get("until") and datetime.fromisoformat(r["until"]) > self.clock.now()
            ]
        if "<subpath>" in route:
            if not self.running(cid):
                raise APIError("plugin_unavailable", 404)
            return await self.transfer(
                cid, "plugin_route", {**params, "method": method, "query": query, "json": data}
            )
        if route.endswith("/panels"):
            if not self.running(cid):
                raise APIError("plugin_unavailable", 404)
            return await self.transfer(cid, "panels", {})
        if "/plugins" in route:
            if not self.running(cid):
                raise APIError("companion_not_running", 409)
            operation = asyncio.create_task(self.plugin_serial(route, cid, data))
            self.operations.add(operation)

            def finished(task):
                self.operations.discard(task)
                if not task.cancelled():
                    task.exception()

            operation.add_done_callback(finished)
            try:
                return await timed(asyncio.shield(operation), self.clock, 600)
            except TimeoutError:
                raise APIError("companion_timeout", 504) from None
        raise APIError("not_found", 404)

    async def plugin_serial(self, route, cid, data):
        async with self.serial:
            lock = file_lock(
                self.home / "plugins/.host/operations.lock",
                {**process_identity(), "operation": "api_plugin_operation"},
            )
            try:
                lock.__enter__()
            except OperationBusy:
                raise APIError("operation_in_progress", 409) from None
            try:
                return await self.plugin_call(route, cid, data)
            finally:
                lock.__exit__(None, None, None)

    async def plugin_call(self, route, cid, data):
        if route.endswith("/plugins"):
            return await self.transfer(cid, "plugins", {})
        if route.endswith("/plan"):
            stopped = (
                await self.uninstall_context(cid, data["target"])
                if data.get("op") == "uninstall"
                else []
            )
            plan = await self.transfer(cid, "plugins/plan", data)
            plan.update(stopped_companions=stopped, configuration=self.fingerprint())
            self.plans[plan["plan_id"]] = plan
            return plan
        plan = self.plans.get(data.get("plan_id"))
        if not plan or plan["configuration"] != self.fingerprint():
            raise APIError("plan_stale", 409)
        if (
            plan["operation"] == "uninstall"
            and await self.uninstall_context(cid, plan["target"]) != plan["stopped_companions"]
        ):
            raise APIError("plan_stale", 409)
        patch = {
            "plugins": {pid: {"enabled": plan["operation"] == "enable"} for pid in plan["steps"]}
        }
        self.writer.preview(cid, patch)
        result = await self.transfer(
            cid, "plugins/apply", {"plan_id": plan["plan_id"], "coordinated": True}
        )
        if result.get("patch"):
            try:
                self.writer.save(cid, result["patch"])
            except (APIError, OSError) as exc:
                self.unsaved[cid] = self.svc.rows[cid].get("restart_count", 0)
                raise APIError(
                    "applied_not_saved",
                    409,
                    {
                        "cause": getattr(exc, "code", "config_write_failed"),
                        "patch": result["patch"],
                        "description": "已生效，但没有保存，下次启动会恢复原状",
                    },
                ) from None
            self.unsaved.pop(cid, None)
        return result
