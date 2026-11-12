"""取消接收任务不等于真实备份执行者已退出；仅使用虚构本机进程。"""

import asyncio

import pytest

from clytie.backup.workers import BackupWorkers
from clytie.clock import SystemClock
from clytie.plugin_host.support import timed


async def test_cancelled_receiver_drains_pipe_and_waits_for_actual_worker_exit(
    tmp_path, monkeypatch
):
    clock = SystemClock()
    workers = BackupWorkers(tmp_path, clock)
    operation_id = "a" * 32
    spawned = []
    spawn = asyncio.create_subprocess_exec
    script = """
import json, sys, threading
from pathlib import Path
from clytie.backup.journal import BackupJournal
from clytie.clock import SystemClock
from clytie.plugin_host.locking import process_identity
home, operation = Path(sys.argv[1]), sys.argv[2]
journal = BackupJournal.load(BackupJournal.path_for(home, operation), SystemClock())
journal.phase('preparing', executor=process_identity(), delivery='waiting')
print(json.dumps(dict(type='ready', id=operation)), flush=True)
assert sys.stdin.buffer.readline() == b'start\\n'
journal.phase('copying', delivery='received')
print(json.dumps(dict(type='started', id=operation)), flush=True)
print(json.dumps(dict(type='backup_io', id='fictional_acquire', operation_id=operation,
                     arguments=dict(action='acquire', stage='held'))), flush=True)
assert json.loads(sys.stdin.buffer.readline())['result'] == {}
(home / 'acquired').touch()
assert sys.stdin.buffer.readline() == b''
(home / 'input-closed').touch()
while not (home / 'allow-exit').exists():
    threading.Event().wait(0.01)
sys.stdout.buffer.write(b'x' * (512 * 1024))
sys.stdout.buffer.flush()
journal.phase('cancelled')
"""

    async def launch(executable, *args, **kwargs):
        process = await spawn(executable, "-c", script, *args[2:], **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)

    async def until(predicate):
        while not predicate():
            await clock.sleep(0.01)

    receiver = None
    try:
        await workers.execute("create", {"scope": "instance"}, operation_id)
        await timed(until(lambda: (tmp_path / "acquired").exists()), clock, 30)
        receiver = workers.tasks[operation_id]
        receiver.cancel()
        await timed(until(lambda: (tmp_path / "input-closed").exists()), clock, 30)
        assert not receiver.done(), "receiver exited before the real worker"
        assert operation_id in workers.tasks and operation_id in workers.processes
        assert spawned[0].returncode is None
        assert workers.io_control.budget.status()["active"]["disconnected"]
        receiver.cancel()
        await clock.sleep(0)
        assert not receiver.done(), "a repeated cancellation abandoned cleanup"
        (tmp_path / "allow-exit").touch()
        with pytest.raises(asyncio.CancelledError):
            await timed(asyncio.shield(receiver), clock, 30)
        assert spawned[0].returncode == 0
        assert spawned[0].stdout.at_eof()
        assert not workers.tasks and not workers.processes and not workers.controls
        assert not workers.io_control.sessions and not workers.io_control.retiring
        assert workers.io_control.budget.status()["active"] is None
    finally:
        (tmp_path / "allow-exit").touch()
        if receiver is not None and not receiver.done():
            await timed(asyncio.gather(receiver, return_exceptions=True), clock, 30)
        if spawned:
            process = spawned[0]
            process.stdin.close()
            await timed(process.stdout.read(), clock, 30)
            await timed(process.wait(), clock, 30)
            await process.stdin.wait_closed()
