"""记忆管理仅用虚构临时实例，共享真实备份通道与实例槽位。"""

import asyncio
import threading
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from clytie.api.management_io import FileCommands
from clytie.api.management_workers import Workers
from clytie.backup.controller import bind
from clytie.backup.io_client import IoClient, checkpoint, current_client, io_checks, io_scope
from clytie.backup.io_control import IoControl
from clytie.backup.io_protocol import BudgetEndpoint
from clytie.backup.workers import BackupWorkers
from clytie.clock import SystemClock
from clytie.memory.store import MemoryStore
from clytie.plugin_host.support import timed

from .test_backup_memory_source import backup
from .test_config import make_home
from .test_storage_management import managed as managed


def test_bind_management_and_backup_share_instance_control(tmp_path):
    clock = SystemClock()
    supervisor = SimpleNamespace(backup_io=IoControl(clock))
    memory = Workers(tmp_path, clock)
    backups = BackupWorkers(tmp_path, clock)
    bind(supervisor, backups, memory_workers=memory)
    assert memory.io_control is backups.io_control is supervisor.backup_io


async def test_file_commands_transfer_only_trusted_io_context():
    clock = SystemClock()
    unrelated = ContextVar("fictional_unrelated", default=None)
    client = IoClient(lambda arguments, **kwargs: {})
    caller = threading.get_ident()
    checked = []

    def inspect():
        checkpoint()
        assert current_client() is client
        assert unrelated.get() is None
        return threading.get_ident()

    binding = unrelated.set("must not cross file boundary")
    try:
        with io_scope(client), io_checks(lambda: checked.append(threading.get_ident())):
            async with FileCommands(clock) as files:
                worker = await files.run(inspect)
        assert worker != caller and checked == [worker]
    finally:
        unrelated.reset(binding)


def external_backup(tmp_path):
    from clytie.backup.repository import SourceRepository

    home, _, _, _ = make_home(tmp_path)
    clock = SystemClock()
    source_home = tmp_path / "source"
    store = MemoryStore(source_home / "memory.sqlite3", clock=clock, config={}, instance={})
    try:
        store.write(
            {"kind": "fact", "content": "虚构的共享通道记录", "visibility": "public"},
            scene="owner_private",
            conversation_id="test_group",
        )
        package, binding = backup(store, source_home, tmp_path)
    finally:
        store.close()
    root = source_home / "backups/regular"
    root.mkdir(parents=True)
    source = root / "fictional.clytie-backup"
    package.replace(source)
    repository = SourceRepository.initialize(root, clock, owner_instance_id="source_instance")
    repository.register(
        dict(backup_id=binding["backup_id"], path=str(source), identity=repository.identity(source))
    )
    return home, dict(
        backup_id=binding["backup_id"],
        source_repository=dict(
            root=str(root), repository_id=repository.repository_id, backup_id=binding["backup_id"]
        ),
        key_path=str(source_home / "secrets/backup.key"),
        _source_repository_confirmed=True,
        mode="new",
        confirm_identity=True,
    )


@pytest.mark.parametrize("cancel", [False, True])
async def test_real_management_process_waits_for_shared_backup_slot(tmp_path, cancel):
    from clytie.api.auth import APIError

    home, arguments = external_backup(tmp_path)
    clock = SystemClock()
    control = IoControl(clock)
    workers = Workers(home, clock)
    workers.io_control = control
    blocker = BudgetEndpoint(control.budget, executor="backup_executor", operation="backup_a")
    await blocker.request(dict(action="acquire", stage="held"))
    call = asyncio.create_task(workers.execute("companion_a", "backup/plan", arguments))

    async def waiting():
        while not control.budget.status()["waiting"] and not call.done():
            await clock.sleep(0.01)

    try:
        await timed(waiting(), clock, 30)
        assert control.budget.status()["waiting"] == 1
        assert not call.done()
        if cancel:
            await timed(workers.stop("cancel"), clock, 30)
            assert control.budget.status()["waiting"] == 0
    finally:
        await blocker.request(dict(action="release", stage="held"))
        if cancel:
            with pytest.raises(APIError, match="cancelled"):
                await timed(call, clock, 30)
        else:
            result = await timed(call, clock, 30)
            assert result["id"]
    assert control.budget.status()["active"] is None
    assert not control.sessions and not control.retiring


@pytest.mark.parametrize("managed", [False], indirect=True)
async def test_online_memory_source_shares_slot_and_preserves_storage_owner(managed, tmp_path):
    from clytie.api.management_journal import Journal
    from clytie.api.management_worker import execute
    from clytie.backup.coordination import run_sync
    from clytie.backup.io_channel import IoChannel

    runtime, memory, _, _ = managed
    _, arguments = external_backup(tmp_path / "external")
    runtime.management_journals = {}
    clock = SystemClock()
    control = IoControl(clock)
    session = control.register()
    journal = Journal.create(runtime.home, runtime.cid, "backup/plan", runtime.clock)
    operation = journal.value["id"]
    control.authorize(session, operation)
    loop = asyncio.get_running_loop()

    async def reply(value):
        channel.receive(value)

    channel = IoChannel(
        lambda value: loop.call_soon_threadsafe(lambda: control.dispatch(session, value, reply))
    )
    runtime.backup_io_client = channel.client
    blocker = BudgetEndpoint(control.budget, executor="backup_executor", operation="backup_a")
    await blocker.request(dict(action="acquire", stage="held"))
    call = asyncio.create_task(
        execute(
            runtime.home, runtime.cid, operation, arguments, clock=runtime.clock, hosted=runtime
        )
    )

    async def waiting():
        while not control.budget.status()["waiting"] and not call.done():
            await clock.sleep(0.01)

    try:
        await timed(waiting(), clock, 30)
        assert control.budget.status()["waiting"] == 1
        # The business connection continues on its original owner while file I/O waits.
        assert await run_sync(memory, memory.all_records) == []
    finally:
        await blocker.request(dict(action="release", stage="held"))
        result = await timed(call, clock, 30)
        await control.ended(session, confirmed=True)
        control.finish_operation(operation)
    assert result["ok"] and result["result"]["id"]
    assert not runtime.management_journals and channel.pending_count == 0
    assert memory._owner.status()["finalizers"] == 0


async def test_real_worker_eof_retains_slot_until_actual_process_exit(tmp_path, monkeypatch):
    from clytie.api.auth import APIError

    clock = SystemClock()
    workers = Workers(tmp_path, clock)
    launched = []
    spawn = asyncio.create_subprocess_exec
    script = """
import json, os, sys
from pathlib import Path
from clytie.api.management_journal import Journal
from clytie.clock import SystemClock
home, cid, operation = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
json.loads(sys.stdin.buffer.readline())
journal = Journal.load(home / 'companions' / cid / 'management/operations' / (operation + '.json'), SystemClock())
journal.phase('preparing')
print(json.dumps(dict(type='backup_io', id='fictional_acquire', operation_id=operation,
                     arguments=dict(action='acquire', stage='native_validation'))), flush=True)
response = json.loads(sys.stdin.buffer.readline())
assert response['result'] == {}
os.close(sys.stdout.fileno())
assert sys.stdin.buffer.readline() == b'finish\\n'
"""

    async def launch(executable, *args, **kwargs):
        process = await spawn(executable, "-c", script, *args[2:], **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    call = asyncio.create_task(workers.execute("companion_a", "backup/plan", {}))

    async def disconnected():
        while True:
            active = workers.io_control.budget.status()["active"]
            if active is not None and active["disconnected"]:
                return
            if call.done():
                return
            await clock.sleep(0.01)

    try:
        await timed(disconnected(), clock, 30)
        assert launched[0].returncode is None and not call.done()
        assert workers.io_control.budget.status()["active"]["disconnected"]
        assert workers.io_control.sessions
    finally:
        if launched and launched[0].returncode is None:
            launched[0].stdin.write(b"finish\n")
            await launched[0].stdin.drain()
        with pytest.raises(APIError, match="management_result_unconfirmed"):
            await timed(call, clock, 30)
    assert not workers.tasks and not workers.io_control.sessions
    assert workers.io_control.budget.status()["active"] is None


@pytest.mark.parametrize("lost", [False, True])
async def test_online_admission_authorizes_and_retires_only_settled_operation(tmp_path, lost):
    from clytie.api.management import Management
    from clytie.api.management_journal import Journal

    from .test_supervisor import make

    supervisor, launcher, clock = await make(tmp_path)
    api = Management(supervisor, None)
    child = launcher.children[0][1]
    original = child.send
    operations = []
    session = supervisor.backup_io_sessions["companion_0"]

    async def send(message):
        if message["type"] != "request":
            return await original(message)
        operation = message["args"]["operation_id"]
        operations.append(operation)
        assert operation in supervisor.backup_io.sessions[session].endpoints
        if lost:
            raise OSError("fictional uncertain delivery")
        journal = Journal.load(
            tmp_path / "companions/companion_0/management/operations" / (operation + ".json"),
            clock,
        )
        journal.committed({"thawed": True})
        await child.queue.put(
            dict(type="response", id=message["id"], ok=True, result=dict(ok=True, result={}))
        )

    child.send = send
    try:
        call = api.call(
            "/companions/<cid>/memory/thaw",
            {"cid": "companion_0"},
            {"record_id": "fictional_record"},
            {},
            "POST",
        )
        if lost:
            with pytest.raises(OSError, match="fictional uncertain delivery"):
                await call
        else:
            await call
        assert (operations[0] in supervisor.backup_io.sessions[session].endpoints) is lost
    finally:
        await supervisor.stop()


async def test_real_worker_diagnostic_stdout_cannot_impersonate_io_or_final_result(
    tmp_path, monkeypatch
):
    clock = SystemClock()
    workers = Workers(tmp_path, clock)
    spawn = asyncio.create_subprocess_exec
    script = """
import sqlite3, sys
from clytie.api import operations
from clytie.api.management_io import file_call
from clytie.backup.native_sqlite import validation_stage, native_checks
async def operation(*args):
    print('{"type":"backup_io","operation_id":"untrusted"}', flush=True)
    print('{"ok":false,"code":"fictional_plugin_text"}', flush=True)
    def validate():
        with validation_stage():
            db = sqlite3.connect(':memory:')
            try:
                with native_checks(db):
                    assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            finally:
                db.close()
        return {'validated': True}
    return await file_call(None, validate)
operations.memory_operation = operation
from clytie.api.management_worker import main
main()
"""

    async def launch(executable, *args, **kwargs):
        return await spawn(executable, "-c", script, *args[2:], **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    result = await timed(workers.execute("companion_a", "backup/plan", {}), clock, 30)
    assert result["validated"]
    assert not workers.io_control.sessions and not workers.io_control.retiring
