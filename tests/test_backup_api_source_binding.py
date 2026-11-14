"""真实回环鉴权在跨目录内容读取之前完成源身份确认。"""

from aiohttp import ClientSession

from clytie.api.server import Server
from clytie.backup.repository import SourceRepository
from clytie.supervisor.common import Audit

from .test_api_management import management


async def test_source_binding_confirmation_does_not_start_reader(tmp_path):
    home, source_home = tmp_path / "maintenance", tmp_path / "source"
    home.mkdir()
    api = management(home)
    root = source_home / "backups/regular"
    root.mkdir(parents=True)
    package = root / "fictional.enc"
    package.write_bytes(b"not-readable-as-a-backup")
    repository = SourceRepository.initialize(root, api.clock, owner_instance_id="source_instance")
    repository.register(
        {
            "backup_id": "backup_a",
            "path": str(package),
            "identity": repository.identity(package),
        }
    )
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    server = Server(home, api.clock, api, Audit(home, api.clock), {"port": 0})
    await server.start()
    try:
        async with ClientSession(trust_env=False) as client:
            headers = {}
            headers["Authorization"] = "Bearer " + server.auth.token
            url = f"http://127.0.0.1:{server.port}/api/v1/backup/memory/plan"
            body = {
                "companion_id": "companion_a",
                "backup_id": "backup_a",
                "source_repository": {"root": str(root), "backup_id": "backup_a"},
                "_source_repository_confirmed": True,
            }
            async with client.post(url, json=body) as response:
                assert response.status == 401
            async with client.post(url, json=body, headers=headers) as response:
                assert response.status == 428
                result = await response.json()
                assert result["error"]["code"] == "source_repository_confirmation_required"
                assert (
                    result["result"]["source_repository"]["repository_id"]
                    == repository.repository_id
                )
            body["confirm"] = True
            # Confirmation alone, without the expected identity, is insufficient.
            async with client.post(url, json=body, headers=headers) as response:
                assert response.status == 428
            assert not api.memory_workers.tasks
            assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before
    finally:
        await server.close()
