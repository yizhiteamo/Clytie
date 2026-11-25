"""离线在线备份组件基准；只创建新虚构目录，不清理或访问部署。

运行：python scripts/benchmark_backup.py --output .cache/a4-3-performance/windows-01
响应是定速 SQLite 查询/协调写入与 FakeModel 响应，不是 HTTP/完整对话 SLO。
MockClock 驱动产品时间；perf_counter/loop.call_later 仅用于负载与性能测量。
"""

import argparse
import asyncio
import ctypes
import hashlib
import json
import math
import os
import platform
import random
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter  # noqa: TID251 -- benchmark measurement, not product time
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from clytie.backup import runtime_capture
from clytie.backup.container import pack, unpack
from clytie.backup.coordination import companion_coordinator, run_sync
from clytie.backup.runtime_capture import capture_online
from clytie.backup.source import capture_instance
from clytie.config import load_home
from clytie.kernel.migration import LedgerRuntime
from clytie.plugin_host import PluginHost
from clytie.sdk import CancelToken
from clytie.sdk.testing import FakeContext, FakeModel, MockClock
from clytie.sdk.types import AssetRef
from clytie.storage.owner import StorageOwner
from clytie.storage.runtime import CompanionResources, bind_handles

ROOT = Path(__file__).resolve().parents[1]
SEED = 0x11DA9
# rows of 4096 bytes, immutable provider file count, bytes per file
SCALES = {
    "small_text": (128, 8, 1024),
    "large_database": (32 * 1024, 8, 1024),
    "many_small_files": (128, 2048, 1024),
    "large_asset": (128, 1, 128 * 1024 * 1024),
}
PROVIDER = """
import asyncio
from clytie.sdk import BackupTicket, BackupView
class Plugin:
    async def start(self, ctx):
        self.files = ()
        self.assets = ()
    async def stop(self):
        pass
    def backup(self):
        return self
    async def prepare(self, request, cancel):
        return BackupTicket(self.files)
    async def capture(self, ticket, epoch, cancel):
        return BackupView(epoch, 1, 'immutable_fixture_v1', ticket.handle, self.assets)
    async def materialize(self, view, sink, cancel):
        for path in view.handle:
            async def chunks(path=path):
                with path.open('rb') as stream:
                    while True:
                        block = await asyncio.to_thread(stream.read, 1024 * 1024)
                        if not block:
                            break
                        yield block
            await sink.write(path.name, chunks())
    async def release(self, ticket):
        pass
"""


def percentiles(values):
    if not values:
        return None
    ordered = sorted(values)
    return {
        "count": len(values),
        **{f"p{p}": ordered[math.ceil(len(values) * p / 100) - 1] for p in (50, 95, 99)},
        "max": ordered[-1],
    }


@contextmanager
def observe_capture_lock(coordinator, waiting, holds, waits):
    attempted, acquired = perf_counter(), False
    try:
        with coordinator._lock():
            acquired = True
            waits.append((perf_counter() - waiting) * 1000)
            yield
    finally:
        # Production checks its deadline after release. Observe the same full
        # interval, including a slow or failed underlying __exit__.
        if acquired:
            holds.append((perf_counter() - attempted) * 1000)


async def measurement_thread(function, *args):
    """测量器取消等待也必须等实际扫描/线程退出；不遗弃收尾执行者。"""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


def rss_bytes():
    if os.name == "nt":

        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "peak",
                    "working",
                    "peak_paged",
                    "paged",
                    "peak_nonpaged",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                )
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise OSError("rss_measurement_failed")
        return counters.working
    # Linux /proc reports current resident pages, including native allocations.
    return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")


def tree_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except FileNotFoundError:
                pass  # an atomic publication can move a file during sampling
    return total


def frame_locations(frame):
    """Only code locations, never locals, credentials or absolute host paths."""
    result = []
    while frame is not None and len(result) < 12:
        path = Path(frame.f_code.co_filename)
        try:
            name = path.relative_to(ROOT).as_posix()
        except ValueError:
            name = path.name
        result.append(dict(file=name, line=frame.f_lineno, function=frame.f_code.co_name))
        frame = frame.f_back
    return result


async def setup(directory, scale, *, core_assets=False, record_payload=False, seed=SEED):
    home = directory / "home"
    shutil.copytree(ROOT / "examples/home", home)
    plugin_path = home / "plugins/benchmark-snapshot"
    plugin_path.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "id": "benchmark-snapshot",
        "name": "虚构性能样本",
        "summary": "离线不可变文件提供者",
        "category": "example",
        "version": "1.0.0",
        "requires": {"sdk": ">=0.7,<0.22"},
        "entry": "plugin:Plugin",
        "contributes": {"backup": {"snapshot": {"factory": "backup"}}},
        "permissions": [],
        "config": {"type": "object", "additionalProperties": False},
        "backup": {"mode": "snapshot"},
    }
    (plugin_path / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_path / "plugin.py").write_text(PROVIDER, encoding="utf-8")
    config_path = home / "companions/companion_a/companion.yaml"
    config = yaml.safe_load(config_path.read_bytes())
    config["plugins"]["benchmark-snapshot"] = {"enabled": True, "config": {}}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    versions = {}
    for pid in ("fake-model", "fake-channel"):
        metadata = json.loads((ROOT / "plugins" / pid / "plugin.json").read_bytes())
        versions[pid] = {
            "plugin_version": metadata["version"],
            "data_version": metadata.get("data", {}).get("version", 1),
        }
    (home / "companions/companion_a/plugin-versions.json").write_text(json.dumps(versions))
    clock = MockClock()
    report = load_home(home, repo_root=ROOT)
    if not report.valid:
        raise RuntimeError("fictional_configuration_invalid")
    host = PluginHost(ROOT, home, clock=clock)
    host.configure_report(report)
    runtime = LedgerRuntime(home, "companion_a", clock)
    runtime.owner_managed = True
    await runtime.open()
    storage_owner = await StorageOwner.start(
        lambda: CompanionResources(host, runtime, {}),
        companion_id="companion_a",
        clock=clock,
        coordination_path=companion_coordinator(home, "companion_a", clock).path,
    )
    try:
        await runtime.attach_owned(bind_handles(storage_owner, host, runtime))
        store = host.memory_resources["companion_a"][0]
        plan = host.plan_enable("companion_a", "benchmark-snapshot")
        applied = await host.apply(plan.plan_id)
        if applied["failed"]:
            raise RuntimeError("fictional_provider_enable_failed")
        rows, files, size = scale
        rng = random.Random(seed)

        def seed_database():
            with store.transaction():
                store.set_meta("companion_uid", "a" * 32)
                if record_payload:
                    from clytie.sdk.types import MemoryRecord

                    # New complete-package fixtures need the actual core schema and
                    # revision chain. Keep the historical component baseline intact.
                    for index in range(rows):
                        store._save(
                            MemoryRecord(
                                record_id=f"{index + 1:032x}",
                                revision=1,
                                kind="fact",
                                content=rng.randbytes(2048).hex(),
                                visibility="private",
                                write_mode="auto",
                                source_type="private",
                                created_at=clock.now(),
                                updated_at=clock.now(),
                                decay={"weight": 1, "frozen": False},
                                retracted=False,
                            ),
                            operator="fixture",
                        )
                else:
                    store.db.execute(
                        "CREATE TABLE benchmark_payload (id INTEGER PRIMARY KEY, data BLOB)"
                    )
                    store.db.executemany(
                        "INSERT INTO benchmark_payload VALUES (?, ?)",
                        ((index, rng.randbytes(4096)) for index in range(rows)),
                    )
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        await run_sync(store, seed_database)
        data = home / (
            "companions/companion_a/assets"
            if core_assets
            else "companions/companion_a/plugin-data/benchmark-snapshot"
        )
        data.mkdir(parents=True, exist_ok=True)
        paths = []
        assets = []
        for index in range(files):
            path = data / f"fictional_{index:05d}.bin"
            with path.open("xb") as stream:
                remaining = size
                digest = hashlib.sha256()
                while remaining:
                    block = rng.randbytes(min(1024 * 1024, remaining))
                    stream.write(block)
                    digest.update(block)
                    remaining -= len(block)
            if core_assets:
                path.chmod(0o444)
                ref = AssetRef(
                    asset_id=digest.hexdigest(),
                    size=size,
                    media_type="application/octet-stream",
                    origin="fictional benchmark",
                )
                host.asset_store.register_private(
                    "companion_a", ref, path.relative_to(home).as_posix()
                )
                assets.append(ref)
            else:
                paths.append(path)
        host.runtimes["companion_a", "benchmark-snapshot"].plugin.files = tuple(paths)
        host.runtimes["companion_a", "benchmark-snapshot"].plugin.assets = tuple(assets)
        return SimpleNamespace(
            home=home,
            cid="companion_a",
            clock=clock,
            host=host,
            runtime=runtime,
            report=report,
            management_requests=set(),
            storage_owner=storage_owner,
        ), store

    except BaseException:
        try:
            await host.close()
        finally:
            try:
                await storage_owner.close()
            finally:
                runtime.close()
        raise


async def run_case(directory, name, *, backup, requests=800, interval=0.025, scale_override=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    owner, store = await setup(
        directory, scale_override or SCALES[name], core_assets=name == "large_asset"
    )
    work = directory / "work"
    work.mkdir()
    peak = {"rss": rss_bytes(), "temporary": 0, "wal": 0}
    stop = threading.Event()
    origin = perf_counter()
    last_clock = origin
    main_thread = threading.get_ident()
    loop_delays, stalled_stacks, phases = [], [], []
    resource_samples, committed_request_indices, snapshot_threads = [], [], []

    def sample():
        while not stop.is_set():
            peak["rss"] = max(peak["rss"], rss_bytes())
            peak["temporary"] = max(peak["temporary"], tree_bytes(work))
            peak["wal"] = max(
                peak["wal"], sum(p.stat().st_size for p in owner.home.rglob("*-wal") if p.exists())
            )
            now = perf_counter()
            resource_samples.append({"at_s": now - origin, **owner.storage_owner.status()})
            if now - last_clock > 0.1:
                frame = sys._current_frames().get(main_thread)
                stalled_stacks.append(
                    dict(
                        at_s=now - origin,
                        delay_ms=(now - last_clock) * 1000,
                        stack=frame_locations(frame),
                    )
                )
            stop.wait(0.05)

    sampler = threading.Thread(target=sample, name="benchmark-resource-sampler", daemon=True)
    sampler.start()
    loop = asyncio.get_running_loop()
    clock_done = False

    def tick():
        nonlocal last_clock
        now = perf_counter()
        loop_delays.append(
            dict(at_s=now - origin, delay_ms=max(0, now - last_clock - 0.005) * 1000)
        )
        owner.clock.advance(now - last_clock)
        last_clock = now
        if not clock_done:
            loop.call_later(0.005, tick)

    tick()
    samples, errors, backup_result = [], [], None
    backup_start = backup_end = None
    capture_holds, capture_waits = [], []
    model = FakeModel(FakeContext(clock=owner.clock))

    @contextmanager
    def phase(name):
        row = dict(name=name, start_s=perf_counter() - origin)
        phases.append(row)
        try:
            yield
        finally:
            row["end_s"] = perf_counter() - origin

    async def wait_until(deadline):
        # Windows event-loop clock resolution may dispatch timers early. Never
        # turn this into negative latency or a higher-than-declared arrival rate.
        while (remaining := deadline - perf_counter()) > 0:
            future = loop.create_future()
            handle = loop.call_later(max(0.001, remaining), future.set_result, None)
            try:
                await future
            finally:
                handle.cancel()

    async def load():
        for index in range(requests):
            scheduled = origin + index * interval
            await wait_until(scheduled)
            start = perf_counter()
            waited = None
            query_ms = commit_ms = model_ms = storage_wait = None
            try:
                enqueued = perf_counter()

                def command(resources, receipt):
                    executing = perf_counter()
                    store = resources.memory
                    query_started = perf_counter()
                    store.db.execute(
                        "SELECT data FROM benchmark_payload WHERE id=?",
                        (index % (scale_override or SCALES[name])[0],),
                    ).fetchone()
                    query = (perf_counter() - query_started) * 1000
                    write_started = perf_counter()
                    with store.coordination.commit():
                        acquired = perf_counter()
                        with store.transaction():
                            store.set_meta("benchmark_counter", index)
                        committed = perf_counter()
                        committed_request_indices.append(index)
                    return (
                        (executing - enqueued) * 1000,
                        query,
                        (acquired - write_started) * 1000,
                        (committed - acquired) * 1000,
                    )

                storage_wait, query_ms, waited, commit_ms = await owner.storage_owner.execute(
                    command
                )
                model_start = perf_counter()
                await model.generate(None, CancelToken())
                model_ms = (perf_counter() - model_start) * 1000
            except Exception as exc:
                errors.append({"index": index, "type": type(exc).__name__})
            samples.append(
                {
                    "index": index,
                    "scheduled_s": scheduled - origin,
                    "start_s": start - origin,
                    "end_s": perf_counter() - origin,
                    "response_ms": (perf_counter() - scheduled) * 1000,
                    "write_wait_ms": waited,
                    "queue_delay_ms": (start - scheduled) * 1000,
                    "storage_queue_ms": storage_wait,
                    "query_ms": query_ms,
                    "commit_ms": commit_ms,
                    "model_ms": model_ms,
                }
            )

    async def do_backup():
        nonlocal backup_start, backup_end, backup_result
        await wait_until(origin + min(0.1, requests * interval / 5))
        backup_start = perf_counter()
        stage = work / "stage"

        async def online(cid, target, epoch):
            with phase("online_capture"):
                return await capture_online(owner, target, epoch)

        original_capture = runtime_capture.capture_group

        async def observed_capture(paths, coordinator, **kwargs):
            class ObservedCoordinator:
                def __getattr__(self, name):
                    return getattr(coordinator, name)

                @contextmanager
                def _lock(self):
                    with observe_capture_lock(coordinator, waiting, capture_holds, capture_waits):
                        yield

            waiting = perf_counter()
            view = await original_capture(paths, ObservedCoordinator(), **kwargs)
            snapshot_threads.append(view._worker.thread)
            return view

        # Observe the real context body without changing deadlines or lock policy.
        with (
            phase("instance_capture"),
            patch.object(runtime_capture, "capture_group", observed_capture),
        ):
            captured = await capture_instance(
                owner.home,
                owner.clock,
                stage,
                source_instance_id="fictional-benchmark",
                epoch="benchmark_epoch",
                online=online,
                online_ids={owner.cid},
                repo_root=ROOT,
            )
        package = work / "package.clytie-backup"
        with phase("pack"):
            await asyncio.to_thread(pack, stage, package, b"b" * 32, captured["manifest"])
        verified = work / "verified"
        verified.mkdir()
        with phase("unpack_verify"):
            unpacked = await asyncio.to_thread(unpack, package, verified, b"b" * 32)
        if name == "large_asset":
            from clytie.assets import AssetStore
            from clytie.backup.assets import materialize

            await asyncio.to_thread(materialize, verified, unpacked)
            restored = AssetStore(verified, owner.clock)
            snapshot = restored.snapshot()
            for row in snapshot.registrations:
                fixed = restored.resolve(snapshot, "companion_a", row.asset_id)
                await asyncio.to_thread(restored.verify_resolved, fixed)
        backup_end = perf_counter()
        backup_result = {
            "seconds": backup_end - backup_start,
            "completeness": captured["manifest"]["completeness"],
            "missing": captured["missing"],
            "package_bytes": package.stat().st_size,
            "verified_files": len(unpacked["files"]),
            "verified_assets": len(unpacked["manifest"]["assets"]),
        }

    try:
        jobs = [load()]
        if backup:
            jobs.append(do_backup())
        await asyncio.gather(*jobs)
        with phase("measurement_final_disk_scan"):
            peak["temporary"] = max(peak["temporary"], await measurement_thread(tree_bytes, work))
        persisted_counter = await run_sync(store, store.meta, "benchmark_counter")
        owner_status_at_end = owner.storage_owner.status()
        resource_samples.append({"at_s": perf_counter() - origin, **owner_status_at_end})
    finally:
        stop.set()
        try:
            with phase("measurement_sampler_exit"):
                await measurement_thread(sampler.join)
        finally:
            clock_done = True
            await owner.host.close()
            await owner.storage_owner.close()
            owner.runtime.close()
    overlap = [
        s
        for s in samples
        if backup_start is not None
        and s["end_s"] >= backup_start - origin
        and s["start_s"] <= backup_end - origin
    ]
    coverage_seconds = (
        max(
            0,
            min(samples[-1]["end_s"], backup_end - origin)
            - max(samples[0]["scheduled_s"], backup_start - origin),
        )
        if samples and backup_start is not None
        else None
    )
    for row in phases:
        row["response_ms"] = percentiles(
            [
                sample["response_ms"]
                for sample in samples
                if sample["end_s"] >= row["start_s"] and sample["scheduled_s"] <= row["end_s"]
            ]
        )
    result = {
        # Process-local monotonic reading, solely for diagnostic timeline alignment.
        "measurement_origin_s": origin,
        "scale": name,
        "scale_parameters": scale_override or SCALES[name],
        "seed": SEED,
        "with_backup": backup,
        "requests": requests,
        "interval_seconds": interval,
        "response_ms": percentiles([s["response_ms"] for s in samples]),
        "write_wait_ms": percentiles(
            [s["write_wait_ms"] for s in samples if s["write_wait_ms"] is not None]
        ),
        "samples_during_backup": len(overlap),
        "overlap_response_ms": percentiles([s["response_ms"] for s in overlap]),
        "backup": backup_result,
        "backup_start_s": backup_start - origin if backup_start is not None else None,
        "backup_end_s": backup_end - origin if backup_end is not None else None,
        "peak_rss_bytes": peak["rss"],
        "peak_temporary_bytes": peak["temporary"],
        "peak_wal_bytes": peak["wal"],
        "errors": errors,
        "committed_request_indices": committed_request_indices,
        "persisted_counter": persisted_counter,
        "owner_status_at_end": owner_status_at_end,
        "owner_thread_alive_after_close": owner.storage_owner._thread.is_alive(),
        "sampler_thread_alive_after_close": sampler.is_alive(),
        "snapshot_threads_alive_after_close": sum(t.is_alive() for t in snapshot_threads),
        "resource_samples": resource_samples,
        "capture_gate_hold_ms": percentiles(capture_holds),
        "capture_gate_wait_ms": percentiles(capture_waits),
        "capture_gate_hold_samples_ms": capture_holds,
        "capture_gate_wait_samples_ms": capture_waits,
        "capture_hold_exceeded_250ms": any(value > 250 for value in capture_holds),
        "capture_wait_exceeded_2000ms": any(value > 2000 for value in capture_waits),
        "backup_load_coverage_seconds": coverage_seconds,
        "backup_load_coverage_ratio": (
            min(1.0, coverage_seconds / (backup_end - backup_start))
            if coverage_seconds is not None and backup_end > backup_start
            else None
        ),
        "samples": samples,
        "phases": phases,
        "loop_delay_ms": percentiles([row["delay_ms"] for row in loop_delays]),
        "loop_delays": loop_delays,
        "stalled_stacks": stalled_stacks,
        "component_ms": {
            key: percentiles([s[key] for s in samples if s[key] is not None])
            for key in ("queue_delay_ms", "storage_queue_ms", "query_ms", "commit_ms", "model_ms")
        },
    }
    (directory / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def render_summary(machine, results):
    """单位转换和显示舍入，不改变原始证据，不过滤失败与未覆盖部分。"""

    def text(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    def fixed(value, unit=""):
        return "未测量" if value is None else f"{value:.3f}{unit}"

    def integer_count(value):
        return f"{value:,}".replace(",", " ")

    def flag(value):
        return "未测量" if value is None else str(value).lower()

    def mib(value):
        return fixed(value / (1024 * 1024), " MiB") if value is not None else "未测量"

    lines = [
        "# 离线备份性能摘要",
        "",
        "显示精度：秒、毫秒、百分比及 MiB 保留三位小数（四舍五入）；"
        "1 MiB = 2^20 bytes。整数计数按空格分隔千位，数值不舍入；超界标记根据未舍入值计算。",
        "原始 JSON 保留在本地；本摘要仍须通过通用与私有扫描才可发布。"
        "数据不是性能通过结论，未测量不代表通过。",
        "",
        "## 机器与运行条件",
        "",
    ]
    for key, value in machine.items():
        if key == "limits":
            continue
        if key == "seed":
            value = hex(value)
        lines.append(f"- {key}: {text(value)}")
    for result in results:
        condition = "有备份" if result["with_backup"] else "无备份"
        lines.extend(["", f"## {text(result['scale'])} / {condition}", ""])
        if "repeat" in result:
            lines.append(
                f"重复序号：{result['repeat']}；组内执行顺序：{result['order']}。所有重复均保留。"
            )
        scale = result.get("scale_parameters")
        if scale:
            rows = f"{scale[0] / 1000:.3f} 千行" if scale[0] >= 10**4 else f"{scale[0]} 行"
            lines.append(
                f"规模：数据库 {rows}（每行 4 KiB）；不可变文件 {scale[1]} 个，"
                f"每文件 {mib(scale[2])}。"
            )
        lines.append(
            f"固定请求数：{result['requests']}；间隔："
            f"{fixed(result['interval_seconds'], ' s')}；备份重叠样本："
            f"{result['samples_during_backup']}。"
        )
        coverage = result.get("backup_load_coverage_ratio")
        lines.append(
            "备份期间负载窗口覆盖率："
            f"{fixed(coverage * 100 if coverage is not None else None, '%')}；"
            f"覆盖时长：{fixed(result.get('backup_load_coverage_seconds'), ' s')}。"
            "覆盖率按首个计划请求至最后完成请求的时间窗与备份时间段交集计算，"
            "不是持续占用 CPU 的比例；低于 100% 表示部分备份阶段无负载覆盖。"
        )
        lines.append(
            f"负载未覆盖完整备份（按未舍入值）："
            f"{flag(coverage < 1 if coverage is not None else None)}。"
        )
        lines.extend(
            [
                "",
                "| 指标 (ms) | 样本数 | P50 | P95 | P99 | 最大值 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        metrics = [
            ("响应", "response_ms"),
            ("写入等待", "write_wait_ms"),
            ("备份重叠响应", "overlap_response_ms"),
            ("捕获门闩持有", "capture_gate_hold_ms"),
            ("捕获门闩获取等待", "capture_gate_wait_ms"),
            ("事件循环调度延迟", "loop_delay_ms"),
            ("请求排队延迟", "queue_delay_ms"),
            ("存储所有者排队", "storage_queue_ms"),
            ("所属线程查询", "query_ms"),
            ("事务写入与提交", "commit_ms"),
            ("假模型调用", "model_ms"),
        ]
        for label, key in metrics:
            metric = result.get(key, result.get("component_ms", {}).get(key))
            columns = (
                (
                    [integer_count(metric["count"])]
                    + [fixed(metric[p]) for p in ("p50", "p95", "p99", "max")]
                )
                if metric
                else ["未测量"] * 5
            )
            lines.append(f"| {label} | " + " | ".join(columns) + " |")
        if result.get("phases"):
            lines.extend(
                ["", "| 阶段 | 起点 (s) | 终点 (s) | 关联响应 P99 (ms) |", "|---|---:|---:|---:|"]
            )
            for row in result["phases"]:
                metric = row.get("response_ms")
                lines.append(
                    f"| {text(row['name'])} | {fixed(row['start_s'])} | {fixed(row['end_s'])} | "
                    f"{fixed(metric['p99'] if metric else None)} |"
                )
            lines.append(
                "阶段关联包含计划到达至完成区间重叠的请求；只表示时间关联，不直接证明因果。"
            )
        stacks = result.get("stalled_stacks", [])
        lines.append(
            f"- 慢事件循环栈采样数：{len(stacks)}（只含代码位置，无局部变量；未采到不证明无停顿）"
        )
        frequencies = {}
        for sample in stacks:
            key = " → ".join(
                f"{row['file']}:{row['line']} {row['function']}" for row in sample["stack"][:4]
            )
            frequencies[key] = frequencies.get(key, 0) + 1
        for stack, count in sorted(frequencies.items(), key=lambda pair: (-pair[1], pair[0])):
            lines.append(f"  - {count} 次：{text(stack)}")
        lines.extend(
            [
                "",
                f"- 捕获持有超过 250 ms：{flag(result.get('capture_hold_exceeded_250ms'))}",
                f"- 捕获等待超过 2000 ms：{flag(result.get('capture_wait_exceeded_2000ms'))}",
            ]
        )
        for label, key in (
            ("RSS 峰值", "peak_rss_bytes"),
            ("临时盘峰值", "peak_temporary_bytes"),
            ("WAL 峰值", "peak_wal_bytes"),
        ):
            lines.append(f"- {label}：{mib(result.get(key))}")
        backup = result.get("backup")
        if backup:
            lines.extend(
                [
                    f"- 备份耗时：{fixed(backup['seconds'], ' s')}",
                    f"- 备份完整性：{text(backup['completeness'])}",
                    f"- 包大小：{mib(backup['package_bytes'])}；核验文件数：{backup['verified_files']}",
                    f"- 缺项数：{len(backup['missing'])}",
                ]
            )
            for missing in backup["missing"]:
                lines.append("  - " + text(json.dumps(missing, ensure_ascii=False)))
        else:
            lines.append("- 备份：未运行")
        lines.append(f"- 负载错误数：{len(result['errors'])}")
        for error in result["errors"]:
            lines.append("  - " + text(json.dumps(error, ensure_ascii=False)))
    lines.extend(["", "## 测量边界", ""])
    lines.extend(f"- {text(limit)}" for limit in machine.get("limits", []))
    return "\n".join(lines) + "\n"


async def main(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    machine = {
        "os": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "logical_cpus": os.cpu_count(),
        "processor": platform.processor(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "runner_id": (
            hashlib.sha256(os.environ["CLYTIE_BENCHMARK_RUNNER_ID"].encode()).hexdigest()
            if os.environ.get("CLYTIE_BENCHMARK_RUNNER_ID")
            else None
        ),
        "method_id": "fixed-query-coordinated-meta-fake-model-v1",
        "measurement_revision": "capture-release-and-async-measurement-teardown-v2",
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "working_tree_modified": subprocess.run(
            ["git", "diff", "--quiet"], cwd=ROOT, check=False
        ).returncode
        != 0,
        "seed": SEED,
        "conditions": "same process; sequential baseline/backup; warm OS cache; no cache flush",
        "background_load": args.conditions_note,
        "limits": [
            "Component latency: fixed-rate owner command (query + coordinated metadata commit) + zero-delay FakeModel.",
            "Real capture_online/capture_group/source/container pack+unpack; no API/network/model.",
            "Large asset uses production core ownership registry, BackupView dependency and streaming verification.",
            "RSS current working set sampled every >=50ms; short peaks may be missed.",
            "Temporary logical file sizes sampled plus final exact sum; excludes filesystem metadata.",
            "Sampler walks temporary/source trees; observer I/O overhead is included, not subtracted.",
            "Samples include scheduling delay (no coordinated omission); tails retain outliers.",
            "MockClock advanced by benchmark driver; timings use perf_counter only.",
            "Capture gate body/wait observed with wall clock; original policy/checkpoints unchanged.",
            "Measurement v2 includes underlying capture-lock release, including failed exits; previous raw results retained.",
            "Final disk scan and sampler join run off-loop and settle before return; their phases remain in raw evidence.",
            "Source assembly shares the benchmark request loop; production source assembly runs in a separate backup process.",
            "Issue #72 thresholds apply; incomplete full-window load coverage blocks acceptance.",
            "Diagnostic timings and thread stack samples add observer overhead; no local variables are collected.",
            "Phase overlap is temporal association, not proof of causation; all repeats and adverse results retained.",
        ],
    }
    (output / "conditions.json").write_text(json.dumps(machine, indent=2), encoding="utf-8")
    summary = []
    for name in args.scales:
        for repeat in range(getattr(args, "repeats", 1)):
            order = (True, False) if repeat % 2 else (False, True)
            for position, backup in enumerate(order):
                label = f"{name}-{'backup' if backup else 'baseline'}"
                if getattr(args, "repeats", 1) > 1:
                    label += f"-repeat-{repeat + 1}"
                result = await run_case(
                    output / label,
                    name,
                    backup=backup,
                    requests=args.requests,
                    interval=args.interval,
                )
                result.update(repeat=repeat + 1, order=position + 1)
                summary.append(
                    {
                        k: v
                        for k, v in result.items()
                        if k not in {"samples", "loop_delays", "resource_samples"}
                    }
                )
                print(
                    f"case complete: {name} / {'backup' if backup else 'baseline'} / repeat {repeat + 1}",
                    flush=True,
                )
                (output / "summary.json").write_text(
                    json.dumps(summary, indent=2), encoding="utf-8"
                )
                (output / "summary.md").write_text(
                    render_summary(machine, summary), encoding="utf-8"
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--requests", type=int, default=800)
    parser.add_argument("--interval", type=float, default=0.025)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--conditions-note", default="background load not controlled")
    parser.add_argument("--scales", nargs="+", choices=tuple(SCALES), default=list(SCALES))
    arguments = parser.parse_args()
    if arguments.requests < 1 or arguments.interval <= 0 or arguments.repeats < 1:
        sys.exit("requests, repeats and interval must be positive")
    asyncio.run(main(arguments))
