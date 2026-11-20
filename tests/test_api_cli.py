from types import SimpleNamespace

import pytest

from clytie.api.cli import CLI, execute, plugin_command
from clytie.cli import main
from clytie.sdk.types import ApiResponse


class FakeClient:
    def __init__(self, home):
        self.home = home
        self.requests = []

    async def request(self, method, path, *, json=None, query=None):
        self.requests.append((method, path, json, query))
        result = (
            {"silence": {"on": False}, "companions": {"companion_a": {}}}
            if path.endswith("/status")
            else {}
        )
        return ApiResponse(status=200, json={"ok": True, "result": result})


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/companions/companion_a/memory/export",
        "/api/v1/backup/create",
        "/api/v1/backup/restore/apply",
    ],
)
async def test_memory_response_loss_retains_operation_id_and_never_replays(
    tmp_path, monkeypatch, path
):
    from aiohttp import ServerDisconnectedError

    from clytie.api import client as module

    monkeypatch.setattr(module, "status", lambda _: {"running": True})
    monkeypatch.setattr(module, "read", lambda _: {"port": 8765})
    (tmp_path / "run").mkdir()
    (tmp_path / "run/api-token").write_text("harmless", encoding="utf-8")
    attempts = []

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            attempts.append("connect")
            raise ServerDisconnectedError()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(module, "ClientSession", Session)
    response = await module.Client(tmp_path).request(
        "POST",
        path,
        json={"operation_id": "a" * 32, "passphrase": "harmless"},
    )
    assert response.json["error"]["code"] == "management_result_unconfirmed"
    assert response.json["result"] == {"operation_id": "a" * 32}
    assert "harmless" not in str(response.json)
    assert attempts == ["connect"]


@pytest.mark.parametrize(
    "command,action,expected",
    [
        ("companion", "start", "/companions/companion_a/start"),
        ("companion", "stop", "/companions/companion_a/stop"),
        ("companion", "restart", "/companions/companion_a/restart"),
        ("boundary", "list", "/companions/companion_a/boundary"),
        ("boundary", "release", "/companions/companion_a/boundary/release"),
        ("plugin", "list", "/companions/companion_a/plugins"),
        ("supervisor", "stop", "/supervisor/stop"),
        ("supervisor", "silence", "/silence"),
        ("admin", "logout-all", "/admin/logout-all"),
    ],
)
async def test_management_cli_uses_client(tmp_path, command, action, expected):
    args = SimpleNamespace(
        command=command,
        action=action,
        cid="companion_a",
        actor_id="member_a",
        home=str(tmp_path),
        yes=True,
        value="on",
        wait_tasks=False,
    )
    client = FakeClient(tmp_path)
    assert (await execute(args, client)).status == 200
    assert client.requests[-1][1] == "/api/v1" + expected


async def test_yes_skips_confirmation_not_password(tmp_path, capsys):
    class Client(FakeClient):
        async def request(self, method, path, *, json=None, query=None):
            if path.endswith("/status"):
                return await super().request(method, path, json=json, query=query)
            self.requests.append((method, path, json, query))
            if not json.get("confirm"):
                return ApiResponse(
                    status=428,
                    json={
                        "ok": False,
                        "result": {"description": "将卸载 echo-tool；建议设置管理口令"},
                    },
                )
            if not json.get("admin_password"):
                return ApiResponse(
                    status=401, json={"ok": False, "error": {"code": "invalid_password"}}
                )
            return ApiResponse(status=200, json={"ok": True, "result": {}})

    client = Client(tmp_path)
    asked = []

    def password(prompt):
        asked.append(prompt)
        return "harmless"

    def input_forbidden(prompt):
        raise AssertionError("yes should skip prompt")

    cli = CLI(client, yes=True, input_fn=input_forbidden, password_fn=password)
    assert (await cli.request("POST", "/example")).status == 200
    assert asked and client.requests[-1][2]["admin_password"] == "harmless"
    assert "echo-tool" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["memory", "thaw", "companion_a", "record_a"],
        ["companion", "start", "companion_a"],
        ["boundary", "list", "companion_a"],
        ["plugin", "list", "companion_a"],
        ["admin", "set-password"],
        ["supervisor", "stop"],
    ],
)
def test_offline_commands_exit_three_before_memory_or_credentials(tmp_path, monkeypatch, argv):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not open memory or prompt")

    monkeypatch.setattr("clytie.api.memory.MemoryAPI.__init__", forbidden)
    monkeypatch.setattr("getpass.getpass", forbidden)
    assert main([*argv, "--home", str(tmp_path)]) == 3


async def test_plugin_cli_disabled_and_uninstalled_never_load(tmp_path, monkeypatch):
    from clytie.supervisor.common import write

    report = SimpleNamespace(
        plugin_configs={"companion_a": {"config": {"plugins": {"api-example": {"enabled": False}}}}}
    )
    monkeypatch.setattr("clytie.config.load_home", lambda *_: report)

    def forbidden(*_, **__):
        raise AssertionError("disabled module loaded")

    monkeypatch.setattr("clytie.plugin_host.manifests.discover", forbidden)
    args = SimpleNamespace(action="api-example", cid="inspect", pid="{}")
    with pytest.raises(ValueError, match="插件未启用"):
        await plugin_command(args, CLI(FakeClient(tmp_path)))
    report.plugin_configs["companion_a"]["config"]["plugins"]["api-example"]["enabled"] = True
    write(tmp_path / "plugins/.host/uninstalled.json", ["api-example"])
    with pytest.raises(ValueError, match="插件未启用"):
        await plugin_command(args, CLI(FakeClient(tmp_path)))
