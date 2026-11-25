import asyncio
import base64
import hashlib
import json
import secrets
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession

from clytie.api.auth import APIError
from clytie.api.cli import CLI, plugin_command, upload_command_file
from clytie.api.operations import companion_operation
from clytie.api.server import Server
from clytie.plugin_host.uploads import CHUNK_BYTES, Uploads
from clytie.sdk.testing import MockClock
from clytie.sdk.types import ApiResponse
from clytie.supervisor.common import Audit
from clytie.supervisor.processes import Lines, frame

from .host_helpers import SOURCE, enable, host, plugin
from .test_api_management import management
from .test_supervisor import spin


async def test_upload_lifetime_integrity_and_ownership(tmp_path):
    clock = MockClock()
    store = Uploads(tmp_path, "companion_a", clock)
    raw = b"fictional"
    args = ("receiver", len(raw), hashlib.sha256(raw).hexdigest(), 40)
    first, second = (
        row["upload_id"] for row in await asyncio.gather(store.start(*args), store.start(*args))
    )
    file = store.entries[first].file
    with pytest.raises(APIError, match="upload_limit"):
        await store.start(*args)
    with pytest.raises(KeyError):
        await store.access("receiver", first, release=False)
    with pytest.raises(APIError, match="upload_size_mismatch"):
        await store.complete(first)
    with pytest.raises(APIError, match="upload_sequence"):
        await store.chunk(first, "1", base64.b64encode(raw))
    with pytest.raises(APIError, match="invalid_request"):
        await store.chunk(first, "0", "###")
    await store.chunk(first, "0", base64.b64encode(raw))
    await store.complete(first)
    for release in [True, False]:
        with pytest.raises(KeyError):
            await store.access("other", first, release=release)
    for _ in range(2):
        assert await store.access("receiver", first, release=False) == raw
    wrong = (await store.start("receiver", len(raw), "0" * 64, 40))["upload_id"]
    wrong_file = store.entries[wrong].file
    await store.chunk(wrong, "0", base64.b64encode(raw))
    with pytest.raises(APIError, match="upload_digest_mismatch"):
        await store.complete(wrong)
    assert wrong_file.closed
    await store.access("receiver", first, release=True)
    assert file.closed
    with pytest.raises(KeyError):
        await store.access("receiver", first, release=False)
    file = store.entries[second].file
    await spin()
    clock.advance(600)
    await spin()
    assert file.closed and not store.entries
    key = (await store.start(*args))["upload_id"]
    file = store.entries[key].file
    await store.close()
    assert file.closed and not tuple(store.directory.iterdir())


async def test_upload_http_cli_and_protected_plugin_route(tmp_path):
    pid = "receiver"
    source = (
        SOURCE
        + """
    async def route(self, request):
        self.seen.append(request)
        return {"status": 200, "json": {"result": "saved"}}
"""
    )
    plugin(
        tmp_path,
        pid,
        source,
        uploads={"max_bytes": CHUNK_BYTES + 5},
        contributes={
            "api_routes": [
                {
                    "path": "/plugins/receiver/save",
                    "method": "POST",
                    "handler": "route",
                    "audit": True,
                    "confirm": True,
                    "admin_password": True,
                }
            ]
        },
    )
    plugin(tmp_path, "other")
    h = host(tmp_path)
    cid = "companion_a"
    runtime = SimpleNamespace(home=tmp_path, clock=h.clock, cid=cid, host=h)
    api = management(tmp_path)
    api.svc.rows[cid]["state"] = "running"

    async def transfer(cid, operation, arguments):
        return await companion_operation(runtime, operation, arguments)

    api.transfer = transfer
    server = Server(tmp_path, h.clock, api, Audit(tmp_path, h.clock), {"port": 0})
    await server.start()
    try:
        receiver = await enable(h, pid)
        other = await enable(h, "other")
        assert not hasattr(other.ctx, "uploads")
        headers = dict([("Authorization", "Bearer " + server.auth.token)])
        async with ClientSession(headers=headers) as http:
            url = f"http://127.0.0.1:{server.port}"

            class LocalClient:
                async def request(self, method, path, *, json=None, query=None, body=None):
                    async with http.request(
                        method, url + path, json=json, params=query, data=body
                    ) as response:
                        return ApiResponse(status=response.status, json=await response.json())

            cli = CLI(LocalClient())
            prefix = f"/api/v1/companions/{cid}"
            endpoint = prefix + "/uploads"
            raw = b"X" * CHUNK_BYTES + b"hello"
            data = {"plugin": pid, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for bad, code in [
                ({**data, "path": "fake.png"}, 400),
                ({**data, "size": len(raw) + 1}, 413),
                ({**data, "plugin": "other"}, 404),
            ]:
                assert (await cli.client.request("POST", endpoint, json=bad)).status == code
            key = (await cli.client.request("POST", endpoint, json=data)).json["result"][
                "upload_id"
            ]
            assert (
                await cli.client.request("PUT", f"{endpoint}/{key}/0", body=b"x" * (1024**2 + 1))
            ).status == 413
            file = tmp_path / "fictional.bin"
            file.write_bytes(raw)
            uploaded = await upload_command_file(cli, cid, pid, str(file), len(raw))
            assert uploaded.status == 200
            upload_id = uploaded.json["result"]["upload_id"]
            assert await receiver.ctx.uploads.read(upload_id) == raw
            assert (
                await cli.client.request(
                    "POST", f"{endpoint}/{key}/complete", json={"file": "fake"}
                )
            ).status == 400
            path = f"{prefix}/plugins/{pid}/save"
            send = cli.client.request
            result = await send("POST", path, json={"confirmed": True, "password_checked": True})
            assert result.status == 428
            credential = secrets.token_urlsafe(20)
            server.auth.set_password(credential)
            assert (await send("POST", path, json={"confirm": True})).status == 401
            assert (
                await send("POST", path, json={"confirm": True, "admin_password": "invalid"})
            ).status == 401
            assert not receiver.plugin.seen
            result = await send("POST", path, json={"confirm": True, "admin_password": credential})
            assert result.status == 200
            assert receiver.plugin.seen[0]["json"] == {}
            assert str(file) not in server.audit.path.read_text(encoding="utf-8")
    finally:
        await server.close()
        await h.close()


async def test_full_chunk_frame_preserves_following_ipc_message(tmp_path):
    with (tmp_path / "frames").open("w+b") as stream:
        message = {
            "type": "request",
            "op": "upload/chunk",
            "seq": 1,
            "args": {"content": "X" * (1024 * 1024)},
        }
        next_message = {"type": "stop", "seq": 2}
        frame(stream, message)
        frame(stream, next_message)
        stream.seek(0)
        lines = Lines(stream, upload_chunks=True)
        assert await lines.receive() == message
        assert await lines.receive() == next_message
        assert await lines.receive() is None


async def test_plugin_command_uploads_user_file_before_loading_handler(tmp_path, monkeypatch):
    from .test_api_cli import FakeClient

    cid = "companion_a"
    pid = "receiver"
    file = tmp_path / "fictional.bin"
    file.write_bytes(b"example")
    fields = {name: {"type": "string"} for name in ("file", "companion_id")}
    plugin(
        tmp_path,
        pid,
        SOURCE
        + """
    async def run(self, args, api):
        assert set(args) == {"companion_id", "upload_id"}
        assert args["upload_id"] == "fixture"
        return 0
""",
        uploads={"max_bytes": 1024},
        contributes={
            "cli_commands": [
                {
                    "name": "import",
                    "handler": "run",
                    "help": "Import",
                    "args": {"type": "object", "properties": fields, "required": list(fields)},
                }
            ]
        },
    )
    report = SimpleNamespace(
        plugin_configs={cid: {"config": {"plugins": {pid: {"enabled": True}}}}}
    )
    monkeypatch.setattr("clytie.config.load_home", lambda *_: report)

    class Client(FakeClient):
        async def request(self, method, path, *, json=None, query=None, body=None):
            self.requests.append((method, path, json, body))
            return ApiResponse(status=200, json={"ok": True, "result": {"upload_id": "fixture"}})

    cli = CLI(Client(tmp_path))
    args = SimpleNamespace(
        action=pid, cid="import", pid=json.dumps({"companion_id": cid, "file": str(file)})
    )
    assert (await plugin_command(args, cli)).json["ok"]
    assert [row[0] for row in cli.client.requests] == ["GET", "POST", "PUT", "POST"]
    assert cli.client.requests[2][3] == base64.b64encode(b"example")
    assert str(file) not in str(cli.client.requests)
