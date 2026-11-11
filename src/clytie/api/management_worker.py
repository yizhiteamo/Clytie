"""One accepted memory operation, isolated from the daemon's event loop."""

import asyncio
import json
import sys
import threading
from contextlib import nullcontext, redirect_stdout
from pathlib import Path

from clytie.clock import SystemClock
from clytie.memory.transfer import runtime_lock
from clytie.plugin_host.locking import process_identity
from clytie.plugin_host.snapshots import contained

from .management_journal import Journal, ManagementCancelled, current, gate
from .operations import memory_operation


async def execute(
    home,
    cid,
    operation_id,
    args,
    *,
    clock=None,
    operation=memory_operation,
    controls=None,
    hosted=None,
):
    clock = clock or SystemClock()
    if hosted is not None:
        tasks = getattr(hosted, "management_tasks", None)
        if tasks is None:
            tasks = hosted.management_tasks = set()
        task = asyncio.create_task(
            _execute_hosted(home, cid, operation_id, args, clock, operation, hosted)
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.shield(task)
    path = contained(
        home, home / "companions" / cid / "management/operations" / (operation_id + ".json")
    )
    journal = Journal.load(path, clock)
    with gate(home, cid):
        if journal.value["state"] != "accepted":
            return {"ok": False, "code": "operation_already_started"}
        with nullcontext() if hosted is not None else runtime_lock(home, cid, "memory_management"):
            journal.runtime_owned = True
            journal.value["executor"] = process_identity()
            journal.phase("preparing")
            token = current.set(journal)
            if hosted is not None:
                hosted.management_journals[operation_id] = journal
            if controls is not None:

                def control():
                    # EOF is a cancellation request, never a kill deadline or commit signal.
                    for line in controls:
                        if line.strip() == b"cancel":
                            journal.cancel()
                    journal.cancel()

                threading.Thread(target=control, daemon=True).start()
            try:
                journal.checkpoint()
                if hosted is not None:
                    result = await operation(
                        home, clock, cid, journal.value["operation"], args, hosted
                    )
                else:
                    result = await operation(home, clock, cid, journal.value["operation"], args)
                journal.committed(result)
                return {"ok": True, "result": result, "operation_id": operation_id}
            except (ManagementCancelled, asyncio.CancelledError):
                if journal.value["committed"]:
                    return {"ok": True, "result": journal.public(), "operation_id": operation_id}
                journal.recover()
                if journal.value["state"] == "interrupted":
                    journal.phase("cancelled")
                return {"ok": False, "code": journal.value["state"], "operation_id": operation_id}
            except Exception:
                # Never serialize an exception containing plugin text or sensitive inputs.
                journal.recover()
                return {
                    "ok": journal.value["committed"],
                    "code": journal.value["state"],
                    "operation_id": operation_id,
                }
            finally:
                if hosted is not None:
                    hosted.management_journals.pop(operation_id, None)
                current.reset(token)


async def _execute_hosted(home, cid, operation_id, args, clock, operation, hosted):
    from clytie.api.management_io import FileCommands, management_command
    from clytie.storage.finalization import release_finalizer, reserve_finalizer
    from clytie.storage.owner import StorageBusy, StorageFailed

    store = hosted.host.memory_store(cid)
    finalizer = None
    work = {}
    loop = asyncio.get_running_loop()
    complete = False

    def prepare():
        nonlocal finalizer

        path = contained(
            home, home / "companions" / cid / "management/operations" / (operation_id + ".json")
        )
        journal = Journal.load(path, clock)
        manager = gate(home, cid)
        manager.__enter__()
        work.update(journal=journal, gate=manager)
        if journal.value["state"] != "accepted":
            return None
        finalizer = reserve_finalizer(store)
        journal.loop = loop
        journal.runtime_owned = True
        journal.storage_finalizer = finalizer
        journal.value["executor"] = process_identity()
        journal.phase("preparing")
        journal.checkpoint()
        return journal.value["operation"]

    def recover(cancelled=False):
        journal = work["journal"]
        journal.recover(store)
        if cancelled and journal.value["state"] == "interrupted":
            journal.phase("cancelled")
        return {
            "ok": journal.value["committed"],
            "code": journal.value["state"],
            "operation_id": operation_id,
        }

    async with FileCommands(clock) as files:
        try:
            try:
                selected = await files.run(prepare)
            except ManagementCancelled:
                await files.run(work["journal"].phase, "cancelled")
                complete = True
                return {"ok": False, "code": "cancelled", "operation_id": operation_id}
            if selected is None:
                complete = True
                return {"ok": False, "code": "operation_already_started"}
            journal = work["journal"]
            binding = current.set(journal)
            hosted.management_journals[operation_id] = journal
            try:
                result = await operation(home, clock, cid, selected, args, hosted)
                await management_command(store, journal.committed, result)
                complete = True
                return {"ok": True, "result": result, "operation_id": operation_id}
            except (StorageBusy, StorageFailed):
                # 控制错误不能伪装成普通成功；真实执行责任尚未核实时保留凭证。
                raise
            except (ManagementCancelled, asyncio.CancelledError):
                result = await management_command(store, recover, True)
                complete = result["code"] != "recovery_blocked"
                if result["ok"]:
                    result["result"] = await management_command(store, journal.public)
                return result
            except Exception:
                result = await management_command(store, recover)
                complete = result["code"] != "recovery_blocked"
                return result
            finally:
                hosted.management_journals.pop(operation_id, None)
                current.reset(binding)
        finally:
            if "gate" in work:
                await files.run(work["gate"].__exit__, None, None, None)
            if complete:
                release_finalizer(finalizer, executor_finished=True, obligations_complete=True)


def main():
    home, cid, operation_id = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]
    stream = sys.stdin.buffer.raw
    try:
        args = json.loads(stream.readline())
        # The private response pipe must contain exactly one protocol frame. Plugin
        # diagnostic output is not a result and must never corrupt that frame.
        with redirect_stdout(sys.stderr):
            if len(sys.argv) > 4 and sys.argv[4] == "recover":
                from .management_journal import recover_pending

                recover_pending(home, cid, SystemClock())
                result = {"ok": True, "result": {"verified": True}}
            else:
                result = asyncio.run(execute(home, cid, operation_id, args, controls=stream))
    except Exception:
        result = {"ok": False, "code": "management_worker_failed", "operation_id": operation_id}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
