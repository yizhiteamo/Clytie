"""Child used only with temporary test instances; no platform or model is loaded."""

import asyncio
import sys
import threading
from pathlib import Path

from clytie.api.management_journal import current
from clytie.api.management_worker import execute
from clytie.clock import SystemClock
from clytie.memory.store import MemoryStore


async def operation(home, clock, cid, op, args):
    journal = current.get()
    path = home / "companions" / cid / "memory/memory.sqlite3"
    store = MemoryStore(path, clock=clock, config={}, instance={})
    journal.database(path)
    journal.phase("transaction")
    try:
        if op == "before_snapshot":
            from clytie.memory import transfer

            original = transfer.write_temporary

            def partial(path, data):
                original(path, data[:5])
                print("ready", flush=True)
                threading.Event().wait()

            transfer.write_temporary = partial
            transfer.Transfers(store, home, cid, runtime_owned=True)._snapshot()
        with store.transaction():
            store.audit("fictional_write")
            journal.mark_transaction(store, {"thawed": True})
            if op == "before_commit":
                print("ready", flush=True)
                await clock.sleep(60 * 60)
        print("ready", flush=True)
        await clock.sleep(60 * 60)
    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(
        execute(
            Path(sys.argv[1]),
            "companion_a",
            sys.argv[2],
            {},
            clock=SystemClock(),
            operation=operation,
        )
    )
