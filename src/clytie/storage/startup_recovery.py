"""业务连接建立前核验遗留管理操作；不恢复旧进程的调度凭证。"""

from clytie.api.management_io import FileCommands
from clytie.api.management_journal import (
    ManagementBusy,
    ManagementCancelled,
    pending,
    recover_pending,
)
from clytie.backup.io_budget import IoBudgetError
from clytie.backup.operation_slot import OperationSlot, release_operation_slot


def recovery_operations(home, cid, clock):
    """只由父进程从自己的实例日志选择授权；坏日志留给原启动检查。"""
    try:
        return tuple(journal.value["id"] for journal in pending(home, cid, clock))
    except (ManagementBusy, OSError, ValueError, KeyError):
        return ()


async def recover_before_owner(runtime):
    factory = getattr(runtime, "backup_io_client", None)

    def check():
        if runtime.stop_requested():
            raise ManagementCancelled()

    def recover_one(journal):
        check()
        if factory is None:
            # Embedded/offline callers without an instance control transport.
            # Production companion_main always installs the parent's client.
            return journal.recover()
        slot = OperationSlot(
            factory(journal.value["id"], check=check),
            runtime.home,
            runtime.cid,
            journal.value["id"],
        )
        try:
            slot.acquire()
        except IoBudgetError as exc:
            # New or unverified work is not authorization to recover on Owner.
            raise ManagementBusy() from exc
        with slot.borrow(slot.binding, cleanup=True):
            result = journal.recover()
        if journal.value["state"] != "recovery_blocked":
            release_operation_slot(slot, journal)
        # A blocked/exceptional recovery remains held until the parent confirms
        # this startup executor actually exited; no speculative slot reclaim.
        return result

    def prepare():
        check()
        if pending(runtime.home, runtime.cid, runtime.clock):
            recover_pending(runtime.home, runtime.cid, runtime.clock, recover_one=recover_one)

    async with FileCommands(runtime.clock) as worker:
        await worker.run(prepare)
