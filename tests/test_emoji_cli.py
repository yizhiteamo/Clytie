from types import SimpleNamespace

import pytest

from clytie.api.emoji import capture
from clytie.sdk.types import ApiResponse


@pytest.mark.parametrize(
    "state,confirmed,saved",
    [
        ("complete", "y", True),
        ("complete", "n", False),
        ("incomplete", "y", False),
        ("unsupported", "y", False),
    ],
)
async def test_capture_only_saves_after_complete_and_confirmation(state, confirmed, saved):
    calls = []

    async def request(method, path, data=None, query=None):
        calls.append((method, path, data))
        value = {"session": "fictional", "state": state, "ids": ["1", "2", "3", "4"]}
        return ApiResponse(status=200, json={"ok": True, "result": value})

    cli = SimpleNamespace(request=request, input=lambda _: confirmed)
    await capture(cli, "companion_a")
    assert any(path.endswith("/apply") for _, path, _ in calls) == saved
    assert calls[-1][1].endswith("/cancel")


async def test_apply_http_requires_confirmation_and_keeps_history(tmp_path):
    import yaml
    from aiohttp import ClientSession

    from clytie.api.server import Server
    from clytie.supervisor.common import Audit

    from .test_api_management import management

    api = management(tmp_path)
    path = tmp_path / "companions/companion_a/companion.yaml"
    path.parent.mkdir(parents=True)
    original = "channels:\n  - plugin: qq-onebot\n    account: bot\n    config:\n      url: ws://127.0.0.1:1\n      emoji_ids: {}\n"
    path.write_text(original, encoding="utf-8")
    api.writer.validate = lambda: True

    async def transfer(cid, op, args):
        assert cid == "companion_a" and op == "plugin_route"
        assert args["subpath"] == "emoji/status"
        return {"status": 200, "json": {"state": "complete", "ids": ["1", "2", "3", "4"]}}

    api.transfer = transfer
    server = Server(tmp_path, api.clock, api, Audit(tmp_path, api.clock), {"port": 0})
    await server.start()
    try:
        async with ClientSession() as client:
            url = f"http://127.0.0.1:{server.port}/api/v1/companions/companion_a/emoji/apply"
            headers = {}
            headers["Authorization"] = "Bearer " + server.auth.token
            async with client.post(url, headers=headers, json={"session": "fictional"}) as r:
                assert r.status == 428
            assert path.read_text(encoding="utf-8") == original
            async with client.post(
                url, headers=headers, json={"session": "fictional", "confirm": True}
            ) as r:
                assert r.status == 200
                assert (await r.json())["result"]["restart_required"]
            assert (
                yaml.safe_load(path.read_text(encoding="utf-8"))["channels"][0]["config"][
                    "emoji_ids"
                ]["✅"]
                == "3"
            )
            assert len(list(path.parent.joinpath("config-history").iterdir())) == 1
    finally:
        await server.close()
