"""Kill-point fixture using the production MemoryAPI with a fictional local instance."""

import sys
import threading
from pathlib import Path

from clytie.api.management_journal import Journal, current
from clytie.api.memory import MemoryAPI
from clytie.clock import SystemClock
from clytie.memory import package
from clytie.memory.store import MemoryStore


def main():
    home, repo = Path(sys.argv[1]), Path(sys.argv[2])
    operation, key, stage = sys.argv[3:]
    clock = SystemClock()
    journal = Journal.load(
        home / "companions/companion_a/management/operations" / (key + ".json"), clock
    )

    def halt():
        print("ready", flush=True)
        threading.Event().wait()

    if stage == "public_replaced":
        original = package.atomic_write

        def write(path, data):
            original(path, data)
            if path.name == "memory-recovery.pub":
                halt()

        package.atomic_write = write
    elif stage == "before_audit":
        original_audit = MemoryStore.audit

        def audit(store, kind, **fields):
            if kind in {"memory_export", "memory_recovery_rotated"}:
                halt()
            return original_audit(store, kind, **fields)

        MemoryStore.audit = audit
    else:
        journal.committed = lambda *args: halt()
    current.set(journal)
    api = MemoryAPI(home, repo_root=repo, clock=clock)
    if operation == "export":
        api.export("companion_a", home / "fictional.enc", passphrase="fictional-long-passphrase")
    else:
        api.new_recovery_code("companion_a", confirmed=True)


if __name__ == "__main__":
    main()
