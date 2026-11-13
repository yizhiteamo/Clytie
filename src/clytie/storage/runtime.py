"""伴侣运行连接工厂；创建、迁移及释放均由存储所有者线程执行。"""

from clytie.api.management_journal import ManagementBusy, gate, pending, recover_locked
from clytie.backup.coordination import companion_coordinator
from clytie.kernel.ledger import Ledger, LedgerError
from clytie.memory.errors import MemoryStartupError
from clytie.memory.store import MemoryStore
from clytie.plugin_host.locking import OperationBusy

from .handle import StoreHandle


class CompanionResources:
    def __init__(self, host, runtime, failure):
        self.host, self.runtime = host, runtime
        self.memory = self.ledger = self.memory_lock = None
        try:
            with gate(host.home, runtime.cid):
                if getattr(runtime, "management_recovery_prepared", False):
                    # Recovery ran before the Owner queue existed. A management
                    # operation admitted in the gap must win admission normally;
                    # never reacquire a heavy-I/O slot inside this factory.
                    if pending(host.home, runtime.cid, host.clock):
                        raise ManagementBusy()
                else:
                    recover_locked(host.home, runtime.cid, host.clock)
                self.memory = host.memory_store(runtime.cid)
                _, self.memory_lock = host.memory_resources[runtime.cid]
                self.ledger = Ledger(
                    runtime.path,
                    runtime.cid,
                    host.clock,
                    coordination=companion_coordinator(host.home, runtime.cid, host.clock),
                )
        except BaseException as exc:
            if isinstance(exc, ManagementBusy):
                failure["reason"] = (
                    "management_recovery_blocked"
                    if (host.home / "companions" / runtime.cid / "management/blocked.json").exists()
                    else "lock_held"
                )
            elif isinstance(exc, OperationBusy):
                failure["reason"] = "lock_held"
            elif isinstance(exc, (MemoryStartupError, LedgerError)):
                failure["reason"] = "migration_blocked"
            self.close()
            raise

    def close(self):
        try:
            if self.ledger is not None:
                self.ledger.close()
                self.ledger = None
        finally:
            try:
                if self.memory is not None:
                    self.memory.close()
                    self.memory = None
            finally:
                if self.memory_lock is not None:
                    self.memory_lock.__exit__(None, None, None)
                    self.memory_lock = None
                # 工厂失败时还没有安装句柄，清理临时宿主登记。
                resource = self.host.memory_resources.get(self.runtime.cid)
                if resource and not isinstance(resource[0], StoreHandle):
                    self.host.memory_resources.pop(self.runtime.cid, None)


def bind_handles(owner, host, runtime):
    config, instance = host.memory_config[runtime.cid]
    coordinator = companion_coordinator(host.home, runtime.cid, host.clock)
    common = {"clock": host.clock, "coordination": coordinator}
    ledger = StoreHandle(
        owner,
        "ledger",
        Ledger,
        {
            **common,
            "companion_id": runtime.cid,
            "path": runtime.path,
            "readonly": False,
        },
    )
    memory = StoreHandle(
        owner,
        "memory",
        MemoryStore,
        {
            **common,
            "path": host.home / "companions" / runtime.cid / "memory/memory.sqlite3",
            "config": config,
            "instance": instance,
            "transient": False,
        },
    )
    host.memory_resources[runtime.cid] = (memory, None)
    host.storage_owners[runtime.cid] = owner
    return ledger
