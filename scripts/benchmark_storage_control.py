"""临时真实 SQLite 所有者在注入慢 I/O 时的控制受理测量。

延迟是明确的诊断注入，不代表机器实际 fsync/commit 时长。队列中的取消请求
必须等真实慢命令结束后才完成；受理指标不会替代最终完成或持久核验。
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import sqlite3
import subprocess
import threading
from pathlib import Path
from time import perf_counter  # noqa: TID251 -- benchmark measurement only

from clytie.sdk.testing import MockClock
from clytie.storage.owner import StorageCancelled, StorageOwner

ROOT = Path(__file__).resolve().parents[1]


def percentiles(values):
    ordered = sorted(values)
    return {
        "count": len(values),
        **{f"p{p}": ordered[math.ceil(len(values) * p / 100) - 1] for p in (50, 95, 99)},
        "max": ordered[-1],
    }


async def scheduled_wait(deadline):
    loop = asyncio.get_running_loop()
    while (remaining := deadline - perf_counter()) > 0:
        ready = loop.create_future()
        timer = loop.call_later(max(0.001, remaining), ready.set_result, None)
        try:
            await ready
        finally:
            timer.cancel()


async def run_case(directory, mode, *, delay=1.0, samples=100, interval=0.005):
    if mode not in {"fsync", "sqlite_commit"}:
        raise ValueError("unknown_io_mode")
    if samples < 1 or interval <= 0 or delay <= samples * interval:
        raise ValueError("injection_must_cover_all_scheduled_controls")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    loop = asyncio.get_running_loop()
    entered = loop.create_future()
    io_finished = threading.Event()
    io_times = {}
    errors, measurements = [], []
    target_executed = threading.Event()

    def block_then(operation):
        io_times["start"] = perf_counter()
        loop.call_soon_threadsafe(entered.set_result, None)
        # Same owner thread as real I/O. Event.wait releases the GIL; the loop must
        # remain responsive while the queued command still cannot complete.
        threading.Event().wait(delay)
        try:
            return operation()
        finally:
            io_times["end"] = perf_counter()
            io_finished.set()

    class ProbeConnection(sqlite3.Connection):
        def commit(self):
            if mode == "sqlite_commit" and self.in_transaction:
                return block_then(super().commit)
            return super().commit()

    class Resources:
        def __init__(self):
            self.db = sqlite3.connect(directory / "probe.db", factory=ProbeConnection)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("CREATE TABLE probe(value INTEGER NOT NULL)")

        def close(self):
            self.db.close()

    def slow(resources, command):
        resources.db.execute("BEGIN IMMEDIATE")
        try:
            resources.db.execute("INSERT INTO probe VALUES(1)")
            command.begin_commit()
            if mode == "fsync":
                with (directory / "fixture.bin").open("xb") as stream:
                    stream.write(b"fictional control probe\n")
                    stream.flush()
                    block_then(lambda: os.fsync(stream.fileno()))
            resources.db.commit()
        except BaseException:
            resources.db.rollback()
            raise

    def must_not_execute(resources, command):
        target_executed.set()
        resources.db.execute("INSERT INTO probe VALUES(2)")
        resources.db.commit()

    owner = await StorageOwner.start(Resources, companion_id="control_probe", clock=MockClock())
    cancel_completed = False
    persisted = None
    try:
        active = owner.submit(slow)
        await entered
        target = owner.submit(must_not_execute)
        queued = [owner.submit(lambda resources, command: None) for _ in range(62)]
        start = perf_counter()
        for index in range(samples):
            scheduled = start + index * interval
            await scheduled_wait(scheduled)
            during_block = not io_finished.is_set()
            status = owner.status()
            status_end = perf_counter()
            accepted = target.cancel()
            cancel_end = perf_counter()
            measurements.append(
                {
                    "index": index,
                    "scheduled_s": scheduled - start,
                    "status_ms": (status_end - scheduled) * 1000,
                    "cancel_ms": (cancel_end - scheduled) * 1000,
                    "during_block": during_block and not io_finished.is_set(),
                    "cancel_accepted": accepted,
                    "cancel_completed_at_return": target.phase == "finished",
                    "status": status,
                }
            )
        await active.result()
        try:
            await target.result()
        except StorageCancelled:
            cancel_completed = True
        await asyncio.gather(*(item.result() for item in queued))
        settled = perf_counter()
        persisted = await owner.submit(
            lambda resources, command: resources.db.execute(
                "SELECT COUNT(*) FROM probe"
            ).fetchone()[0]
        ).result()
        if target_executed.is_set():
            errors.append("cancelled_command_executed")
        status_at_end = owner.status()
    finally:
        await owner.close()
    result = {
        "mode": mode,
        "requested_samples": samples,
        "interval_seconds": interval,
        "injected_delay_seconds": delay,
        "injection_method": "owner_thread_event_wait_before_real_io",
        "observed_io_block_seconds": io_times["end"] - io_times["start"],
        "status_ms": percentiles([row["status_ms"] for row in measurements]),
        "cancel_ms": percentiles([row["cancel_ms"] for row in measurements]),
        "cancel_settlement_ms": (settled - start) * 1000,
        "queued_cancel_completed": cancel_completed,
        "persisted_rows": persisted,
        "owner_status_at_end": status_at_end,
        "owner_thread_alive_after_close": owner._thread.is_alive(),
        "errors": errors,
        "samples": measurements,
    }
    (directory / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def render_summary(rows):
    lines = [
        "# 慢 I/O 控制测量",
        "",
        "明确注入延迟后执行真实 fsync/SQLite commit；仅测受理，不把取消当完成。",
        "定速请求含调度迟延；每次测量时队列先填满；实际提交、取消结束及所有者退出另行核验。",
        "",
    ]
    for row in rows:
        lines.append(
            f"- {row['mode']} / 第 {row['repeat']} 组：注入 {row['injected_delay_seconds']:.3f} s；"
            f"观测阻塞 {row['observed_io_block_seconds']:.3f} s；取消实际收尾 {row['cancel_settlement_ms']:.3f} ms。"
        )
        for operation in ("status", "cancel"):
            metric = row[operation + "_ms"]
            lines.append(
                f"  - {operation}：P99 {metric['p99']:.3f} ms；最大 {metric['max']:.3f} ms。"
            )
    return "\n".join(lines) + "\n"


async def main(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    runner = os.environ.get("CLYTIE_BENCHMARK_RUNNER_ID")
    conditions = {
        "os": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "runner_id": hashlib.sha256(runner.encode()).hexdigest() if runner else None,
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "method_id": "owner-control-injected-io-v1",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limits": [
            "Injected I/O delay is diagnostic, not native disk latency.",
            "Control acceptance is separate from actual command settlement.",
            "Fictional temporary SQLite data; no product model, platform or network.",
        ],
    }
    (output / "conditions.json").write_text(json.dumps(conditions, indent=2), encoding="utf-8")
    rows = []
    for repeat in range(1, args.repeats + 1):
        for mode in ("fsync", "sqlite_commit"):
            result = await run_case(
                output / f"{mode}-repeat-{repeat}",
                mode,
                delay=args.delay,
                samples=args.samples,
                interval=args.interval,
            )
            result["repeat"] = repeat
            rows.append(result)
            (output / "summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
            (output / "summary.md").write_text(render_summary(rows), encoding="utf-8")
            print(f"control case complete: {mode} / repeat {repeat}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--interval", type=float, default=0.005)
    arguments = parser.parse_args()
    if (
        arguments.repeats < 1
        or arguments.samples < 1
        or arguments.interval <= 0
        or arguments.delay <= arguments.samples * arguments.interval
    ):
        parser.error("positive repeats/samples/interval and delay > samples * interval required")
    asyncio.run(main(arguments))
