from types import SimpleNamespace

import pytest

from clytie.cli import main
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
def test_capture_only_saves_after_complete_and_confirmation(
    tmp_path, monkeypatch, state, confirmed, saved, capsys
):
    calls = []
    prefix = "/api/v1/companions/companion_a/plugins/qq-onebot/"
    session = "fictional"

    async def request(method, path, *, json=None, query=None):
        calls.append((method, path, dict(json) if json is not None else None))
        if path == "/api/v1/status":
            value = {}
        elif path == prefix + "emoji/start":
            assert json == {"seconds": 180}
            value = {"session": session}
        elif path == prefix + "emoji/status":
            assert query == {"session": session, "wait": "1"}
            value = {"state": state, "ids": ["1", "2", "3", "4"]}
        elif path == prefix + "config/apply":
            if "confirm" not in json:
                assert json == {"session": session}
                return ApiResponse(
                    status=428,
                    json={
                        "ok": False,
                        "error": {"code": "confirmation_required"},
                        "result": {"description": "保存配置"},
                    },
                )
            assert json == {"session": session, "confirm": True}
            value = {"restart_required": True}
        else:
            assert path == prefix + "emoji/cancel" and json == {"session": session}
            value = {}
        return ApiResponse(status=200, json={"ok": True, "result": value})

    client = SimpleNamespace(request=request, home=tmp_path)
    monkeypatch.setattr("clytie.api.cli.Client", lambda _: client)
    monkeypatch.setattr("clytie.supervisor.entry.status", lambda _: {"running": True})
    # This channel is enabled through a role binding without a separate enabled flag.
    report = SimpleNamespace(
        plugin_configs={"companion_a": {"config": {}, "plugins": {"qq-onebot": {}}}}
    )
    monkeypatch.setattr("clytie.config.load_home", lambda _: report)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: confirmed)
    code = main(
        [
            "plugin",
            "qq-onebot",
            "emoji-capture",
            '{"companion":"companion_a"}',
            "--home",
            str(tmp_path),
        ]
    )
    assert code == (0 if saved else 1)
    assert sum(
        1 for _, path, body in calls if path.endswith("/apply") and body.get("confirm")
    ) == int(saved)
    assert calls[-1][1].endswith("/cancel")
    assert "请在一条没有其他表情的消息上添加" in capsys.readouterr().out


async def test_apply_http_requires_confirmation_and_keeps_history(tmp_path, monkeypatch):
    import json

    import yaml
    from aiohttp import ClientSession, ClientTimeout

    from clytie.api.operations import companion_operation
    from clytie.api.server import Server
    from clytie.plugin_host.manifests import read_manifest
    from clytie.supervisor.common import Audit

    from .test_api_management import management
    from .test_config import make_home

    home, repo, _, _ = make_home(tmp_path)
    monkeypatch.setattr("clytie.config.repository_root", lambda: repo)
    monkeypatch.setattr("clytie.api.config_write.repository_root", lambda: repo)
    pid = "fake-channel"
    manifest_path = repo / "plugins" / pid / "plugin.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document.update(config_writable=["setting"], requires={"sdk": ">=0.1,<1"})
    document["config"]["properties"]["setting"] = {"type": "integer"}
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    runtime_plugin = SimpleNamespace(manifest=read_manifest(manifest_path, repo_root=repo))
    changes = {"setting": 7}
    proposal = {"changes": changes}
    proposal_status = 200
    calls = []

    async def propose(request):
        assert request == {
            "method": "GET",
            "query": {"session": "fictional"},
            "json": {},
            "subpath": "config/proposal",
        }
        calls.append(request)
        return {"status": proposal_status, "json": proposal}

    def registration(subpath):
        return SimpleNamespace(
            plugin_id=pid,
            declaration={"method": "GET", "path": f"/plugins/{pid}/{subpath}"},
            implementation=propose,
        )

    layouts = [{"role": "channel", "account": "example_bot"}]
    routes = [registration("config/proposal")]
    host = SimpleNamespace(
        registrations=lambda *_: routes,
        is_active=lambda cid, target: target == pid,
        unit_configs={("companion_a", pid): layouts},
        runtimes={("companion_a", pid): runtime_plugin},
    )
    api = management(home)
    api.svc.rows["companion_a"]["state"] = "running"
    runtime = SimpleNamespace(home=home, clock=api.clock, cid="companion_a", host=host)
    api.transfer = lambda cid, op, args: companion_operation(runtime, op, args)
    path = home / "companions/companion_a/companion.yaml"
    original = path.read_bytes()
    server = Server(home, api.clock, api, Audit(home, api.clock), {"port": 0})
    await server.start()
    try:
        headers = {}
        headers["Authorization"] = "Bearer " + server.auth.token
        async with ClientSession(timeout=ClientTimeout(total=5), headers=headers) as client:
            base = f"http://127.0.0.1:{server.port}/api/v1/companions/companion_a"
            url = base + f"/plugins/{pid}/config/apply"
            async with client.post(base + "/emoji/apply", json={"confirm": True}) as removed:
                assert removed.status == 404

            async def request(body, path=url):
                async with client.post(path, json=body) as response:
                    return response.status, await response.json()

            assert (await request({"session": "fictional"}))[0] == 428
            assert not calls
            confirmed = {"session": "fictional", "confirm": True}
            assert (await request({**confirmed, "pid": "fake-model"}))[0] == 400
            assert (await request(confirmed, base + "/plugins/fake-model/config/apply"))[0] == 404
            changes["plugins"] = {"fake-model": {"enabled": False}}
            assert (await request(confirmed))[1]["error"]["code"] == "config_not_writable"
            del changes["plugins"]
            proposal["account"] = "other"
            assert (await request(confirmed))[1]["error"]["code"] == "plugin_invalid_response"
            del proposal["account"]
            proposal_status = 409
            error_code = "proposal_unavailable"
            assert (await request(confirmed))[1]["error"]["code"] == error_code
            proposal_status = 200
            changes["setting"] = "invalid"
            assert (await request(confirmed))[1]["error"]["code"] == "config_invalid"
            changes["setting"] = 7
            layouts[0]["account"] = "different_binding"
            assert (await request(confirmed))[1]["error"]["code"] == "config_binding_mismatch"
            layouts[0]["account"] = "example_bot"
            layouts.append(layouts[0])
            assert (await request(confirmed))[1]["error"]["code"] == "config_binding_mismatch"
            layouts.pop()
            assert path.read_bytes() == original
            assert not (path.parent / "config-history").exists()
            status, body = await request(confirmed)
            assert status == 200, body
            assert body["result"] == {"saved": True, "restart_required": True}
            saved = yaml.safe_load(path.read_bytes())
            assert saved["channels"][0]["config"] == changes
            assert saved["plugins"][pid]["config"] == {}
            (history,) = (path.parent / "config-history").iterdir()
            assert history.read_bytes() == original
    finally:
        await server.close()


async def test_plugin_client_refuses_supplied_confirmation_and_noninteractive_retry(monkeypatch):
    from clytie.api.cli import CLI, PluginCommandClient

    calls = []

    async def request(method, path, **kwargs):
        calls.append(kwargs["json"])
        return ApiResponse(
            status=428,
            json={
                "ok": False,
                "error": {"code": "confirmation_required"},
                "result": {"description": "保存配置"},
            },
        )

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    cli = CLI(SimpleNamespace(request=request), yes=True)
    cli.prefix = True
    client = PluginCommandClient(cli)
    for supplied in (True, False, None):
        reply = await client.request("POST", "/api/v1/test", json={"confirm": supplied})
        assert reply.json["error"]["code"] == "confirm_not_allowed"
    assert not calls
    reply = await client.request("POST", "/api/v1/test", json={})
    assert reply.json["error"]["code"] == "confirmation_declined"
    assert calls == [{}]


def test_null_plugins_rejects_bound_config_proposal():
    from clytie.api.auth import APIError
    from clytie.api.config_write import proposal_source

    source = "plugins:\nchannels:\n  - plugin: fake-channel\n    account: example_bot\n"
    with pytest.raises(APIError) as caught:
        proposal_source(source, "fake-channel", "example_bot", {"setting": 7})
    assert caught.value.code == "config_not_writable"
