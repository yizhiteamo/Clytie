"""Management operations shared by the daemon and the owning companion process."""

import json
import re
from dataclasses import asdict

from clytie.api.auth import APIError
from clytie.api.memory import MemoryAPI
from clytie.backup.coordination import run_sync
from clytie.plugin_host.snapshots import contained, identifier
from clytie.plugin_host.support import timed


def load_plan(home, plan_id):
    if (
        not isinstance(plan_id, str)
        or len(plan_id) != 32
        or any(c not in "0123456789abcdef" for c in plan_id)
    ):
        raise APIError("invalid_request")
    path = contained(home, home / "run/memory-plans" / (plan_id + ".json"))
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        raise APIError("plan_stale", 409) from None


async def memory_operation(home, clock, cid, op, args, hosted=None):
    api = MemoryAPI(home, clock=clock, hosted=hosted)
    if op == "export":
        return await api.export_online(cid, **args)
    if op == "thaw":
        return await api.command(cid, api.thaw_record, args["record_id"])
    if op == "recovery-code":
        return {"recovery_code": await api.command(cid, api.new_recovery_code, confirmed=True)}
    if op == "import/plan":
        return await api.plan(cid, **args)
    if op == "backup/plan":
        return await api.backup_plan(cid, **args)
    if op == "import/undo":
        return await api.command(cid, api.undo, **args)
    if op == "import/apply":
        plan = await api.file_command(cid, load_plan, home, args["plan_id"])
        if plan["companion_id"] != cid:
            raise APIError("plan_stale", 409)
        return {"import_id": await api.apply(**args)}
    if op == "import/content":
        plan = await api.file_command(cid, load_plan, home, args["plan_id"])
        if plan["companion_id"] != cid:
            raise APIError("plan_stale", 409)
        if plan["kind"] == "undo":
            return await api.command(
                cid, api.undo, plan["undo"], choices=plan.get("choices"), show_content=True
            )
        if plan.get("source_type") == "backup":
            return await api.backup_content(plan)
        return await api.plan(
            cid,
            plan["source"],
            mode=plan["mode"],
            choices=plan["choices"],
            confirm_identity=plan["confirm_identity"],
            importer=plan["importer"],
            approve_proposals=plan["approve_proposals"],
            show_content=True,
            **{k: v for k, v in args.items() if k in {"passphrase", "recovery_code"}},
        )
    raise APIError("not_found", 404)


def owned_task(ledger, task_id, *, full=False):
    from clytie.kernel.query import task_view

    def error(code, message):
        return dict(ok=False, error=dict(code=code, message=message, companions=[]), warnings=[])

    if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{8}", task_id):
        return error("invalid_request", "task_id must be eight lowercase hexadecimal characters")
    records = [row for row in ledger.rows("record") if row["task_id"] == task_id]
    if not records:
        return error("not_found", "task not found")
    return dict(ok=True, task=task_view(ledger, records[0], full=full), warnings=[])


async def companion_operation(runtime, op, args):
    home, clock, cid, host = runtime.home, runtime.clock, runtime.cid, runtime.host
    if op == "upload/start":
        pid = args["plugin"]
        if not host.is_active(cid, pid) or "uploads" not in host.runtimes[cid, pid].manifest.data:
            raise APIError("plugin_unavailable", 404)
        maximum = host.runtimes[cid, pid].manifest.data["uploads"]["max_bytes"]
        return await host.uploads(cid).start(pid, args["size"], args["sha256"], maximum)
    if op == "upload/chunk":
        return await host.uploads(cid).chunk(args["upload_id"], args["sequence"], args["content"])
    if op == "upload/complete":
        return await host.uploads(cid).complete(args["upload_id"])
    if op == "proactive":
        from clytie.backup.coordination import CaptureError

        try:
            return await runtime.agent.proactive_pause.set(args["paused"])
        except CaptureError as exc:
            raise APIError(exc.code, 409) from None
    if op == "backup_capture":
        from clytie.backup.controller import capture_request

        return await capture_request(runtime, args)
    if op.startswith("managed_memory/"):
        from .management_worker import execute

        return await execute(
            home, cid, args["operation_id"], args["arguments"], clock=clock, hosted=runtime
        )
    if op.startswith("memory/"):
        return await memory_operation(home, clock, cid, op.removeprefix("memory/"), args, runtime)
    if op == "smoke/records":
        from clytie.smoke.summary import records

        ledger = runtime.agent.ledger
        return await run_sync(ledger, lambda: records(ledger, args["conversation"], args["since"]))
    if op == "tasks":
        return await run_sync(
            runtime.agent.ledger,
            lambda: owned_task(runtime.agent.ledger, args["task_id"], full=args.get("full", False)),
        )
    if op == "boundary":
        from datetime import datetime

        return [
            {
                "actor_id": actor,
                "nickname": runtime.agent.actor_names.get(actor),
                "until": row["until"],
            }
            for actor, row in runtime.agent.boundary.state["actors"].items()
            if row.get("until") and datetime.fromisoformat(row["until"]) > clock.now()
        ]
    if op == "boundary/release":
        identifier(args["actor_id"])
        await runtime.agent.boundary.release(cid, args["actor_id"])
        return {"released": args["actor_id"]}
    if op == "plugins":
        host.refresh()
        return [
            {
                "id": pid,
                "version": m.data["version"],
                "enabled": host.is_active(cid, pid),
                "reason": host.states.get(cid, {}).get(pid, {}).get("status"),
                "backup": host.backup_support(cid, pid),
            }
            for pid, m in host.manifests.items()
        ]
    if op == "plugins/plan":
        operation = args["op"]
        if operation == "enable":
            plan = host.plan_enable(
                cid, args["target"], restore_cascaded=args.get("restore_cascaded", False)
            )
        elif operation in {"disable", "uninstall"}:
            plan = getattr(host, "plan_" + operation)(cid, args["target"])
        else:
            raise APIError("invalid_request")
        return asdict(plan)
    if op == "plugins/apply":
        from clytie.plugin_host.host import PlanStale

        try:
            return await host.apply(args["plan_id"], coordinated=args.get("coordinated", False))
        except PlanStale:
            raise APIError("plan_stale", 409) from None
    if op == "panels":
        return [dict(r.declaration) for r in host.registrations(cid, "webui_panel")]
    if op == "config_proposal":
        pid = args["pid"]
        if not host.is_active(cid, pid):
            raise APIError("plugin_unavailable", 404)
        layouts = host.unit_configs.get((cid, pid), [])
        account = None
        if any(unit["role"] == "channel" for unit in layouts):
            if (
                len(layouts) != 1
                or layouts[0]["role"] != "channel"
                or not layouts[0].get("account")
            ):
                raise APIError("config_binding_mismatch", 409)
            account = layouts[0]["account"]
        elif pid not in host.settings.get(cid, {}).get("plugins", {}):
            raise APIError("config_binding_mismatch", 409)
        manifest = host.runtimes[cid, pid].manifest
        writable = set(manifest.data.get("config_writable", ()))
        if not writable:
            raise APIError("config_not_writable")
        response = await plugin_route(
            host,
            cid,
            clock,
            {
                "pid": pid,
                "subpath": "config/proposal",
                "method": "GET",
                "query": {"session": args["session"]},
                "json": {},
            },
        )
        body = response["json"]
        if response["status"] != 200:
            raise APIError("proposal_unavailable", 409)
        if (
            not isinstance(body, dict)
            or set(body) != {"changes"}
            or not isinstance(body["changes"], dict)
        ):
            raise APIError("plugin_invalid_response", 502)
        changes = body["changes"]
        if not changes or changes.keys() - writable:
            raise APIError("config_not_writable")
        return {"pid": pid, "account": account, "changes": changes}
    if op == "plugin_route_policy":
        declaration = registered_route(host, cid, args).declaration
        return {key: declaration.get(key, False) for key in ("confirm", "admin_password")}
    if op == "plugin_route":
        return await plugin_route(host, cid, clock, args)
    raise APIError("not_found", 404)


def registered_route(host, cid, args):
    pid = args["pid"]
    key = "/plugins/" + pid + "/" + args["subpath"]
    reg = next(
        (
            r
            for r in host.registrations(cid, "api_route")
            if r.plugin_id == pid
            and r.declaration["path"] == key
            and r.declaration["method"] == args["method"]
        ),
        None,
    )
    if reg is None or (args["method"] != "GET" and not reg.declaration.get("audit")):
        raise APIError("plugin_unavailable", 404)
    return reg


async def plugin_route(host, cid, clock, args):
    reg = registered_route(host, cid, args)
    # A reload between policy lookup and dispatch must not weaken the new route.
    if reg.declaration.get("confirm") and not args.get("confirmed"):
        raise APIError("confirmation_required", 428)
    if reg.declaration.get("admin_password") and not args.get("password_checked"):
        raise APIError("reauth_required", 401)
    try:
        value = await timed(
            reg.implementation({k: args[k] for k in ("method", "subpath", "query", "json")}),
            clock,
            10,
        )
    except TimeoutError:
        raise APIError("plugin_timeout", 504) from None
    if (
        not isinstance(value, dict)
        or set(value) != {"status", "json"}
        or type(value["status"]) is not int
        or not (
            value["status"] in {200, 201, 202}
            or (400 <= value["status"] < 500 and value["status"] not in {401, 403, 407, 428})
        )
    ):
        raise APIError("plugin_invalid_response", 502)
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError):
        raise APIError("plugin_invalid_response", 502) from None
    if size > 1024 * 1024:
        raise APIError("plugin_response_too_large", 502)
    return value
