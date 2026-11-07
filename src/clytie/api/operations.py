"""Management operations shared by the daemon and the owning companion process."""

import json
from dataclasses import asdict

from clytie.api.auth import APIError
from clytie.api.memory import MemoryAPI
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
        return api.export(cid, **args)
    if op == "thaw":
        return api.thaw_record(cid, args["record_id"])
    if op == "recovery-code":
        return {"recovery_code": api.new_recovery_code(cid, confirmed=True)}
    if op == "import/plan":
        return await api.plan(cid, **args)
    if op == "backup/plan":
        return await api.backup_plan(cid, **args)
    if op == "import/undo":
        return api.undo(cid, **args)
    if op == "import/apply":
        plan = load_plan(home, args["plan_id"])
        if plan["companion_id"] != cid:
            raise APIError("plan_stale", 409)
        return {"import_id": await api.apply(**args)}
    if op == "import/content":
        plan = load_plan(home, args["plan_id"])
        if plan["companion_id"] != cid:
            raise APIError("plan_stale", 409)
        if plan["kind"] == "undo":
            return api.undo(cid, plan["undo"], choices=plan.get("choices"), show_content=True)
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


async def companion_operation(runtime, op, args):
    home, clock, cid, host = runtime.home, runtime.clock, runtime.cid, runtime.host
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
    if op == "tasks":
        from clytie.kernel.query import local_task

        return local_task(
            args["task_id"], home=home, companion_id=cid, full=args.get("full", False)
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
        from clytie.backup.coordination import run_sync

        await run_sync(runtime.agent.ledger, runtime.agent.boundary.release, cid, args["actor_id"])
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
    if op == "plugin_route":
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
    raise APIError("not_found", 404)
