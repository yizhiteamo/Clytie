"""只读任务查询；不创建账本、不取得运行锁、不执行恢复。"""

import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from clytie.config import resolve_home
from clytie.plugin_host.snapshots import contained, identifier

from .ledger import EXEC_TERMINAL, Ledger, LedgerError


def lock_held(path):
    """查询 OS 锁，不通过试拿锁来影响并发启动。"""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.ReadFile.argtypes = [
                wintypes.HANDLE,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.c_void_p,
            ]
            kernel.ReadFile.restype = wintypes.BOOL
            count, buffer = wintypes.DWORD(), ctypes.create_string_buffer(1)
            if kernel.ReadFile(
                msvcrt.get_osfhandle(descriptor), buffer, 1, ctypes.byref(count), None
            ):
                return False
            error = ctypes.get_last_error()
            if error == 33:
                return True
            raise ctypes.WinError(error)
        stat = os.fstat(descriptor)
        target = (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino)
        for line in Path("/proc/locks").read_text().splitlines():
            fields = line.split()
            if "FLOCK" not in fields or "->" in fields:
                continue
            device = next((x for x in fields if x.count(":") == 2), None)
            if device:
                major, minor, inode = device.split(":")
                if (int(major, 16), int(minor, 16), int(inode)) == target:
                    return True
        return False
    finally:
        os.close(descriptor)


def elapsed(start, end):
    return (
        max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
        if start and end
        else None
    )


def task_origin(record):
    return "proactive" if record.get("proactive") else "message"


def task_view(ledger, record, *, full=False):
    trace = record["trace_id"]
    invocations = [i for i in ledger.rows("invocation") if i["trace_id"] == trace]
    deliveries = []
    for i in ledger.intents(trace):
        attempts = [a for a in ledger.rows("attempt") if a["intent_id"] == i["intent_id"]]
        deliveries.append(
            dict(
                intent_id=i["intent_id"],
                kind=i["kind"],
                split=i["split"],
                status=i["status"],
                status_reaction=i["status_reaction"],
                reaction_state=i["reaction_state"],
                error=i["error"],
                attempts=attempts,
            )
        )
    parent = ledger.get("record", record["parent_trace_id"]) if record["parent_trace_id"] else None
    unfinished = (
        record["exec_status"] not in EXEC_TERMINAL or record["delivery_status"] == "pending"
    )
    task = {
        k: record[k]
        for k in (
            "task_id",
            "companion_id",
            "kind",
            "tool",
            "requester",
            "conversation_id",
            "exec_status",
            "delivery_status",
            "error",
        )
    }
    task.update(
        origin=task_origin(record),
        model_calls=record.get("model_calls", 0),
        tool_rounds=record.get("tool_rounds", 0),
        timings=record_timings(record),
        parent_task_id=parent["task_id"] if parent else None,
        child_task_ids=[
            r["task_id"]
            for r in ledger.rows("record")
            if r["parent_trace_id"] == trace and r["task_id"]
        ],
        deliveries=deliveries,
        asset_ids=list(dict.fromkeys(a for i in invocations for a in i["asset_ids"])),
        unrecovered=bool(unfinished and not lock_held(ledger.path.parent / ".lock")),
    )
    if full:
        inbound = ledger.source_inbound(record)
        task["content"] = dict(
            inbound=inbound.get("message") if inbound else None,
            invocations=[{k: i[k] for k in ("tool", "args", "result")} for i in invocations],
            outbound=[
                dict(intent_id=i["intent_id"], parts=i["parts"]) for i in ledger.intents(trace)
            ],
        )
    else:
        for delivery in deliveries:
            if delivery["error"]:
                delivery["error"].pop("message", None)
            for attempt in delivery["attempts"]:
                if attempt["receipt"] and attempt["receipt"].get("error"):
                    attempt["receipt"]["error"].pop("message", None)
    return task


def record_timings(record):
    start = record.get("execution_started_at") or record["started_at"]
    return dict(
        queue_seconds=elapsed(record["created_at"], start),
        execution_seconds=elapsed(start, record["finished_at"]),
        delivery_seconds=elapsed(record["finished_at"], record.get("delivery_finished_at")),
    )


def local_task(task_id, *, home=None, companion_id=None, full=False, repo_root=None):
    warnings, found = [], []

    def error(code, message, companions=()):
        return dict(
            ok=False,
            error=dict(code=code, message=message, companions=list(companions)),
            warnings=warnings,
        )

    if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{8}", task_id):
        return error("invalid_request", "task_id must be eight lowercase hexadecimal characters")
    try:
        root = resolve_home(home, repo_root=repo_root)
        if companion_id:
            ids = [identifier(companion_id)]
        else:
            ids = (
                sorted(p.name for p in (root / "companions").iterdir() if p.is_dir())
                if (root / "companions").is_dir()
                else []
            )
    except (OSError, ValueError):
        return error("invalid_request", "invalid instance or companion path")
    for cid in ids:
        ledger = None
        try:
            path = contained(
                root, root / "companions" / identifier(cid) / "ledger" / "ledger.sqlite3"
            )
            ledger = Ledger(path, cid, None, readonly=True)
            records = [r for r in ledger.rows("record") if r["task_id"] == task_id]
            for record in records:
                found.append(task_view(ledger, record, full=full))
        except LedgerError as exc:
            warnings.append(dict(companion_id=cid, code=exc.code, message=str(exc)))
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            warnings.append(
                dict(
                    companion_id=cid,
                    code="ledger_corrupt",
                    message="ledger or lock state cannot be read",
                )
            )
        finally:
            if ledger:
                ledger.close()
    if len(found) > 1:
        return error(
            "ambiguous", "task id exists in multiple companions", [t["companion_id"] for t in found]
        )
    if found:
        return dict(ok=True, task=found[0], warnings=warnings)
    if warnings:
        code = warnings[0]["code"] if companion_id else "ledger_unavailable"
        return error(
            code,
            "ledger unavailable; cannot determine whether task exists",
            [w["companion_id"] for w in warnings],
        )
    return error("not_found", "task not found")


def task_exit_code(result):
    if result["ok"]:
        return 0
    return {"not_found": 1, "ambiguous": 2, "invalid_request": 4}.get(result["error"]["code"], 3)
