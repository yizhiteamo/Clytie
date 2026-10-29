"""Command-line presentation; configuration logic lives behind the local API."""

import argparse
import json
import sys
from collections.abc import Sequence

from clytie.api import local_status, local_task, task_exit_code


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="clytie")
    subcommands = parser.add_subparsers(dest="command", required=True)
    status_parser = subcommands.add_parser("status", help="离线检查实例配置")
    status_parser.add_argument("--home")
    status_parser.add_argument("--json", action="store_true")
    task_parser = subcommands.add_parser("task", help="只读查询任务账本")
    task_parser.add_argument("task_id")
    task_parser.add_argument("--home")
    task_parser.add_argument("--companion")
    task_parser.add_argument("--json", action="store_true")
    task_parser.add_argument("--full", action="store_true")
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code and argv and "task" in argv:
            return 4
        raise
    if arguments.command == "task":
        result = local_task(
            arguments.task_id,
            home=arguments.home,
            companion_id=arguments.companion,
            full=arguments.full,
        )
        if arguments.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif result["ok"]:
            task = result["task"]
            print(f"任务 {task['task_id']} · 伴侣 {task['companion_id']}")
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
