import secrets

from aiohttp import ClientSession

from clytie.api.server import Server
from clytie.supervisor.common import Audit

from .test_api_management import management


async def test_backup_routes_keep_opaque_ids_and_server_password_policy(tmp_path):
    api = management(tmp_path)
    calls = []

    class Workers:
        async def execute(self, operation, arguments, operation_id=None):
            calls.append((operation, arguments, operation_id))
            return {"id": operation_id or "1" * 32, "phase": "accepted"}

    api.backup_workers = Workers()
    server = Server(tmp_path, api.clock, api, Audit(tmp_path, api.clock), {"port": 0})
    await server.start()
    admin_value = secrets.token_urlsafe(24)
    server.auth.set_password(admin_value)
    try:
        async with ClientSession(trust_env=False) as client:
            headers = {}
            headers["Authorization"] = "Bearer " + server.auth.token
            base = f"http://127.0.0.1:{server.port}/api/v1/backup"
            async with client.post(
                base + "/verify", headers=headers, json={"backup_id": "1" * 32}
            ) as response:
                assert response.status == 200
            assert calls[-1][1]["backup_id"] == "1" * 32
            for endpoint in ("/restore/apply", "/cleanup/apply"):
                before = len(calls)
                body = {"plan_id": "2" * 32}
                async with client.post(base + endpoint, headers=headers, json=body) as response:
                    assert response.status == 428
                body["confirm"] = True
                async with client.post(base + endpoint, headers=headers, json=body) as response:
                    assert response.status == 401
                assert len(calls) == before
                body["admin_password"] = admin_value
                async with client.post(base + endpoint, headers=headers, json=body) as response:
                    assert response.status == 200
                assert calls[-1][1] == {"plan_id": "2" * 32}
            async with client.post(
                base + "/cleanup/plan",
                headers=headers,
                json={
                    "category": "regular",
                    "backup_ids": ["1" * 32],
                },
            ) as response:
                assert response.status == 200
            assert calls[-1][0] == "cleanup_plan"
            assert calls[-1][1] == {"category": "regular", "backup_ids": ["1" * 32]}
            before = len(calls)
            for invalid in (
                {"category": "regular", "backup_ids": ["*"]},
                {"category": "regular", "backup_ids": ["1" * 32], "path": "fictional"},
                {"category": "regular", "backup_ids": ["1" * 32], "source_repository": {}},
                {"category": "regular", "backup_ids": ["1" * 32, "1" * 32]},
            ):
                async with client.post(
                    base + "/cleanup/plan", headers=headers, json=invalid
                ) as response:
                    assert response.status == 400
            assert len(calls) == before
    finally:
        await server.close()
