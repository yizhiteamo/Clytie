"""独立锁阶段诊断：新建虚构目录、原 benchmark 请求，不产生性能通过结论。

例：python scripts/diagnose_storage_lock.py --output NEW_DIR --scale large_database
插桩增加开销；只追踪输出树中的 backup-capture.lock，不读取元数据正文或局部变量。
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import platform
import subprocess
import sys
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from time import perf_counter  # noqa: TID251 -- isolated diagnostic measurement
from unittest.mock import patch

from clytie.backup.coordination import CaptureCoordinator
from clytie.plugin_host import locking

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "diagnostic_benchmark", ROOT / "scripts/benchmark_backup.py"
)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class LockTrace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.events, self.stacks = [], []
        self._guard = threading.Lock()
        self._descriptors, self._owner_threads = {}, set()
        self._sequence = 0
        self._local = threading.local()
        self._stop = threading.Event()
        self._patches = ExitStack()
        self.origin = perf_counter()
        self.sampler = threading.Thread(target=self._sample, name="lock-diagnostic-sampler")

    def _within(self, path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return False
        value = Path(os.fsdecode(path))
        return value.name == "backup-capture.lock" and value.resolve().is_relative_to(self.root)

    def _identifier(self):
        with self._guard:
            self._sequence += 1
            return self._sequence

    def _measure(self, stage, operation, identifier):
        start = perf_counter()
        error = None
        thread = threading.current_thread()
        if thread.name == "clytie-storage-owner":
            with self._guard:
                self._owner_threads.add(thread.ident)
        try:
            return operation()
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            end = perf_counter()
            row = dict(
                lock_id=identifier,
                stage=stage,
                context=getattr(self._local, "phase", None),
                thread=thread.name,
                start_s=start - self.origin,
                end_s=end - self.origin,
                duration_ms=(end - start) * 1000,
                error=error,
            )
            with self._guard:
                self.events.append(row)

    def _fd_call(self, descriptor, stage, operation, *, closing=False):
        with self._guard:
            identifier = self._descriptors.get(descriptor)
        if identifier is None:
            return operation()
        result = self._measure(stage, operation, identifier)
        if closing:
            with self._guard:
                if self._descriptors.get(descriptor) == identifier:
                    self._descriptors.pop(descriptor, None)
        return result

    @property
    def active_descriptors(self):
        with self._guard:
            return len(self._descriptors)

    def sample_owner_positions(self):
        with self._guard:
            owners = self._owner_threads.copy()
        live = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name == "clytie-storage-owner"
        }
        frames = sys._current_frames()
        for identifier in owners & live:
            frame = frames.get(identifier)
            if frame is not None:
                row = dict(
                    at_s=perf_counter() - self.origin,
                    thread="clytie-storage-owner",
                    stack=benchmark.frame_locations(frame),
                )
                with self._guard:
                    self.stacks.append(row)

    def _sample(self):
        while not self._stop.wait(0.05):
            self.sample_owner_positions()

    async def __aenter__(self):
        original_open, original_lock = os.open, locking._os_lock
        original_coordinator = CaptureCoordinator._lock

        def opened(path, *args, **kwargs):
            if not self._within(path):
                return original_open(path, *args, **kwargs)
            identifier = getattr(self._local, "identifier", None) or self._identifier()
            descriptor = self._measure(
                "open", lambda: original_open(path, *args, **kwargs), identifier
            )
            with self._guard:
                self._descriptors[descriptor] = identifier
            return descriptor

        def os_lock(descriptor, acquire):
            return self._fd_call(
                descriptor,
                "os_lock_acquire" if acquire else "os_lock_release",
                lambda: original_lock(descriptor, acquire),
            )

        @contextmanager
        def coordinator_lock(coordinator):
            if not self._within(coordinator.path):
                with original_coordinator(coordinator) as value:
                    yield value
                return
            identifier = self._identifier()
            manager = original_coordinator(coordinator)

            def invoke(phase, operation):
                previous = (
                    getattr(self._local, "identifier", None),
                    getattr(self._local, "phase", None),
                )
                self._local.identifier, self._local.phase = identifier, phase
                try:
                    return self._measure("coordinator_" + phase, operation, identifier)
                finally:
                    self._local.identifier, self._local.phase = previous

            value = invoke("enter", manager.__enter__)
            try:
                yield value
            except BaseException:
                info = sys.exc_info()
                if not invoke("exit", lambda: manager.__exit__(*info)):
                    raise
            else:
                invoke("exit", lambda: manager.__exit__(None, None, None))

        try:
            self._patches.enter_context(patch.object(os, "open", opened))
            self._patches.enter_context(patch.object(locking, "_os_lock", os_lock))
            self._patches.enter_context(patch.object(CaptureCoordinator, "_lock", coordinator_lock))
            for name, stage in (
                ("write", "metadata_write"),
                ("ftruncate", "metadata_truncate"),
                ("fsync", "metadata_fsync"),
                ("close", "close"),
            ):
                original = getattr(os, name)

                def operation(descriptor, *args, _function=original, _stage=stage, **kwargs):
                    return self._fd_call(
                        descriptor,
                        _stage,
                        lambda: _function(descriptor, *args, **kwargs),
                        closing=_stage == "close",
                    )

                self._patches.enter_context(patch.object(os, name, operation))
            self.sampler.start()
        except BaseException:
            self._patches.close()
            raise
        return self

    async def __aexit__(self, *exc):
        self._stop.set()
        try:
            await benchmark.measurement_thread(self.sampler.join)
        finally:
            self._patches.close()

    def report(self, result=None):
        with self._guard:
            events, stacks = [row.copy() for row in self.events], list(self.stacks)
        origin = result["measurement_origin_s"] - self.origin if result else None
        cleanup = (
            min(
                (
                    row["start_s"]
                    for row in result["phases"]
                    if row["name"].startswith("measurement_")
                ),
                default=float("inf"),
            )
            if result
            else None
        )
        for row in events:
            row["window"] = (
                "unclassified"
                if origin is None
                else "setup"
                if row["end_s"] < origin
                else "measurement_cleanup"
                if row["start_s"] >= origin + cleanup
                else "workload"
            )
        groups = []
        for window, thread, stage, context in sorted(
            {
                (row["window"], row["thread"], row["stage"], row["context"] or "none")
                for row in events
            }
        ):
            selected = [
                row
                for row in events
                if (row["window"], row["thread"], row["stage"], row["context"] or "none")
                == (window, thread, stage, context)
            ]
            groups.append(
                dict(
                    window=window,
                    thread=thread,
                    stage=stage,
                    context=context,
                    duration_ms=benchmark.percentiles([row["duration_ms"] for row in selected]),
                    errors=sum(row["error"] is not None for row in selected),
                )
            )
        attempts = [row for row in events if row["stage"] == "os_lock_acquire"]
        return dict(
            scope="diagnostic_only",
            benchmark_origin_s=origin,
            lock_attempts=len(attempts),
            lock_failures=sum(row["error"] is not None for row in attempts),
            in_process_or_os_busy=sum(
                row["stage"] == "coordinator_enter" and row["error"] == "OperationBusy"
                for row in events
            ),
            active_descriptors=self.active_descriptors,
            sampler_alive=self.sampler.is_alive(),
            groups=groups,
            events=events,
            owner_stacks=stacks,
        )


def render_summary(report, *, raw_hash):
    def count(value):
        return f"{value:,}".replace(",", " ")

    def fixed(value):
        return f"{value:,.3f}".replace(",", " ")

    lines = [
        "# 存储锁阶段诊断",
        "",
        "Diagnostic only; not a performance verdict.",
        "同虚构规模与请求链路，插桩有额外开销；不替代正式对照，不减去开销，不改变锁/同步规则。",
        "只追踪新建输出树的 backup-capture.lock；不读取锁元数据正文或线程局部变量。",
        "非阻塞取锁失败与进入协调门闩的 OperationBusy 分开计数；后者也可能是进程内冲突。",
        "开锁路径判断与插桩本身也有成本；各子阶段不能相加为互斥等待。",
        "setup、workload、measurement_cleanup 分开显示；时段相对诊断起点。",
        f"原始诊断 SHA-256：{raw_hash}",
        "",
        f"非阻塞取锁：尝试 {count(report['lock_attempts'])}；失败 {count(report['lock_failures'])}；协调 busy {count(report['in_process_or_os_busy'])}。",
        f"收尾：未关闭 fd {count(report['active_descriptors'])}；采样线程存活 {str(report['sampler_alive']).lower()}。",
        "",
        "| 窗口 / 线程 / 阶段 / 上下文 | 次数 | P99 ms | 最大 ms | 错误数 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in report["groups"]:
        metric = row["duration_ms"]
        lines.append(
            f"| {row['window']} / {row['thread']} / {row['stage']} / {row['context']} | {count(metric['count'])} | {fixed(metric['p99'])} | {fixed(metric['max'])} | {count(row['errors'])} |"
        )
    if report.get("benchmark_origin_s") is not None:
        lines.extend(["", f"请求测量相对起点：{fixed(report['benchmark_origin_s'])} s。"])
    lines.extend(["", "最慢操作时段（各阶段互相嵌套，不能相加）：", ""])
    for row in sorted(report["events"], key=lambda row: row["duration_ms"], reverse=True)[:20]:
        lines.append(
            f"- {row['window']} / {row['thread']} / {row['stage']} / lock {count(row['lock_id'])}：{fixed(row['start_s'])}–{fixed(row['end_s'])} s，{fixed(row['duration_ms'])} ms；{row['error'] or 'completed'}。"
        )
    frequencies = {}
    for row in report["owner_stacks"]:
        key = " → ".join(
            f"{frame['file']}:{frame['line']} {frame['function']}" for frame in row["stack"][:8]
        )
        frequencies[key] = frequencies.get(key, 0) + 1
    lines.extend(["", "所有者线程代码位置采样（时间关联，不证明因果）：", ""])
    for stack, frequency in sorted(frequencies.items(), key=lambda pair: (-pair[1], pair[0])):
        lines.append(f"- {count(frequency)} 次：{stack}")
    return "\n".join(lines) + "\n"


async def main(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    conditions = dict(
        method_id="diagnostic-lock-stages-v1",
        os=platform.system(),
        commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        scale=args.scale,
        requests=args.requests,
        interval_seconds=args.interval,
        with_backup=not args.no_backup,
        scope="diagnostic_only",
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    (output / "conditions.json").write_text(json.dumps(conditions, indent=2), encoding="utf-8")
    trace = LockTrace(output)
    result = None
    try:
        async with trace:
            result = await benchmark.run_case(
                output / "case",
                args.scale,
                backup=not args.no_backup,
                requests=args.requests,
                interval=args.interval,
            )
    finally:
        report = trace.report(result)
        if result is not None:
            report["benchmark_result_sha256"] = hashlib.sha256(
                (output / "case/result.json").read_bytes()
            ).hexdigest()
        raw = json.dumps(report, indent=2).encode()
        (output / "trace.json").write_bytes(raw)
        (output / "summary.md").write_text(
            render_summary(report, raw_hash=hashlib.sha256(raw).hexdigest()), encoding="utf-8"
        )
    print("lock diagnostic recorded; not a performance verdict", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--scale", choices=tuple(benchmark.SCALES), default="small_text")
    parser.add_argument("--requests", type=int, default=800)
    parser.add_argument("--interval", type=float, default=0.025)
    parser.add_argument("--no-backup", action="store_true")
    arguments = parser.parse_args()
    if arguments.requests < 1 or arguments.interval <= 0:
        parser.error("requests and interval must be positive")
    asyncio.run(main(arguments))
