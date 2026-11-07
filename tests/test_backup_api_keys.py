import secrets
from types import SimpleNamespace

from aiohttp import ClientSession

from clytie.api.cli import execute
from clytie.api.server import Server
from clytie.backup.keys import BackupKeys
from clytie.supervisor.common import Audit

from .test_api_cli import FakeClient
from .test_api_management import management


async def test_key_export_http_auth_confirmation_password_and_no_secret_history(tmp_path):
    home = tmp_path / "home"
    api = management(home)
    keys = BackupKeys(home, api.clock, repository=tmp_path / "repository")
    keys.ensure()
    server = Server(home, api.clock, api, Audit(home, api.clock), {"port": 0})
    await server.start()
    admin_value = secrets.token_urlsafe(24)
    server.auth.set_password(admin_value)
    route = f"http://127.0.0.1:{server.port}/api/v1/backup/key/export"
    destination = tmp_path / "saved-key"
    try:
        async with ClientSession(trust_env=False) as client:
            async with client.post(route, json={"path": str(destination)}) as response:
                assert response.status == 401
            headers = {}
            headers["Authorization"] = "Bearer " + server.auth.token
            for extra, expected in [
                ({}, 428),
                ({"confirm": True}, 401),
                ({"confirm": True, "admin_password": admin_value}, 200),
            ]:
                async with client.post(
                    route, json={"path": str(destination), **extra}, headers=headers
                ) as response:
                    assert response.status == expected
                    body = await response.json()
            assert body["result"]["exported"]
            assert set(body["result"]) == {"exported", "fingerprint"}
        assert destination.read_bytes() == (home / "secrets/backup.key").read_bytes()
        audit = (home / "supervisor/audit.jsonl").read_text(encoding="utf-8")
        assert admin_value not in audit and server.auth.token not in audit
        assert str(destination) not in audit
    finally:
        await server.close()


async def test_backup_cli_only_translates_api_calls(tmp_path):
    client = FakeClient(tmp_path)
    args = SimpleNamespace(
        command="backup",
        action="restore",
        value="source-stopped",
        restore_id="a" * 32,
        home=tmp_path,
        yes=True,
    )
    await execute(args, client)
    assert client.requests[-1][1:3] == (
        "/api/v1/backup/restore/source-stopped",
        {"restore_id": "a" * 32},
    )
    args.action, args.value = "show-key-fingerprint", None
    await execute(args, client)
    assert client.requests[-1][:2] == ("GET", "/api/v1/backup/key/fingerprint")
