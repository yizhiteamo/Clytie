"""Real maintenance HTTP/worker and separate source rotation process."""

import asyncio
import json
import shutil
import sys
from datetime import timedelta
from uuid import uuid4

from clytie.api.client import Client, detached_start
from clytie.backup.restore import digest, identity
from clytie.backup.storage import BackupStorage
from clytie.clock import SystemClock

from .management_integration_helpers import until
from .test_backup_maintenance import stop_home
from .test_backup_restore import configured_package


async def scheduled_result(awaitable):
    task = asyncio.ensure_future(awaitable)
    try:
        await until(task.done)
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


ROTATE = """
import json,sys
from clytie.backup.storage import BackupStorage
from clytie.backup.retention import RetentionConfig
from clytie.clock import SystemClock
store=BackupStorage(sys.argv[1],SystemClock())
print('ready',flush=True)
for command in sys.stdin:
    if command.strip() == 'exit': break
    result=store.rotate(RetentionConfig(daily_days=0,weekly_weeks=0,monthly_months=0,target_bytes=1),
        started_bytes=sum(r['size'] for r in store.list()),new_backup_id='backup_4')
    print(json.dumps({'status':result,'ids':[r['backup_id'] for r in store.list()]}),flush=True)
"""


async def test_real_maintenance_preview_blocks_other_process_rotation(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _, package, key, _, _ = configured_package(fixture, monkeypatch)
    source_home, management_home = tmp_path / "source_home", tmp_path / "maintenance"
    management_home.mkdir()
    clock = SystemClock()
    store = BackupStorage(source_home, clock)
    rows = []
    for number in range(5):
        path = store.root / f"{number}.clytie-backup"
        if number == 0:
            shutil.copyfile(package, path)
        else:
            path.write_bytes(b"fictional rotation-only record")
        row = dict(
            backup_id=f"backup_{number}",
            path=str(path),
            scope="instance",
            group="test_group",
            completed_at=(clock.now() - timedelta(days=5 - number)).isoformat(),
            size=path.stat().st_size,
            sha256=digest(path),
            identity=identity(path),
            manual=False,
            completeness="complete",
            missing_count=0,
        )
        store.register(row)
        rows.append(row)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        ROTATE,
        str(source_home),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert (await scheduled_result(process.stdout.readline())).strip() == b"ready"
    client = Client(management_home)
    try:
        assert (await detached_start(management_home, clock, maintenance=True))["ok"]
        operation_id = uuid4().hex
        reply = await client.request(
            "POST",
            "/api/v1/backup/restore/plan",
            json={
                "source": rows[0]["path"],
                "key_path": str(key),
                "target": str(tmp_path / "target"),
                "source_repository": {
                    "root": str(store.root),
                    "repository_id": store.repository.repository_id,
                    "backup_id": "backup_0",
                },
                "confirm": True,
                "operation_id": operation_id,
            },
        )
        assert reply.status == 200, reply.json
        for _ in range(300):
            reply = await client.request("GET", "/api/v1/backup/operations/" + operation_id)
            result = reply.json["result"]
            if result["phase"] in {"committed", "failed", "recovery_blocked", "cancelled"}:
                break
            await clock.sleep(0.1)
        assert result["phase"] == "committed", result
        plan_id = result["result"]["plan_id"]

        async def rotate():
            process.stdin.write(b"rotate\n")
            await process.stdin.drain()
            return json.loads(await scheduled_result(process.stdout.readline()))

        protected = await rotate()
        assert "backup_0" in protected["ids"]
        assert "backup_1" not in protected["ids"]
        cancel = await client.request(
            "POST", f"/api/v1/backup/plans/instance/{plan_id}/cancel", json={"confirm": True}
        )
        assert cancel.status == 200, cancel.json
        assert (store.root / "0.clytie-backup").exists()
        released = await rotate()
        assert "backup_0" not in released["ids"]
        assert not (store.root / "0.clytie-backup").exists()
        assert set(released["ids"]) == {"backup_2", "backup_3", "backup_4"}
    finally:
        await stop_home(management_home, client, clock)
        if process.returncode is None:
            process.stdin.write(b"exit\n")
            await process.stdin.drain()
            await scheduled_result(process.wait())
        assert process.returncode == 0, (await process.stderr.read()).decode()
