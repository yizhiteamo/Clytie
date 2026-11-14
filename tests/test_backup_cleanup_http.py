"""Real HTTP and cleanup worker; all selected packages are fictional fixtures."""

import secrets
from pathlib import Path

from aiohttp import ClientSession

from clytie.api.server import Server
from clytie.backup.workers import BackupWorkers
from clytie.supervisor.common import Audit

from .management_integration_helpers import until
from .test_api_management import management
from .test_backup_explicit_cleanup import fixture


async def test_real_http_cleanup_worker_fixed_selection_and_query(tmp_path):
    store, cleanup = fixture(tmp_path)
    api = management(store.home)
    workers = BackupWorkers(store.home, api.clock)
    api.backup_workers = workers
    server = Server(store.home, api.clock, api, Audit(store.home, api.clock), {"port": 0})
    await server.start()
    admin_value = secrets.token_urlsafe(24)
    server.auth.set_password(admin_value)
    try:
        async with ClientSession(trust_env=False) as client:
            base = f"http://127.0.0.1:{server.port}/api/v1/backup"
            headers = {}
            headers["Authorization"] = "Bearer " + server.auth.token

            async def post(endpoint, data):
                async with client.post(base + endpoint, headers=headers, json=data) as response:
                    return response.status, await response.json()

            code, body = await post(
                "/cleanup/plan",
                {"backup_ids": ["package_0", "package_4"], "operation_id": "a" * 32},
            )
            assert code == 200, body
            await until(lambda: not workers.tasks)
            assert workers.query("a" * 32)["phase"] == "committed"
            plan_id = workers.query("a" * 32)["result"]["plan_id"]
            async with client.get(base + "/cleanup/plans/" + plan_id, headers=headers) as response:
                assert response.status == 200
                plan = (await response.json())["result"]
                assert plan["eligible_ids"] == ["package_0"]
                assert plan["items"][1]["eligible"] is False
                assert all("record" not in row for row in plan["items"])
            apply = {"plan_id": plan_id, "confirm": True, "operation_id": "b" * 32}
            code, _ = await post("/cleanup/apply", apply)
            assert code == 401
            assert Path(store.get("package_0")["path"]).exists()
            apply["admin_password"] = admin_value
            code, _ = await post("/cleanup/apply", apply)
            assert code == 200
            await until(lambda: not workers.tasks)
            result = workers.query("b" * 32)
            assert result["phase"] == "committed"
            assert result["result"]["verification"] == "failed"  # Protected selection: partial.
            assert cleanup.operation(plan_id)["state"] == "partial"
            assert len(store.list()) == 4
            code, _ = await post("/cleanup/apply", apply)
            assert code == 200
            assert not workers.tasks
            assert len(store.list()) == 4
    finally:
        await workers.stop("cancel")
        await server.close()
