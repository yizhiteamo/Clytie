"""One accepted memory operation, isolated from the daemon's event loop."""

import asyncio
import json
import sys
import threading
from contextlib import ExitStack, nullcontext, redirect_stdout
from pathlib import Path

from clytie.backup.io_channel import IoChannel
from clytie.backup.io_client import io_checks, io_scope
from clytie.backup.operation_slot import OperationSlot, operation_scope, release_operation_slot
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
    io_channel=None,
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
                    try:
                        for line in controls:
                            if line.strip() == b"cancel":
                                journal.cancel()
                            elif io_channel is not None:
                                try:
                                    io_channel.receive(json.loads(line))
                                except (ValueError, TypeError):
                                    continue
                    finally:
                        journal.cancel()
                        if io_channel is not None:
                            io_channel.disconnect()

                threading.Thread(target=control, daemon=True).start()
            client = (
                io_channel.client(operation_id, check=journal.checkpoint)
                if io_channel is not None
                else None
            )
            scope = ExitStack()
            scope.enter_context(io_scope(client))
            scope.enter_context(io_checks(journal.checkpoint))
            slot = None
            complete = False
            try:
                journal.checkpoint()
                if client is not None:
                    from .management_io import FileCommands

                    slot = OperationSlot(client, home, cid, operation_id)
                    async with FileCommands(clock) as files:
                        await files.run(slot.acquire)
                    scope.enter_context(operation_scope(slot.binding))
                if hosted is not None:
                    result = await operation(
                        home, clock, cid, journal.value["operation"], args, hosted
                    )
                else:
                    result = await operation(home, clock, cid, journal.value["operation"], args)
                journal.committed(result)
                complete = True
                return {"ok": True, "result": result, "operation_id": operation_id}
            except (ManagementCancelled, asyncio.CancelledError):
                if journal.value["committed"]:
                    complete = True
                    return {"ok": True, "result": journal.public(), "operation_id": operation_id}
                from .management_io import FileCommands

                async with FileCommands(clock) as files:
                    await files.run(journal.recover, _cleanup=True)
                complete = journal.value["state"] != "recovery_blocked"
                if journal.value["state"] == "interrupted":
                    journal.phase("cancelled")
                return {"ok": False, "code": journal.value["state"], "operation_id": operation_id}
            except Exception:
                # Never serialize an exception containing plugin text or sensitive inputs.
                from .management_io import FileCommands

                async with FileCommands(clock) as files:
                    await files.run(journal.recover, _cleanup=True)
                complete = journal.value["state"] != "recovery_blocked"
                return {
                    "ok": journal.value["committed"],
                    "code": journal.value["state"],
                    "operation_id": operation_id,
                }
            finally:
                scope.__exit__(None, None, None)
                if slot is not None and slot.held and complete:
                    from .management_io import FileCommands, settle

                    async with FileCommands(clock) as files:
                        await settle(
                            asyncio.create_task(
                                files.run(release_operation_slot, slot, journal, _cleanup=True)
                            )
                        )
                if hosted is not None:
                    hosted.management_journals.pop(operation_id, None)
                current.reset(token)


async def _execute_hosted(home, cid, operation_id, args, clock, operation, hosted):
    from clytie.api.management_io import FileCommands, management_command, settle
    from clytie.storage.finalization import release_finalizer, reserve_finalizer
    from clytie.storage.owner import StorageBusy, StorageFailed

    store = hosted.host.memory_store(cid)
    finalizer = None
    work = {}
    loop = asyncio.get_running_loop()
    complete = False
    slot = None

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
            factory = getattr(hosted, "backup_io_client", None)
            client = factory(operation_id, check=journal.checkpoint) if factory else None
            scope = ExitStack()
            scope.enter_context(io_scope(client))
            scope.enter_context(io_checks(journal.checkpoint))
            try:
                if client is not None:
                    slot = OperationSlot(client, home, cid, operation_id)
                    await files.run(slot.acquire)
                    scope.enter_context(operation_scope(slot.binding))
                result = await operation(home, clock, cid, selected, args, hosted)
                await management_command(store, journal.committed, result, _cleanup=True)
                complete = True
                return {"ok": True, "result": result, "operation_id": operation_id}
            except (StorageBusy, StorageFailed):
                # 控制错误不能伪装成普通成功；真实执行责任尚未核实时保留凭证。
                raise
            except (ManagementCancelled, asyncio.CancelledError):
                result = await management_command(store, recover, True, _cleanup=True)
                complete = result["code"] != "recovery_blocked"
                if result["ok"]:
                    result["result"] = await management_command(
                        store, journal.public, _cleanup=True
                    )
                return result
            except Exception:
                result = await management_command(store, recover, _cleanup=True)
                complete = result["code"] != "recovery_blocked"
                return result
            finally:
                scope.__exit__(None, None, None)
                hosted.management_journals.pop(operation_id, None)
                current.reset(binding)
        finally:

            async def finish():
                if "gate" in work:
                    await files.run(work["gate"].__exit__, None, None, None)
                if complete:
                    if slot is not None and slot.held:
                        await files.run(
                            release_operation_slot, slot, work["journal"], _cleanup=True
                        )
                    release_finalizer(finalizer, executor_finished=True, obligations_complete=True)

            await settle(asyncio.create_task(finish()))


def recover_only(home, cid, clock, channel, controls):
    """旧进程已退出后的日志核验：新执行资格，不恢复旧内存凭证。"""
    from .management_journal import recover_pending

    cancelled = threading.Event()

    def receive():
        try:
            for line in controls:
                if line.strip() == b"cancel":
                    cancelled.set()
                else:
                    try:
                        channel.receive(json.loads(line))
                    except (TypeError, ValueError):
                        continue
        finally:
            cancelled.set()
            channel.disconnect()

    def check():
        channel.check_connected()
        if cancelled.is_set():
            raise ManagementCancelled()

    def recover(journal):
        slot = OperationSlot(
            channel.client(journal.value["id"], check=check), home, cid, journal.value["id"]
        )
        slot.acquire()
        with slot.borrow(slot.binding, cleanup=True):
            journal.recover()
        if journal.value["state"] != "recovery_blocked":
            release_operation_slot(slot, journal)

    threading.Thread(target=receive, daemon=True).start()
    recover_pending(home, cid, clock, recover_one=recover)


def main():
    home, cid, operation_id = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]
    stream = sys.stdin.buffer.raw
    protocol = sys.stdout.buffer
    output_lock = threading.Lock()

    def emit(value):
        with output_lock:
            protocol.write(json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n")
            protocol.flush()

    channel = IoChannel(emit)
    try:
        args = json.loads(stream.readline())
        # The response pipe has internal I/O frames and exactly one final result.
        # Arbitrary plugin stdout remains diagnostic output, never protocol data.
        with redirect_stdout(sys.stderr):
            if len(sys.argv) > 4 and sys.argv[4] == "recover":
                recover_only(home, cid, SystemClock(), channel, stream)
                result = {"ok": True, "result": {"verified": True}}
            else:
                result = asyncio.run(
                    execute(home, cid, operation_id, args, controls=stream, io_channel=channel)
                )
    except Exception:
        result = {"ok": False, "code": "management_worker_failed", "operation_id": operation_id}
    emit(result)


if __name__ == "__main__":
    main()
