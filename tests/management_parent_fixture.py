"""Disposable parent of one fake management worker, used for process-loss tests."""

import json
import subprocess
import sys
import threading

from clytie.supervisor.processes import python_process


def main():
    executable, env = python_process()
    child = subprocess.Popen(
        [executable, "-m", "tests.management_crash_fixture", sys.argv[1], sys.argv[2]],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    if child.stdout.readline().strip() != b"ready":
        return 1
    print(json.dumps({"pid": child.pid}), flush=True)
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
