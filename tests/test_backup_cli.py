import argparse

import pytest

from clytie.api.cli import CLI, add_parser, execute
from clytie.sdk.types import ApiResponse

from .test_api_cli import FakeClient


@pytest.mark.parametrize(
    "argv,method,path,expected",
    [
        (["list"], "GET", "/backup/list", {}),
        (
            ["create", "--scope", "memory", "--companion", "companion_a"],
            "POST",
            "/backup/create",
            {"scope": "memory", "companion_id": "companion_a"},
        ),
        (["verify", "backup_a"], "POST", "/backup/verify", {"backup_id": "backup_a"}),
        (["operation", "operation_a"], "GET", "/backup/operations/operation_a", {}),
        (["cancel", "operation_a"], "POST", "/backup/operations/operation_a/cancel", {}),
        (["restore", "apply", "plan_a"], "POST", "/backup/restore/apply", {"plan_id": "plan_a"}),
        (
            ["memory-plan", "backup_a", "--companion", "companion_a", "--confirm-identity"],
            "POST",
            "/backup/memory/plan",
            {"backup_id": "backup_a", "confirm_identity": True},
        ),
    ],
)
async def test_backup_cli_uses_api_only(tmp_path, argv, method, path, expected):
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["backup", *argv])
    client = FakeClient(tmp_path)
    await execute(args, client)
    actual_method, actual_path, body, _ = client.requests[-1]
    assert (actual_method, actual_path) == (method, "/api/v1" + path)
    for key, value in expected.items():
        assert body[key] == value
    assert not list(tmp_path.iterdir())


async def test_source_confirmation_echoes_only_expected_identity_and_stable_request(
    tmp_path, capsys
):
    class Client(FakeClient):
        async def request(self, method, path, *, json=None, query=None):
            self.requests.append((method, path, json, query))
            if path.endswith("/status"):
                return ApiResponse(status=200, json={"ok": True, "result": {}})
            if not json.get("confirm"):
                return ApiResponse(
                    status=428,
                    json={
                        "ok": False,
                        "result": {
                            "source_repository": {
                                "root": "fictional",
                                "repository_id": "repo_a",
                                "backup_id": "backup_a",
                                "source_identity": {"device": 1},
                            }
                        },
                    },
                )
            return ApiResponse(status=200, json={"ok": True, "result": {}})

    client = Client(tmp_path)
    await CLI(client, input_fn=lambda _: "y").request("POST", "/backup/restore/plan", {})
    assert client.requests[-1][2]["source_repository"] == {
        "root": "fictional",
        "repository_id": "repo_a",
        "backup_id": "backup_a",
    }
    assert client.requests[-1][2]["operation_id"] == client.requests[-2][2]["operation_id"]
    assert "源仓库" in capsys.readouterr().out
