"""Policy and orchestration for the local API; the CLI contains no business rules."""

import asyncio
import hashlib
import re
from datetime import datetime

from clytie.config import load_home, thaw
from clytie.kernel.query import local_task
from clytie.plugin_host.locking import OperationBusy, file_lock, process_identity
from clytie.plugin_host.snapshots import identifier
from clytie.plugin_host.support import timed
from clytie.supervisor.common import read, write

from .auth import APIError, RequestNotDelivered
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
        from clytie.backup.controller import bind
        from clytie.backup.workers import BackupWorkers

        self.backup_workers = BackupWorkers(
            self.home, self.clock, timeout=supervisor.setting("stop", "timeout_seconds", 120)
        )
        bind(supervisor, self.backup_workers, memory_workers=self.memory_workers)

    def running(self, cid):
        return self.svc.rows.get(cid, {}).get("state") in {"running", "degraded"}

    def check_cid(self, cid):
        identifier(cid)
        if cid not in self.svc.companions:
            raise APIError("not_found", 404)

    def memory_plan(self, data):
        return load_plan(self.home, data.get("plan_id"))

    async def backup_operation(self, route, params, data):
        from clytie.backup.previews import PreviewError
        from clytie.backup.repository import RepositoryError
        from clytie.backup.restore import RestoreError
        from clytie.backup.storage import BackupStorage
        from clytie.backup.workers import WorkerError

        def opaque(value):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                raise APIError("invalid_request")
            return value

        try:
            if route == "/backup/list":
                return {
                    "backups": await asyncio.to_thread(BackupStorage(self.home, self.clock).list)
                }
            if "/operations/" in route:
                key = params["operation_id"]
                if route.endswith("/cancel"):
                    await self.backup_workers.cancel(key)
                return self.backup_workers.query(key)
            if route == "/backup/create":
                scope = data.get("scope", "instance")
                if scope not in {"memory", "instance"}:
                    raise APIError("invalid_request")
                cid = data.get("companion_id")
                if scope == "memory":
                    self.check_cid(cid)
                elif cid is not None:
                    raise APIError("invalid_request")
                operation, args = "create", dict(scope=scope, companion_id=cid, manual=True)
            elif route == "/backup/verify":
                opaque(data.get("backup_id"))
                operation, args = "verify", {"backup_id": data["backup_id"]}
            elif route == "/backup/restore/plan":
                if any(
                    not isinstance(data.get(k), str) or not data[k]
                    for k in ("source", "key_path", "target")
                ):
                    raise APIError("invalid_request")
                operation = "restore_plan"
                args = {k: data[k] for k in ("source", "key_path", "target")}
                if "source_repository" in data:
                    args.update(
                        source_repository=data["source_repository"],
                        source_repository_confirmed=data.get("_source_repository_confirmed")
                        is True,
                    )
            elif route == "/backup/cleanup/plan":
                category = data.get("category", "regular" if "backup_ids" in data else None)
                if category == "regular":
                    ids = data.get("backup_ids")
                    if (
                        not isinstance(ids, list)
                        or not ids
                        or not all(isinstance(v, str) for v in ids)
                    ):
                        raise APIError("invalid_request")
                    if len(ids) != len(set(ids)):
                        raise APIError("invalid_request")
                    for value in ids:
                        opaque(value)
                    if set(data) - {"category", "backup_ids", "operation_id"}:
                        raise APIError("invalid_request")
                    args = {"category": "regular", "backup_ids": ids}
                elif category == "old_instance":
                    if not isinstance(data.get("preserved", {}), dict):
                        raise APIError("invalid_request")
                    opaque(data.get("old_id"))
                    args = {
                        "category": "old_instance",
                        "old_id": data["old_id"],
                        "preserved": data.get("preserved", {}),
                    }
                else:
                    raise APIError("invalid_request")
                operation = "cleanup_plan"
            else:
                opaque(data.get("plan_id"))
                operation = "cleanup_apply" if route == "/backup/cleanup/apply" else "restore_apply"
                if operation == "cleanup_apply" and set(data) - {"plan_id", "operation_id"}:
                    raise APIError("invalid_request")
                args = {"plan_id": data["plan_id"]}
            return await self.backup_workers.execute(operation, args, data.get("operation_id"))
        except (WorkerError, PreviewError, RepositoryError, RestoreError) as exc:
            raise APIError(exc.code, 409) from None
        except (OSError, ValueError, KeyError, TypeError):
            raise APIError("backup_request_unavailable", 409) from None

    async def backup_configuration(self, action, data):
        from clytie.backup.configuration import BackupConfigWriter

        if not hasattr(self, "_backup_config_writer"):
            self._backup_config_writer = BackupConfigWriter(self.home, self.clock)
        writer = self._backup_config_writer

        def execute():
            if action == "plan":
                if not isinstance(data.get("patch"), dict):
                    raise APIError("invalid_request")
                return writer.plan(data["patch"])
            if not isinstance(data.get("plan_id"), str):
                raise APIError("invalid_request")
            if action == "read":
                import json

                from clytie.plugin_host.snapshots import atomic_read

                record = json.loads(atomic_read(writer._plan_path(data["plan_id"])))
                if type(record.get("requires_confirmation")) is not bool:
                    raise ValueError("plan_stale")
                return record
            return writer.apply(
                data["plan_id"], confirmed=data.get("_backup_config_confirmed") is True
            )

        try:
            if action == "read":
                return await asyncio.to_thread(execute)
            operation = asyncio.create_task(asyncio.to_thread(execute))
            self.operations.add(operation)

            def finished(task):
                self.operations.discard(task)
                if not task.cancelled():
                    task.exception()

            operation.add_done_callback(finished)
            result = await asyncio.shield(operation)
            if action == "apply" and result.get("saved"):
                controller = getattr(self.svc, "backup_controller", None)
                if controller is not None:
                    from zoneinfo import ZoneInfo

                    instance = thaw((await asyncio.to_thread(self.configuration)).instance)
                    controller.scheduler.config = instance.get("backup", {})
                    controller.scheduler.timezone = ZoneInfo(instance.get("timezone", "UTC"))
            return result
        except OperationBusy:
            raise APIError("operation_in_progress", 409) from None
        except OSError:
            raise APIError("config_unavailable", 409) from None
        except (ValueError, TypeError, KeyError) as exc:
            allowed = {
                "plan_stale",
                "invalid_plan_id",
                "config_invalid",
                "config_not_writable",
                "config_invalid_after_write",
                "config_changed_during_validation",
                "confirmation_required",
            }
            code = str(exc) if str(exc) in allowed else "config_not_writable"
            raise APIError(code, 428 if code == "confirmation_required" else 409) from None

    async def policy(self, route, params, data):
        if (
            getattr(self.svc, "maintenance", False)
            and route
            not in {
                "/status",
                "/supervisor/stop",
                "/supervisor/escalate",
                "/auth/logout",
                "/auth/reauth",
                "/admin/password",
                "/admin/logout-all",
            }
            and not route.startswith(("/restore/", "/backup/", "/key/", "/old-instances/"))
        ):
            raise APIError("maintenance_mode", 409)
        cid = params.get("cid")
        if cid:
            self.check_cid(cid)
        confirm = False
        needs_password = False
        description = ""
        if route in {"/backup/memory/plan", "/backup/restore/plan"} and "source_repository" in data:
            from clytie.backup.repository import RepositoryError, SourceRepository

            binding = data["source_repository"]
            if not isinstance(binding, dict) or not {"root", "backup_id"} <= set(binding):
                raise APIError("invalid_request")
            if set(binding) - {"root", "repository_id", "backup_id"}:
                raise APIError("invalid_request")
            try:
                await asyncio.to_thread(
                    lambda: SourceRepository(binding["root"], self.clock).binding(
                        binding,
                        confirmed=data.get("confirm") is True,
                        source=data.get("source"),
                    )
                )
            except RepositoryError as exc:
                raise APIError(
                    exc.code,
                    428 if exc.code == "source_repository_confirmation_required" else 409,
                    {"source_repository": exc.binding} if exc.binding else None,
                ) from None
            except OSError:
                raise APIError("source_repository_unavailable", 409) from None
        if route == "/backup/cleanup/apply":
            confirm = needs_password = True
            description = "仅删除已确认计划中的合格文件；最低恢复点与活跃或未知引用始终保护，逐项记录已删除、受阻和失败，不替换候选"
        elif route == "/backup/restore/apply":
            confirm = needs_password = True
            description = "将备份恢复到预览指定的新空目标；不覆盖当前实例，不自动上线"
        elif route == "/backup/operations/<operation_id>/cancel":
            confirm = True
            description = "请求协作取消备份操作；已经提交的产物保留，必要核验和恢复会继续"
        elif route == "/backup/plans/<kind>/<plan_id>/cancel":
            confirm = True
            description = "此恢复预览将失效，源备份不再受本预览保护，之后可能被正常轮转删除。"
        elif route == "/backup/config/apply":
            plan = await self.backup_configuration("read", data)
            confirm = plan["requires_confirmation"]
            description = "修改实例备份配置；保留调整在后续成功自动备份生效，不立即删除备份"
        elif route == "/backup/key/export":
            confirm = needs_password = True
            description = "将实例备份密钥写入指定的新文件；请单独保管，已有文件不会被覆盖"
        elif route in {"/backup/restore/source-stopped", "/backup/restore/acknowledge"}:
            confirm = True
            needs_password = route == "/backup/restore/source-stopped"
            description = (
                "确认来源实例已经停用；之后可以按正常配置校验启动此恢复目标，不会自动启动"
                if needs_password
                else "登记此目标已正常启动并完成切换；旧实例仍保留，不自动删除"
            )
        elif route in {"/supervisor/stop", "/silence", "/admin/logout-all"}:
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

    def configuration_status(self):
        # Revalidate current bytes, not the daemon's startup report. No credentials
        # or error.actual values are returned, and no business plugins are loaded.
        report = self.configuration()

        def field(error):
            from pathlib import Path

            try:
                file = Path(error.file).resolve().relative_to(self.home.resolve()).as_posix()
            except ValueError:
                file = "plugin_schema"
            return {"file": file, "pointer": error.pointer, "code": error.code or "config_invalid"}

        return {
            "valid": report.valid,
            "global_errors": [field(e) for e in report.global_errors],
            "errors": [field(e) for e in report.errors],
            "companions": [{"id": c["id"], "valid": c["valid"]} for c in report.companions],
        }

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
        if route == "/backup/cleanup/plans/<plan_id>":
            from clytie.backup.cleanup import CleanupService

            try:
                result = await asyncio.to_thread(
                    CleanupService(self.home, self.clock).operation, params["plan_id"]
                )
                if result.get("category") == "regular":
                    return {
                        **{
                            k: result[k]
                            for k in (
                                "id",
                                "category",
                                "state",
                                "backup_ids",
                                "eligible_ids",
                                "estimated_freed_bytes",
                                "groups_after",
                                "created_at",
                                "reason",
                            )
                            if k in result
                        },
                        "items": [
                            {k: v for k, v in row.items() if k != "record"}
                            for row in result.get("items", [])
                        ],
                    }
                return result
            except (OSError, ValueError, KeyError):
                raise APIError("cleanup_plan_unavailable", 409) from None
        if route in {
            "/backup/list",
            "/backup/create",
            "/backup/verify",
            "/backup/restore/plan",
            "/backup/restore/apply",
            "/backup/cleanup/plan",
            "/backup/cleanup/apply",
            "/backup/operations/<operation_id>",
            "/backup/operations/<operation_id>/cancel",
        }:
            return await self.backup_operation(route, params, data)
        if route.startswith("/backup/plans/"):
            from clytie.backup.previews import PreviewCatalog, PreviewError

            catalog = PreviewCatalog(self.home, self.clock)
            action = catalog.cancel if method == "POST" else catalog.get
            try:
                value = await asyncio.to_thread(action, params["kind"], params["plan_id"])
            except PreviewError as exc:
                raise APIError(
                    exc.code,
                    404 if exc.code == "plan_not_found" else 409,
                    {"operation_id": exc.operation_id} if exc.operation_id else None,
                ) from None
            return {
                key: value.get(key)
                for key in ("kind", "plan_id", "state", "created_at", "expires_at", "operation_id")
            }
        cid = params.get("cid")
        if route in {"/backup/config/plan", "/backup/config/apply"}:
            return await self.backup_configuration(route.rsplit("/", 1)[-1], data)
        if route.startswith("/backup/key/"):
            from clytie.backup.keys import BackupKeys, KeyErrorCode
            from clytie.config import repository_root

            report = self.configuration()
            mirror = (report.instance or {}).get("backup", {}).get("mirror", {}).get("path")
            keys = BackupKeys(self.home, self.clock, repository=repository_root(), mirror=mirror)
            operation = route.rsplit("/", 1)[-1]
            try:
                if operation == "fingerprint":
                    return keys.status()
                if operation == "acknowledge":
                    return keys.acknowledge()
                if not isinstance(data.get("path"), str) or not data["path"]:
                    raise APIError("invalid_request")
                return getattr(keys, operation)(data["path"])
            except KeyErrorCode as exc:
                raise APIError(str(exc), 409) from None
            except OSError:
                raise APIError("key_file_unavailable", 409) from None
        if route in {"/backup/restore/source-stopped", "/backup/restore/acknowledge"}:
            from clytie.backup.restore import RestoreError, acknowledge, source_stopped

            restore_id = data.get("restore_id")
            if not isinstance(restore_id, str):
                raise APIError("invalid_request")
            try:
                if route == "/backup/restore/source-stopped":
                    if not getattr(self.svc, "maintenance", False):
                        raise APIError("maintenance_required", 409)
                    result = source_stopped(self.home, restore_id, clock=self.clock)
                else:
                    result = acknowledge(
                        self.home,
                        restore_id,
                        clock=self.clock,
                        runtime_check=lambda: {
                            "home": str(self.home.resolve()),
                            "mode": "maintenance"
                            if getattr(self.svc, "maintenance", False)
                            else "normal",
                            "running": not self.svc.stopping
                            and bool(self.svc.rows)
                            and any(
                                r["state"] in {"running", "degraded"}
                                for r in self.svc.rows.values()
                            ),
                            "configuration_valid": not self.configuration().global_errors,
                            "restore_id": restore_id,
                        },
                    )
                    if hasattr(self.svc, "refresh_old_instances"):
                        await self.svc.refresh_old_instances()
                        await self.svc.notifications.update(
                            self.svc.problems, pending=self.svc.pending_health
                        )
            except RestoreError as exc:
                raise APIError(exc.code, 409) from None
            return {
                key: result.get(key)
                for key in ("restore_id", "data_state", "source_stopped", "switched")
            }
        if route == "/backup/status":
            from clytie.backup.keys import BackupKeys
            from clytie.backup.previews import PreviewCatalog
            from clytie.backup.restore import RestoreError, read_state
            from clytie.backup.space import space_status
            from clytie.backup.status import storage_categories
            from clytie.backup.storage import BackupStorage
            from clytie.backup.temporary import temporary_status
            from clytie.config import repository_root

            try:
                state = read_state(self.home)
            except (RestoreError, OSError):
                state = {"data_state": "recovery_blocked"}
            configuration = self.configuration_status()
            errors = {(e["file"], e["pointer"]) for e in configuration["errors"]}
            missing = [
                e
                for e in (state or {}).get("credential_exclusions", [])
                if (e["file"], e["pointer"]) in errors
            ]
            try:
                primary = await asyncio.to_thread(BackupStorage(self.home, self.clock).status)
            except (OSError, ValueError, KeyError):
                primary = {"state": "unavailable", "reason": "backup_index_unverified"}
            try:
                temporary = await asyncio.to_thread(temporary_status, self.home)
            except (OSError, ValueError, KeyError):
                temporary = {"state": "unavailable", "reason": "temporary_registry_unverified"}
            try:
                protection = await asyncio.to_thread(PreviewCatalog(self.home, self.clock).status)
            except (OSError, ValueError, KeyError):
                protection = {"state": "unavailable", "reason": "source_protection_unverified"}
            audit = None
            if hasattr(getattr(self.svc, "audit", None), "writer"):
                try:
                    full_audit = await asyncio.to_thread(self.svc.audit.writer.status)
                    audit = {
                        key: full_audit[key]
                        for key in (
                            "current_bytes",
                            "archive_bytes",
                            "untracked_archives",
                        )
                    }
                except (OSError, ValueError, KeyError):
                    audit = {"state": "unavailable"}
            try:
                from clytie.backup.configuration import BackupConfigWriter

                mirror = await asyncio.to_thread(
                    lambda: BackupConfigWriter(self.home, self.clock)._mirror_manager().status()
                )
            except (OSError, ValueError, KeyError):
                mirror = {"status": "unavailable"}
            try:
                space_estimates = await asyncio.to_thread(space_status, self.home)
            except (OSError, ValueError, KeyError):
                space_estimates = {"state": "unavailable", "reason": "space_evidence_unverified"}
            return {
                "space_estimates": space_estimates,
                "key": await asyncio.to_thread(
                    BackupKeys(self.home, self.clock, repository=repository_root()).status
                ),
                "storage_categories": await asyncio.to_thread(storage_categories, self.home),
                "audit": audit,
                "mirror": mirror,
                "old_instances": getattr(self.svc, "old_instances_summary", None),
                "protection": protection,
                "primary": primary,
                "temporary": temporary,
                "operations": self.backup_workers.status(),
                "still_finishing": self.backup_workers.still_finishing,
                "mode": "maintenance" if getattr(self.svc, "maintenance", False) else "normal",
                "data_state": state.get("data_state") if state else None,
                "credential_exclusions": missing,
                "configuration": configuration,
                "message": "数据恢复完成，尚需配置凭据"
                if missing and state.get("data_state") == "ready_for_switch"
                else None,
                "source_stopped": state.get("source_stopped", False) if state else False,
            }
        if route == "/status":
            self.unsaved = {
                c: generation
                for c, generation in self.unsaved.items()
                if self.svc.rows[c].get("restart_count", 0) == generation
            }
            return {
                "mode": "maintenance" if getattr(self.svc, "maintenance", False) else "normal",
                "configuration": self.configuration_status(),
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
            return await asyncio.to_thread(
                local_task,
                params["task_id"],
                home=self.home,
                companion_id=cid,
                full=query.get("full") == "1",
            )
        backup_apply = None
        if route == "/backup/memory/plan":
            cid = data.get("companion_id")
            self.check_cid(cid)
            op = "backup/plan"
            data = {
                k: v
                for k, v in data.items()
                if k
                in {
                    "backup_id",
                    "mode",
                    "choices",
                    "confirm_identity",
                    "operation_id",
                    "source_repository",
                    "key_path",
                    "_source_repository_confirmed",
                }
            }
        elif route.startswith("/memory/import/plans/"):
            plan = load_plan(self.home, params["plan_id"])
            if method == "GET":
                if plan.get("source_type") == "backup":
                    from clytie.backup.previews import PreviewCatalog

                    return {
                        **plan,
                        "lifecycle": await asyncio.to_thread(
                            PreviewCatalog(self.home, self.clock).get, "memory", plan["id"]
                        ),
                    }
                return plan
            cid = plan["companion_id"]
            op = "import/content"
            data = {**data, "plan_id": plan["id"]}
        elif route == "/memory/import/apply":
            plan = self.memory_plan(data)
            if plan.get("source_type") == "backup":
                backup_apply = plan
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
            if backup_apply is not None:
                from clytie.backup.previews import PreviewCatalog, PreviewError

                catalog = PreviewCatalog(self.home, self.clock)
                try:
                    lifecycle = await asyncio.to_thread(catalog.get, "memory", backup_apply["id"])
                    key = lifecycle["operation_id"] or backup_apply["id"]
                    if lifecycle["state"] not in {"ready", "consumed"}:
                        raise PreviewError("plan_" + lifecycle["state"])
                except PreviewError as exc:
                    raise APIError(exc.code, 404 if exc.code == "plan_not_found" else 409) from None
            if self.running(cid):
                if op in {"import/plan", "backup/plan"} and data.get("mode") == "restore":
                    raise APIError("companion_running", 409)
                if op == "import/undo":
                    raise APIError("companion_running", 409)

                async def online(journal, arguments, control):
                    def dispatch(child):
                        try:
                            identity = child.identity()
                        except OSError:
                            raise RequestNotDelivered("companion_busy", 409) from None
                        # Persist the intended receiver before transport. An uncertain
                        # send must not leave the still-live daemon recorded as writer.
                        journal.value["executor"] = identity
                        journal.save()

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
                    try:
                        return await self.svc.request(
                            cid,
                            "managed_memory/" + op,
                            {"operation_id": journal.value["id"], "arguments": arguments},
                            timeout=None,
                            before_send=dispatch,
                        )
                    except RequestNotDelivered:
                        journal.phase("failed")
                        raise

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
