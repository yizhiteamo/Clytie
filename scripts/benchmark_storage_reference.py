"""同一 runner 上固定旧候选的无备份基线；只创建新的虚构目录，保留全部结果。"""

import argparse
import asyncio
import hashlib
import importlib.util
import io
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = "05ec93c7c4ae972b7475911b0aec9db57bb00390"
METHOD = "fixed-query-coordinated-meta-fake-model-v1"


def extract_reference(root):
    root.mkdir(parents=True, exist_ok=False)
    archive = subprocess.check_output(["git", "archive", "--format=tar", REFERENCE], cwd=ROOT)
    files = []
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle:
            path = root / member.name
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("reference_path_invalid")
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                data = bundle.extractfile(member).read()
                with path.open("xb") as stream:
                    stream.write(data)
                files.append({"path": member.name, "sha256": hashlib.sha256(data).hexdigest()})
            else:
                raise ValueError("reference_entry_unsupported")
    return {
        "commit": REFERENCE,
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
        "files": files,
    }


async def main(args):
    if any(name == "clytie" or name.startswith("clytie.") for name in sys.modules):
        raise RuntimeError("reference_requires_fresh_process")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    inventory = extract_reference(source)
    (output / "reference-inventory.json").write_text(
        json.dumps(inventory, indent=2), encoding="utf-8"
    )
    # 独立进程只载入固定旧树，不把新代码误当成旧基线。
    sys.path.insert(0, str(source / "src"))
    location = source / "scripts/benchmark_backup.py"
    spec = importlib.util.spec_from_file_location("fixed_reference_benchmark", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    machine = {
        "os": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "logical_cpus": os.cpu_count(),
        "processor": platform.processor(),
        "runner_id": (
            hashlib.sha256(os.environ["CLYTIE_BENCHMARK_RUNNER_ID"].encode()).hexdigest()
            if os.environ.get("CLYTIE_BENCHMARK_RUNNER_ID")
            else None
        ),
        "method_id": METHOD,
        "commit": REFERENCE,
        "working_tree_modified": False,
        "script_sha256": hashlib.sha256(location.read_bytes()).hexdigest(),
        "seed": module.SEED,
        "background_load": args.conditions_note,
        "limits": [
            "Fresh process, fixed pre-StorageOwner tree; original no-backup workload unchanged.",
            "All old results retained, including abnormal latency; no outlier removal.",
            "This baseline does not test new ownership, capture or recovery guarantees.",
        ],
    }
    (output / "conditions.json").write_text(json.dumps(machine, indent=2), encoding="utf-8")
    results = []
    for scale in args.scales:
        for repeat in range(1, args.repeats + 1):
            result = await module.run_case(
                output / f"{scale}-baseline-repeat-{repeat}",
                scale,
                backup=False,
                requests=args.requests,
                interval=args.interval,
            )
            result.update(repeat=repeat, order=1)
            results.append({k: v for k, v in result.items() if k not in {"samples", "loop_delays"}})
            (output / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
            (output / "summary.md").write_text(
                module.render_summary(machine, results), encoding="utf-8"
            )
            print(f"reference complete: {scale} / repeat {repeat}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--requests", type=int, default=800)
    parser.add_argument("--interval", type=float, default=0.025)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--scales",
        nargs="+",
        choices=["small_text", "large_database", "many_small_files", "large_asset"],
        required=True,
    )
    parser.add_argument("--conditions-note", required=True)
    arguments = parser.parse_args()
    if arguments.requests < 1 or arguments.interval <= 0 or arguments.repeats < 1:
        sys.exit("requests, repeats and interval must be positive")
    asyncio.run(main(arguments))
