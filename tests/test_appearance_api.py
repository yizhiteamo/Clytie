import argparse
import base64
import json
import re
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import make_mocked_request
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from clytie.api import appearance as routes
from clytie.api.cli import CLI, PluginCommandClient, add_parser, execute
from clytie.api.server import Server
from clytie.sdk.types import ApiResponse
from clytie.supervisor.common import Audit

from .test_api_management import management

ROOT = "/api/v1/companions/companion_a/appearance"


def png(padding=0):
    output = BytesIO()
    info = PngInfo()
    if padding:
        info.add_text("fixture", "a" * padding)
    Image.new("RGB", (7, 5), "blue").save(output, format="PNG", pnginfo=info)
    return output.getvalue()


@pytest.fixture
def api(tmp_path):
    backend = management(tmp_path)
    config = {"vision": {"max_images": 1}}
    backend.configuration = lambda: SimpleNamespace(
        plugin_configs={"companion_a": {"config": config}}
    )
    server = Server(tmp_path, backend.clock, backend, Audit(tmp_path, backend.clock))
    server.port = 8765
    return server


async def request(api, method, path, data=None, *, bearer=True):
    headers = {"Host": "localhost:8765", "Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = "Bearer " + api.auth.token
    payload = Mock()
    payload.at_eof.return_value = data is None
    payload.readany = AsyncMock(side_effect=[json.dumps(data).encode(), b""])
    req = make_mocked_request(method, path, headers=headers, payload=payload)
    result = await api.handle(req)
    return ApiResponse(status=result.status, json=json.loads(result.text))


async def test_cli_routes_confirm_password_and_private_display(api, tmp_path, monkeypatch, capsys):
    class Transport:
        async def request(self, method, path, *, json=None, query=None):
            if path == "/api/v1/status":
                return ApiResponse(status=200, json={"ok": True, "result": {}})
            return await request(api, method, path, json)

    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))

    async def run(*argv):
        args = parser.parse_args(["companion", "appearance", *argv])
        return await execute(args, Transport())

    phrase = "fictional phrase"
    api.auth.set_password(phrase)
    prompts = []

    def provide_password(prompt):
        prompts.append(prompt)
        return phrase

    monkeypatch.setattr("getpass.getpass", provide_password)
    image = tmp_path / "fixture.png"
    # Large valid input clears the normal one-MiB API limit on this route only.
    image.write_bytes(png(850_000))
    added = await run("add", "companion_a", str(image), "--label", "常服", "--yes")
    assert added.status == 200, added.json
    assert added.json["result"]["message"] == "重启伴侣后生效"
    explanation = capsys.readouterr().out
    assert "参考图会在比对和自检时发给模型服务商" in explanation
    assert re.search(r"预计用量约 [1-9]\d* token", explanation)
    assert "重启后会自动调用一次模型检查形象资料" in explanation
    show = (await run("show", "companion_a")).json["result"]
    assert show["description_present"] is False and show["self_check"] is None
    assert list(show["references"]) == [
        {"label": "常服", "state": "current", "number": 1, "width": 7, "height": 5}
    ]
    check = tmp_path / "companions/companion_a/state/appearance-check.json"
    check.parent.mkdir(parents=True, exist_ok=True)
    check.write_text(json.dumps({"result": {"state": "passed", "note": "fictional note"}}))
    assert "fictional note" in str((await run("show", "companion_a")).json)
    assert (
        await run("edit", "companion_a", "1", "--label", "礼服", "--current", "--yes")
    ).status == 200
    assert (await run("show", "companion_a")).json["result"]["references"][0]["label"] == "礼服"
    assert (await run("remove", "companion_a", "1", "--yes")).status == 200
    assert not (await run("show", "companion_a")).json["result"]["references"]
    assert len(prompts) == 3
    audit = api.audit.path.read_text(encoding="utf-8")
    for private in (phrase, str(image), "常服", "礼服", "fictional note", "iVBORw0"):
        assert private not in audit


async def test_appearance_api_rejects_path_and_preserves_auth(api):
    for action in ("", "/add", "/edit", "/remove"):
        method = "POST" if action else "GET"
        result = await request(api, method, ROOT + action, bearer=False)
        assert result.status == 401
        assert (await request(api, method, ROOT + action + "?path=fixture.png")).status == 400
        result = await request(api, method, ROOT + action, {"path": "fixture.png"})
        assert result.status == 400 and result.json["error"]["code"] == "invalid_request"
    data = {"label": "常服", "content": base64.b64encode(png()).decode()}
    for action, body in (
        ("/add", data),
        ("/edit", {"number": 1, "state": "former"}),
        ("/remove", {"number": 1}),
    ):
        assert (await request(api, "POST", ROOT + action, body)).status == 428
    assert not (await request(api, "GET", ROOT)).json["result"]["references"]
    assert (await request(api, "POST", ROOT + "/add", {**data, "confirm": True})).status == 200
    result = await request(
        api, "POST", ROOT + "/edit", {"number": 1, "state": "former", "confirm": True}
    )
    assert result.json["error"]["code"] == "current_appearance_required"


async def test_confirmation_respects_self_check_and_image_limit(api):
    files = api.backend
    config = files.configuration().plugin_configs["companion_a"]["config"]
    params = {"cid": "companion_a"}
    data = {"label": "常服", "content": base64.b64encode(png()).decode()}
    _, _, one = await files.policy(routes.ADD, params, data)
    await files.call(routes.ADD, params, data, {}, "POST")
    _, _, two = await files.policy(routes.ADD, params, data)
    assert "约 2256 token。" in one
    assert "约 2256 token。" in two  # The configured image limit is one.
    config["vision"]["max_images"] = 2
    _, _, uncapped = await files.policy(routes.ADD, params, data)
    assert "约 4256 token。" in uncapped
    (files.home / "companions/companion_a/persona/appearance.md").write_text(
        "虚构", encoding="utf-8"
    )
    _, _, with_description = await files.policy(routes.ADD, params, data)
    assert "约 4258 token。" in with_description
    _, _, removal = await files.policy(routes.ROOT + "/remove", params, {"number": 1})
    assert "0 token" in removal and "自动调用" not in removal
    config["appearance"] = {"self_check": False}
    _, _, disabled = await files.policy(routes.ADD, params, data)
    assert "自检已关闭" in disabled and "0 token" in disabled


async def test_confirmation_without_admin_password_uses_separate_sentence(api):
    result = await request(api, "POST", ROOT + "/add", {"label": "常服"})
    assert result.status == 428
    description = result.json["result"]["description"]
    assert "。；" not in description
    assert description.endswith("token。建议设置管理口令。")


async def test_upload_limits_and_readable_failure(api, monkeypatch):
    data = {"content": "invalid", "label": "常服", "confirm": True}
    response = await request(api, "POST", ROOT + "/add", data)
    assert response.json["error"]["code"] == "invalid_image"
    assert "图片损坏或格式不受支持" in response.json["error"]["message"]
    monkeypatch.setattr(routes, "UPLOAD_BODY_BYTES", 16)
    assert (await request(api, "POST", ROOT + "/add", data)).status == 413
    assert (await request(api, "POST", ROOT + "/add", data, bearer=False)).status == 401


async def test_cli_bounds_file_and_restricts_plugin_view(tmp_path):
    from clytie.appearance import MAX_BYTES

    from .test_api_cli import FakeClient

    image = tmp_path / "fictional-image"
    with image.open("wb") as source:
        source.truncate(MAX_BYTES + 1)
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["companion", "appearance", "add", "companion_a", str(image), "--label", "常服"]
    )
    client = FakeClient(tmp_path)
    assert (await execute(args, client)).json["error"]["code"] == "too_large"
    assert all(path.endswith("/status") for _, path, _, _ in client.requests)
    plugin = PluginCommandClient(CLI(client))
    assert (await plugin.request("GET", ROOT)).status == 403
