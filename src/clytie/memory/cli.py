"""记忆 CLI 的交互层；数据操作全部委托 MemoryAPI。"""

import asyncio
import getpass
import json

from clytie.api.memory import MemoryAPI


def add_parser(commands):
    memory = commands.add_parser("memory", help="记忆导出、搬家计划和恢复码")
    actions = memory.add_subparsers(dest="memory_action", required=True)
    export = actions.add_parser("export")
    export.add_argument("companion")
    export.add_argument("--out", required=True)
    export.add_argument("--format", choices=["encrypted", "jsonl"], default="encrypted")
    export.add_argument("--home")
    recovery = actions.add_parser("recovery-code")
    recovery.add_argument("new", choices=["new"])
    recovery.add_argument("--companion")
    recovery.add_argument("--confirm", action="store_true")
    recovery.add_argument("--home")
    imports = actions.add_parser("import").add_subparsers(dest="import_action", required=True)
    plan = imports.add_parser("plan")
    plan.add_argument("companion")
    plan.add_argument("source")
    plan.add_argument("--mode", choices=["new", "restore", "merge"], required=True)
    plan.add_argument("--home")
    plan.add_argument("--recovery-code", action="store_true")
    plan.add_argument("--show-content", action="store_true")
    plan.add_argument("--confirm-identity", action="store_true")
    plan.add_argument("--choice", action="append", default=[])
    apply = imports.add_parser("apply")
    apply.add_argument("plan_id")
    apply.add_argument("--home")
    apply.add_argument("--recovery-code", action="store_true")
    undo = imports.add_parser("undo")
    undo.add_argument("import_id")
    undo.add_argument("--home")
    undo.add_argument("--companion")
    undo.add_argument("--show-content", action="store_true")
    undo.add_argument("--choice", action="append", default=[])


def private_text(recovery):
    if recovery:
        return {"recovery_code": getpass.getpass("恢复码：")}
    return {"passphrase": getpass.getpass("记忆包口令：")}


def choices(values):
    result = {}
    for value in values:
        key, separator, choice = value.partition("=")
        if not separator or choice not in {"current", "package"} or key in result:
            raise ValueError("冲突选择格式为 record_id=current|package，且每个编号只能出现一次")
        result[key] = choice
    return result


def run(arguments):
    api = MemoryAPI(arguments.home)
    try:
        if arguments.memory_action == "export":
            result = api.export(
                arguments.companion,
                arguments.out,
                format=arguments.format,
                **({} if arguments.format == "jsonl" else private_text(False)),
            )
            if result["recovery_code"]:
                print("恢复码只显示这一次，请打印或存入密码管理器：")
                print(result.pop("recovery_code"))
        elif arguments.memory_action == "recovery-code":
            print("请保留旧恢复码；旧包仍然需要旧恢复码或各自口令。")
            print(api.new_recovery_code(arguments.companion, confirmed=arguments.confirm))
            return 0
        elif arguments.import_action == "plan":
            result = asyncio.run(
                api.plan(
                    arguments.companion,
                    arguments.source,
                    mode=arguments.mode,
                    choices=choices(arguments.choice),
                    confirm_identity=arguments.confirm_identity,
                    show_content=arguments.show_content,
                    **private_text(arguments.recovery_code),
                )
            )
        elif arguments.import_action == "undo":
            result = api.undo(
                arguments.companion,
                arguments.import_id,
                choices=choices(arguments.choice),
                show_content=arguments.show_content,
            )
        else:
            options = (
                {}
                if api.plan_kind(arguments.plan_id) == "undo"
                else private_text(arguments.recovery_code)
            )
            result = {"import_id": asyncio.run(api.apply(arguments.plan_id, **options))}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"记忆操作失败：{exc}")
        return 1
