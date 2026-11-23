"""Command-line presentation; configuration logic lives behind the local API."""

import argparse
import json
import sys
from collections.abc import Sequence

from clytie.api import local_status, local_task, task_exit_code


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and (argv[0] == "_supervisor" or argv[:2] == ["companion", "run"]):
        from clytie.supervisor.entry import internal

        return internal(argv)
    parser = argparse.ArgumentParser(prog="clytie")
    subcommands = parser.add_subparsers(dest="command", required=True)
    smoke = (
        subcommands.add_parser("smoke")
        .add_subparsers(dest="action", required=True)
        .add_parser("run")
    )
    smoke.add_argument("--home")
    smoke.add_argument("--yes", action="store_true", help="仍需当次确认")
    supervisor_parser = subcommands.add_parser("supervisor", help="守护进程")
    supervisor_parser.add_argument("action", choices=["start", "status", "stop", "silence"])
    supervisor_parser.add_argument("--home")
    supervisor_parser.add_argument("--json", action="store_true")
    supervisor_parser.add_argument("--detach", action="store_true")
    supervisor_parser.add_argument("--maintenance", action="store_true")
    supervisor_parser.add_argument("--wait-tasks", action="store_true")
    supervisor_parser.add_argument("--yes", action="store_true")
    supervisor_parser.add_argument("value", nargs="?", choices=["on", "off"])
    from clytie.api.cli import add_parser as management_parser

    management_parser(subcommands)
    status_parser = subcommands.add_parser("status", help="离线检查实例配置")
    status_parser.add_argument("--home")
    status_parser.add_argument("--json", action="store_true")
    task_parser = subcommands.add_parser("task", help="只读查询任务账本")
    task_parser.add_argument("task_id")
    task_parser.add_argument("--home")
    task_parser.add_argument("--companion")
    task_parser.add_argument("--json", action="store_true")
    task_parser.add_argument("--full", action="store_true")
    from clytie.memory.cli import add_parser

    add_parser(subcommands)
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code and argv and "task" in argv:
            return 4
        raise
    from clytie.api.cli import run as management_run
    from clytie.config import resolve_home
    from clytie.supervisor.entry import status as supervisor_status

    if arguments.command == "smoke":
        from clytie.smoke.runner import main as smoke_run

        return smoke_run(resolve_home(arguments.home))
    offline = arguments.command == "status" or (
        arguments.command in {"task", "supervisor"}
        and (
            getattr(arguments, "action", None) == "start"
            or not supervisor_status(resolve_home(arguments.home))["running"]
        )
    )
    if offline:
        from clytie.supervisor.common import read

        silence = read(resolve_home(arguments.home) / "supervisor/silence.json", {})
        if silence.get("on"):
            print("全部静默中（自" + str(silence.get("since")) + "起）")
    if arguments.command == "supervisor":
        if arguments.action == "start":
            if arguments.detach:
                import asyncio

                from clytie.api.client import detached_start
                from clytie.clock import SystemClock

                result = asyncio.run(
                    detached_start(
                        resolve_home(arguments.home),
                        SystemClock(),
                        maintenance=arguments.maintenance,
                    )
                )
                print(json.dumps(result, ensure_ascii=False))
                return 0 if result["ok"] else 1
            from clytie.supervisor.entry import run

            return run(arguments)
        if (
            arguments.action == "status"
            and not supervisor_status(resolve_home(arguments.home))["running"]
        ):
            from clytie.supervisor.entry import run

            return run(arguments)
        return management_run(arguments)
    if arguments.command in {"memory", "companion", "boundary", "plugin", "admin", "backup"}:
        return management_run(arguments)
    if arguments.command == "task" and supervisor_status(resolve_home(arguments.home))["running"]:
        return management_run(arguments)
    if arguments.command == "task":
        result = local_task(
            arguments.task_id,
            home=arguments.home,
            companion_id=arguments.companion,
            full=arguments.full,
        )
        return display_task(arguments, result)
    report = local_status(arguments.home)
    result = report.to_dict()
    if arguments.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"实例目录：{report.home}")
        print(f"配置：{'有效' if report.valid else '无效'}")
        for companion in result["companions"]:
            print(f"伴侣 {companion['id']}：{'有效' if companion['valid'] else '无效'}")
            if companion["model"]:
                print(f"  模型：{companion['model']['id']} ({companion['model']['status']})")
            for kind, label in (("channels", "平台"), ("plugins", "启用插件")):
                for plugin in companion[kind]:
                    print(f"  {label}：{plugin['id']} ({plugin['status']})")
            for persona in companion["persona"]:
                print(f"  人设：{persona['layer']} ({persona['status']})")
            if belt := companion.get("safety_belt_config"):
                print(
                    f"  群聊安全带配置：{belt['window_minutes']} 分钟内回复 {belt['replies']} 次，暂停 {belt['pause_minutes']} 分钟"
                )
            if observed := companion.get("storage_observed_at"):
                print(f"  状态采样时间：{observed}")
            for group, value in companion.get("safety_belt", {}).items():
                from zoneinfo import ZoneInfo

                from clytie.agent.safety_belt import recovery_time
                from clytie.clock import SystemClock

                for row in value if isinstance(value, list) else [value]:
                    until = recovery_time(
                        row["until"],
                        SystemClock().now(),
                        ZoneInfo(report.instance.get("timezone", "UTC")),
                    )
                    print(f"  群聊安全带：{group} 当天触发 {row['count']} 次，恢复时间 {until}")
                    if row["owner_private_missing"]:
                        print("  主人私聊未登记，已跳过私聊提醒")
            for key, usage in companion.get("attention", {}).items():
                print(f"  轻量判断：{key} 当天 {usage['count']} 次，共 {usage['tokens']} token")
        for error in report.errors:
            actual = json.dumps(error.to_dict()["actual"], ensure_ascii=False)
            print(f"{error.file}#{error.pointer}：期望 {error.expected}；实际 {actual}")
        retained = result["failed_plugin_state"]
        for alert in result["ledger_errors"]:
            print("账本迁移阻止 [error]：" + json.dumps(alert, ensure_ascii=False))
        for notice in result["ledger_notices"]:
            print("账本快照保留提示 [warning]：" + json.dumps(notice, ensure_ascii=False))
        for notice in result["dialogue_notices"]:
            print("对话配置提示 [warning]：" + json.dumps(notice, ensure_ascii=False))
        for alert in retained.get("recovery_errors", []):
            print("恢复未完成 [error]：" + json.dumps(alert, ensure_ascii=False))
        for entry in retained.get("directories", []):
            hint = "问题已解决，可以清理" if entry["resolved"] else "故障状态保留，等待确认清理"
            print(f"{hint}：{entry['path']}；大小 {entry['bytes']} bytes")
            if entry["error"]:
                print(f"  {entry['error']}")
        if retained.get("directories"):
            print(
                f"故障目录总占用：{retained['total_bytes']} bytes；统计完整：{retained['size_complete']}"
            )
    return report.exit_code


def display_task(arguments, result):
    if arguments.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        task = result["task"]
        print(f"任务 {task['task_id']} · 伴侣 {task['companion_id']}")
        if task.get("origin") == "proactive":
            print("主动发起")
        for key, label in (
            ("kind", "类型"),
            ("tool", "能力"),
            ("requester", "请求人"),
            ("conversation_id", "会话"),
            ("exec_status", "执行状态"),
            ("delivery_status", "交付状态"),
            ("timings", "阶段耗时"),
            ("parent_task_id", "父任务"),
            ("child_task_ids", "子任务"),
            ("deliveries", "交付与收据"),
            ("error", "错误摘要"),
            ("asset_ids", "产物引用"),
            ("unrecovered", "尚未恢复"),
        ):
            print(f"{label}：{json.dumps(task[key], ensure_ascii=False)}")
        if arguments.full:
            print("完整内容：" + json.dumps(task["content"], ensure_ascii=False))
    else:
        print(f"查询失败 [{result['error']['code']}]：{result['error']['message']}")
        if result["error"].get("companions"):
            print("伴侣：" + ", ".join(result["error"]["companions"]))
    if not arguments.json and result["warnings"]:
        print("提醒：" + json.dumps(result["warnings"], ensure_ascii=False))
    return task_exit_code(result)
