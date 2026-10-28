"""Command-line presentation; configuration logic lives behind the local API."""

import argparse
import json
from collections.abc import Sequence

from clytie.api import local_status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clytie")
    subcommands = parser.add_subparsers(dest="command", required=True)
    status_parser = subcommands.add_parser("status", help="离线检查实例配置")
    status_parser.add_argument("--home")
    status_parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
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
    return report.exit_code
