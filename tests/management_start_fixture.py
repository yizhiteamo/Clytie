"""Real companion entry point with a barrier inside its real startup gate."""

import asyncio
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from clytie.api import management_journal
from clytie.plugin_host.locking import process_identity
from clytie.supervisor.common import write
from clytie.supervisor.entry import companion_main


def main():
    home, cid = Path(sys.argv[1]), sys.argv[2]
    original = management_journal.gate
    armed = True

    @contextmanager
    def barrier_gate(home, companion_id):
        nonlocal armed
        with original(home, companion_id):
            yield
            # Companion.start has opened its persistent memory store, but has
            # not yet released the admission gate or connected any plugin.
            if armed:
                armed = False
                write(home / "startup-held.json", process_identity())
                while not (home / "startup-release").exists():
                    threading.Event().wait(0.01)

    management_journal.gate = barrier_gate
    asyncio.run(companion_main(home, cid))


if __name__ == "__main__":
    main()
