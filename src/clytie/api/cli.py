"""Management command presentation. Confirmation and authorization remain server policy."""

import argparse
import asyncio
import base64
import getpass
import json
import posixpath
import sys
from pathlib import Path
from urllib.parse import unquote

from clytie.config import resolve_home
from clytie.sdk.immutable import thaw
from clytie.sdk.types import ApiResponse

from .client import Client, NotRunning


def add_parser(commands):
    companion = commands.add_parser("companion")
    actions = companion.add_subparsers(dest="action", required=True)
    companion_leaves = []
    for name in ("start", "stop", "restart"):
        leaf = actions.add_parser(name)
        leaf.add_argument("cid")
        companion_leaves.append(leaf)
    proactive = actions.add_parser("proactive")
    proactive.add_argument("cid")
    proactive.add_argument("proactive_action", choices=("pause", "resume", "status"))
    companion_leaves.append(proactive)
    appearance = actions.add_parser("appearance")
    subcommands = appearance.add_subparsers(dest="appearance_action", required=True)
    for name in ("show", "add", "edit", "remove"):
        leaf = subcommands.add_parser(name)
        leaf.add_argument("cid")
        if name == "add":
            leaf.add_argument("file", type=Path)
            leaf.add_argument("--label", required=True)
            leaf.add_argument("--former", action="store_true")
        if name in ("edit", "remove"):
            leaf.add_argument("number", type=int)
        if name == "edit":
            leaf.add_argument("--label")
            state = leaf.add_mutually_exclusive_group()
            state.add_argument("--current", dest="state", action="store_const", const="current")
            state.add_argument("--former", dest="state", action="store_const", const="former")
        companion_leaves.append(leaf)
    # Keep common flags valid both before and after the companion subcommand.
    for leaf in companion_leaves:
        leaf.add_argument("--home", default=argparse.SUPPRESS)
        leaf.add_argument("--yes", action="store_true", default=argparse.SUPPRESS)
        leaf.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    boundary = commands.add_parser("boundary")
    boundary.add_argument("action", choices=["list", "release"])
    boundary.add_argument("cid")
    boundary.add_argument("actor_id", nargs="?")
    plugin = commands.add_parser("plugin")
    plugin.add_argument("action")
    plugin.add_argument("cid")
    plugin.add_argument("pid", nargs="?")
    plugin.add_argument("plugin_args", nargs="*")
    admin = commands.add_parser("admin")
    admin.add_argument("action", choices=["set-password", "logout-all"])
    backup = commands.add_parser("backup")
    backup.add_argument(
        "action",
        choices=[
            "status",
            "show-key-fingerprint",
            "export-key",
            "verify-key",
            "acknowledge-key",
            "restore",
            "plan",
            "list",
            "create",
            "verify",
            "operation",
            "cancel",
            "memory-plan",
            "config",
            "cleanup",
        ],
    )
    backup.add_argument("value", nargs="?")
    backup.add_argument("restore_id", nargs="?")
    backup.add_argument("plan_id", nargs="?")
    backup.add_argument("--scope", choices=["instance", "memory"], default="instance")
    backup.add_argument("--companion")
    backup.add_argument("--source")
    backup.add_argument("--source-repository")
    backup.add_argument("--repository-id")
    backup.add_argument("--backup-id")
    backup.add_argument("--backup-ids", nargs="+")
    backup.add_argument("--key-path")
    backup.add_argument("--target")
    backup.add_argument("--mode", choices=["new", "merge", "restore"], default="new")
    backup.add_argument("--confirm-identity", action="store_true")
    backup.add_argument("--patch")
    backup.add_argument("--preserved")
    for p in (companion, boundary, plugin, admin, backup):
        p.add_argument("--home")
        p.add_argument("--yes", action="store_true")
        p.add_argument("--json", action="store_true")


class CLI:
    def __init__(self, client, *, yes=False, input_fn=None, password_fn=None):
        self.client, self.yes = client, yes
        self.input, self.password_prompt = input_fn or input, password_fn or getpass.getpass
        self.prefix = False

    async def request(self, method, path, data=None, query=None, *, plugin=False):
        data = dict(data or {})
        if method == "POST" and (
            "/memory/" in path
            or path
            in {
                "/backup/create",
                "/backup/verify",
                "/backup/restore/plan",
                "/backup/restore/apply",
                "/backup/cleanup/plan",
                "/backup/cleanup/apply",
            }
        ):
            from uuid import uuid4

            data.setdefault("operation_id", uuid4().hex)
        if not self.prefix:
            status = await self.client.request("GET", "/api/v1/status")
            silence = (status.json or {}).get("result", {}).get("silence", {})
            if silence.get("on"):
                print("全部静默中（自" + str(silence.get("since")) + "起）")
            self.prefix = True
        response = await self.client.request(
            method, "/api/v1" + path, json=data if method != "GET" else None, query=query
        )
        if response.status == 428 and (
            not plugin or response.json.get("error", {}).get("code") == "confirmation_required"
        ):
            print(response.json.get("result", {}).get("description", ""))
            source = response.json.get("result", {}).get("source_repository")
            if source is not None:
                print("将向所选源仓库登记本次恢复保护，不修改源业务数据：")
                print(json.dumps(source, ensure_ascii=False))
                data["source_repository"] = {
                    key: source[key] for key in ("root", "repository_id", "backup_id")
                }
            if plugin and not sys.stdin.isatty():
                return declined()
            if not self.yes:
                try:
                    approved = self.input("继续？[y/N] ").strip().lower() == "y"
                except EOFError:
                    approved = False
                if not approved:
                    return declined() if plugin else response
            data["confirm"] = True
            response = await self.client.request(method, "/api/v1" + path, json=data, query=query)
        if (
            response.status == 401
            and response.json.get("error", {}).get("code") == "invalid_password"
        ):
            data["admin_password"] = self.password_prompt("管理口令：")
            response = await self.client.request(method, "/api/v1" + path, json=data, query=query)
        return response


async def execute(args, client=None):
    cli = CLI(
        client or Client(resolve_home(args.home)),
        yes=getattr(args, "yes", False) or getattr(args, "confirm", False),
    )
    current = await cli.request("GET", "/status")
    command = args.command
    if command == "backup":
        source_binding = None
        if getattr(args, "source_repository", None):
            source_binding = {
                "root": args.source_repository,
                "backup_id": args.backup_id or args.value,
            }
            if args.repository_id:
                source_binding["repository_id"] = args.repository_id
        if args.action == "list":
            return await cli.request("GET", "/backup/list")
        if args.action == "create":
            data = {"scope": args.scope}
            if args.companion:
                data["companion_id"] = args.companion
            return await cli.request("POST", "/backup/create", data)
        if args.action in {"operation", "cancel", "verify"}:
            if not args.value:
                raise ValueError("需要明确的操作或备份编号")
            if args.action == "verify":
                return await cli.request("POST", "/backup/verify", {"backup_id": args.value})
            return await cli.request(
                "GET" if args.action == "operation" else "POST",
                "/backup/operations/" + args.value + ("/cancel" if args.action == "cancel" else ""),
            )
        if args.action == "config":
            if args.value == "plan" and args.patch:
                return await cli.request(
                    "POST", "/backup/config/plan", {"patch": json.loads(args.patch)}
                )
            if args.value == "apply" and args.restore_id:
                return await cli.request(
                    "POST", "/backup/config/apply", {"plan_id": args.restore_id}
                )
            raise ValueError("config 需要 plan --patch JSON 或 apply 计划编号")
        if args.action == "cleanup":
            if args.value == "plan" and args.backup_ids:
                if args.restore_id or args.preserved:
                    raise ValueError("常规备份选择不能同时指定旧实例或文件路径")
                return await cli.request(
                    "POST",
                    "/backup/cleanup/plan",
                    {
                        "category": "regular",
                        "backup_ids": args.backup_ids,
                    },
                )
            if args.value == "plan" and args.restore_id:
                return await cli.request(
                    "POST",
                    "/backup/cleanup/plan",
                    {
                        "category": "old_instance",
                        "old_id": args.restore_id,
                        "preserved": json.loads(args.preserved) if args.preserved else {},
                    },
                )
            if args.value == "show" and args.restore_id:
                return await cli.request("GET", "/backup/cleanup/plans/" + args.restore_id)
            if args.value == "apply" and args.restore_id:
                return await cli.request(
                    "POST", "/backup/cleanup/apply", {"plan_id": args.restore_id}
                )
            raise ValueError(
                "cleanup 需要 plan --backup-ids 明确编号、plan 旧实例编号、show 或 apply 计划编号"
            )
        if args.action == "memory-plan":
            data = dict(
                backup_id=args.value,
                companion_id=args.companion,
                mode=args.mode,
                confirm_identity=args.confirm_identity,
            )
            if source_binding:
                data.update(source_repository=source_binding, key_path=args.key_path)
            return await cli.request("POST", "/backup/memory/plan", data)
        if args.action == "plan":
            if (
                args.value not in {"show", "cancel"}
                or args.restore_id not in {"instance", "memory"}
                or not args.plan_id
            ):
                raise ValueError("恢复预览需要 show 或 cancel、instance 或 memory 及预览编号")
            path = f"/backup/plans/{args.restore_id}/{args.plan_id}"
            return await cli.request(
                "GET" if args.value == "show" else "POST",
                path + ("/cancel" if args.value == "cancel" else ""),
            )
        if args.action == "status":
            return await cli.request("GET", "/backup/status")
        if args.action == "restore":
            if args.value == "plan":
                data = {"source": args.source, "key_path": args.key_path, "target": args.target}
                if source_binding:
                    data["source_repository"] = source_binding
                return await cli.request("POST", "/backup/restore/plan", data)
            if args.value == "apply" and args.restore_id:
                return await cli.request(
                    "POST", "/backup/restore/apply", {"plan_id": args.restore_id}
                )
            if args.value not in {"source-stopped", "acknowledge"} or not args.restore_id:
                raise ValueError("restore 需要 source-stopped 或 acknowledge 及恢复编号")
            return await cli.request(
                "POST", "/backup/restore/" + args.value, {"restore_id": args.restore_id}
            )
        operation = {
            "show-key-fingerprint": "fingerprint",
            "export-key": "export",
            "verify-key": "verify",
            "acknowledge-key": "acknowledge",
        }[args.action]
        if operation in {"export", "verify"} and not args.value:
            raise ValueError("需要明确的本机文件路径")
        return await cli.request(
            "GET" if operation == "fingerprint" else "POST",
            "/backup/key/" + operation,
            {"path": args.value},
        )
    if command == "supervisor":
        path = (
            "/status"
            if args.action == "status"
            else "/silence"
            if args.action == "silence"
            else "/supervisor/" + args.action
        )
        data = (
            {"on": args.value == "on"}
            if args.action == "silence"
            else {"task_mode": "wait" if args.wait_tasks else "cancel"}
        )
        return await cli.request("GET" if args.action == "status" else "POST", path, data)
    if command == "companion":
        if args.action == "appearance":
            return await appearance_command(args, cli)
        if args.action == "proactive":
            if args.proactive_action == "status":
                if not current.json.get("ok"):
                    return current
                row = current.json["result"]["companions"].get(args.cid)
                if row is None:
                    return ApiResponse(
                        status=404, json={"ok": False, "error": {"code": "not_found"}}
                    )
                return ApiResponse(status=200, json={"ok": True, "result": thaw(row["proactive"])})
            return await cli.request(
                "POST",
                f"/companions/{args.cid}/proactive",
                {"paused": args.proactive_action == "pause"},
            )
        return await cli.request("POST", f"/companions/{args.cid}/{args.action}")
    if command == "boundary":
        return await cli.request(
            "GET" if args.action == "list" else "POST",
            f"/companions/{args.cid}/boundary" + ("/release" if args.action == "release" else ""),
            {"actor_id": args.actor_id},
        )
    if command == "admin":
        if args.action == "logout-all":
            return await cli.request("POST", "/admin/logout-all")
        old = getpass.getpass("旧管理口令（首次设置留空）：")
        new = getpass.getpass("新管理口令：")
        if new != getpass.getpass("再次输入新管理口令："):
            raise ValueError("两次输入不一致")
        return await cli.request(
            "POST", "/admin/password", {"old_password": old, "new_password": new}
        )
    if command == "plugin":
        if args.action not in {"list", "enable", "disable", "uninstall"}:
            return await plugin_command(args, cli)
        path = f"/companions/{args.cid}/plugins"
        if args.action == "list":
            return await cli.request("GET", path)
        plan = await cli.request("POST", path + "/plan", {"op": args.action, "target": args.pid})
        if not plan.json["ok"]:
            return plan
        print(json.dumps(thaw(plan.json["result"]), ensure_ascii=False))
        return await cli.request(
            "POST", path + "/apply", {"plan_id": plan.json["result"]["plan_id"]}
        )
    if command == "task":
        # Query only through API while running; task ids can be ambiguous across companions.
        status = await cli.request("GET", "/status")
        cids = [args.companion] if args.companion else list(status.json["result"]["companions"])
        results = []
        for cid in cids:
            response = await cli.request(
                "GET",
                f"/companions/{cid}/tasks/{args.task_id}",
                query={"full": "1" if args.full else "0"},
            )
            if response.json.get("result", {}).get("ok"):
                results.append(response)
        if len(results) == 1:
            return results[0]

        return ApiResponse(
            status=404,
            json={"ok": False, "error": {"code": "not_found", "message": "任务编号不存在或不唯一"}},
        )
    if command == "memory":
        from clytie.memory.cli import choices, private_text

        action = args.memory_action
        cid = getattr(args, "companion", None)
        if cid is None and action != "import":
            status = await cli.request("GET", "/status")
            cid = next(iter(status.json["result"]["companions"]))
        prefix = f"/companions/{cid}/memory/"
        if action == "thaw":
            return await cli.request("POST", prefix + "thaw", {"record_id": args.record_id})
        if action == "export":
            return await cli.request(
                "POST",
                prefix + "export",
                {
                    "out": args.out,
                    "format": args.format,
                    **({} if args.format == "jsonl" else private_text(False)),
                },
            )
        if action == "recovery-code":
            return await cli.request("POST", prefix + "recovery-code")
        if args.import_action == "plan":
            return await cli.request(
                "POST",
                prefix + "import/plan",
                {
                    "source": args.source,
                    "mode": args.mode,
                    "choices": choices(args.choice),
                    "confirm_identity": args.confirm_identity,
                    "show_content": args.show_content,
                    "importer": args.importer,
                    "approve_proposals": args.approve_proposals,
                    **({} if args.importer else private_text(args.recovery_code)),
                },
            )
        if args.import_action == "undo":
            if not cid:
                status = await cli.request("GET", "/status")
                found = []
                for candidate in status.json["result"]["companions"]:
                    response = await cli.request(
                        "POST",
                        f"/companions/{candidate}/memory/import/undo",
                        {
                            "import_id": args.import_id,
                            "choices": choices(args.choice),
                            "show_content": args.show_content,
                        },
                    )
                    if response.json.get("ok"):
                        found.append(response)
                if len(found) != 1:
                    raise ValueError("请指定 --companion")
                return found[0]
            return await cli.request(
                "POST",
                prefix + "import/undo",
                {
                    "import_id": args.import_id,
                    "choices": choices(args.choice),
                    "show_content": args.show_content,
                },
            )
        meta = await cli.request("GET", "/memory/import/plans/" + args.plan_id)
        if not meta.json["ok"]:
            return meta
        plan = meta.json["result"]
        data = {"plan_id": args.plan_id}
        if plan["kind"] != "undo" and not plan.get("importer"):
            data.update(private_text(args.recovery_code))
        return await cli.request("POST", "/memory/import/apply", data)
    raise ValueError("未知命令")


async def appearance_command(args, cli):
    from clytie.appearance import MAX_BYTES

    path = f"/companions/{args.cid}/appearance"
    action = args.appearance_action
    if action == "show":
        return await cli.request("GET", path)
    if action == "add":
        try:
            with args.file.open("rb") as source:
                raw = source.read(MAX_BYTES + 1)
        except OSError:
            return ApiResponse(
                status=400,
                json={
                    "ok": False,
                    "error": {"code": "file_unreadable", "message": "参考图文件无法读取"},
                },
            )
        if len(raw) > MAX_BYTES:
            return ApiResponse(
                status=413,
                json={
                    "ok": False,
                    "error": {"code": "too_large", "message": "参考图文件超过 20 MiB"},
                },
            )
        data = {
            "content": base64.b64encode(raw).decode("ascii"),
            "label": args.label,
            "state": "former" if args.former else "current",
        }
    else:
        data = {"number": args.number}
        if action == "edit":
            if args.state is not None:
                data["state"] = args.state
            if args.label is not None:
                data["label"] = args.label
            if len(data) == 1:
                raise ValueError("请指定标签或状态")
    return await cli.request("POST", path + "/" + action, data)


def declined():
    return ApiResponse(status=428, json={"ok": False, "error": {"code": "confirmation_declined"}})


class PluginCommandClient:
    def __init__(self, cli):
        self._cli = cli

    async def request(self, method, path, *, json=None, query=None):
        if json is not None and "confirm" in json:
            return ApiResponse(
                status=400, json={"ok": False, "error": {"code": "confirm_not_allowed"}}
            )
        if not path.startswith("/api/v1/"):
            raise ValueError("invalid API path")
        parts = posixpath.normpath(unquote(path)).split("/")
        if len(parts) >= 6 and parts[3] == "companions" and parts[5] == "appearance":
            return ApiResponse(
                status=403, json={"ok": False, "error": {"code": "appearance_owner_only"}}
            )
        return await self._cli.request(method, path[len("/api/v1") :], json, query, plugin=True)


async def plugin_command(args, cli):
    from jsonschema import ValidationError, validate

    from clytie.config import load_home, repository_root
    from clytie.plugin_host.manifests import discover
    from clytie.supervisor.common import read

    client = cli.client
    pid = args.action
    report = load_home(client.home)
    excluded = read(client.home / "plugins/.host/uninstalled.json", [])
    # Resolved plugin configs also include implicitly enabled role bindings.
    enabled = any(
        result["config"]
        .get("plugins", {})
        .get(pid, {})
        .get("enabled", pid in result.get("plugins", {}))
        for result in report.plugin_configs.values()
    )
    if pid in excluded or not enabled:
        raise ValueError("插件未启用")
    manifests = discover(
        repository_root() / "plugins", client.home / "plugins", repo_root=repository_root()
    )
    manifest = manifests[pid]
    declaration = next(
        (x for x in manifest.data["contributes"].get("cli_commands", []) if x["name"] == args.cid),
        None,
    )
    if declaration is None:
        raise ValueError("未知插件命令")
    # CLI arguments are a JSON object matching the declared schema.
    values = json.loads(args.pid or "{}")
    try:
        validate(values, thaw(declaration["args"]))
    except ValidationError:
        raise ValueError("插件命令参数错误") from None
    from clytie.clock import SystemClock
    from clytie.plugin_host import PluginHost

    cls, _ = PluginHost(repository_root(), client.home, clock=SystemClock())._load_class(manifest)
    code = await getattr(cls(), declaration["handler"])(values, PluginCommandClient(cli))
    return ApiResponse(status=200, json={"ok": code == 0, "result": {"exit_code": code}})


def run(args):
    try:
        # Check before prompting for credentials or loading plugin code.
        client = Client(resolve_home(args.home))
        from clytie.supervisor.entry import status

        if not status(client.home)["running"]:
            raise NotRunning()
        response = asyncio.run(execute(args, client))
        output = thaw(response.json)
        if args.command == "task" and output.get("ok"):
            from clytie.cli import display_task

            return display_task(args, output["result"])
        if args.command == "memory" and output.get("ok"):
            output = output["result"]
            if args.memory_action == "recovery-code":
                print("请保留旧恢复码；旧包仍然需要旧恢复码或各自口令。")
                print(output["recovery_code"])
                return 0
            if isinstance(output, dict) and output.get("recovery_code"):
                print("恢复码只显示这一次，请打印或存入密码管理器：")
                print(output.pop("recovery_code"))
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if response.json.get("ok") else 4 if response.status in {401, 403, 428, 429} else 1
    except NotRunning:
        print("守护进程没有运行，请先运行 clytie supervisor start")
        return 3
    except (ValueError, KeyError):
        print("参数错误或插件未启用")
        return 2
