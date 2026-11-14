import secrets

from aiohttp import ClientSession

from clytie.api.server import Server
from clytie.backup.previews import PreviewCatalog
from clytie.supervisor.common import Audit

from .test_api_management import management


async def test_preview_cancel_auth_confirmation_idempotency_and_consumed_result(tmp_path):
    api = management(tmp_path)
    catalog = PreviewCatalog(tmp_path, api.clock)
    source = tmp_path / "fictional-source"
    source.write_text("fictional")
    ids = ("a" * 32, "b" * 32)
    for pid in ids:
        catalog.reference(source, pid, kind="generation")
        catalog.publish("memory", pid, source, pid, target="companion_a", fingerprint="fictional")
    catalog.accept("memory", ids[1], "c" * 32)
    server = Server(tmp_path, api.clock, api, Audit(tmp_path, api.clock), {"port": 0})
    await server.start()
    server.auth.set_password(secrets.token_urlsafe(24))
    try:
        async with ClientSession(trust_env=False) as client:
            base = f"http://127.0.0.1:{server.port}/api/v1/backup/plans/memory/"
            async with client.get(base + ids[0]) as response:
                assert response.status == 401
            headers = {}
            headers["Authorization"] = "Bearer " + server.auth.token
            async with client.get(base + ids[0], headers=headers) as response:
                assert response.status == 200
                ready = (await response.json())["result"]
                assert ready["state"] == "ready"
                assert "source" not in ready
            async with client.post(base + ids[0] + "/cancel", headers=headers, json={}) as response:
                assert response.status == 428
            for _ in range(2):
                async with client.post(
                    base + ids[0] + "/cancel", headers=headers, json={"confirm": True}
                ) as response:
                    assert response.status == 200  # No extra admin password.
                    assert (await response.json())["result"]["state"] == "cancelled"
            async with client.post(
                base + ids[1] + "/cancel", headers=headers, json={"confirm": True}
            ) as response:
                assert response.status == 409
                body = await response.json()
                assert body["error"]["code"] == "plan_in_use"
                assert body["result"]["operation_id"] == "c" * 32
            async with client.get(
                base.replace("/memory/", "/instance/") + ids[0], headers=headers
            ) as response:
                assert response.status == 404
    finally:
        await server.close()
