"""In-process fake HTTP boundary keeps existing memory assertions on the real API."""

from clytie.api.operations import load_plan
from clytie.api.server import match
from clytie.sdk.types import ApiResponse


def fake_memory_client(monkeypatch, api):
    monkeypatch.setattr("clytie.supervisor.entry.status", lambda home: {"running": True})
    monkeypatch.setattr("builtins.input", lambda _: "y")
    calls = []

    async def request(client, method, path, *, json=None, query=None):
        calls.append((method, path))
        route, params = match(method, path)
        data = dict(json or {})
        # Management metadata is consumed by the API coordinator, not MemoryAPI.
        data.pop("operation_id", None)
        if route == "/status":
            result = {"companions": {"companion_a": {}}, "silence": {"on": False}}
        elif route == "/memory/import/plans/<plan_id>":
            result = load_plan(api.home, params["plan_id"])
        elif route == "/memory/import/apply":
            if not data.pop("confirm", False):
                return ApiResponse(
                    status=428,
                    json={
                        "ok": False,
                        "error": {"code": "confirmation_required"},
                        "result": {"description": "确认执行记忆计划"},
                    },
                )
            result = {"import_id": await api.apply(**data)}
        elif route.endswith("/export"):
            result = api.export(params["cid"], **data)
        elif route.endswith("/import/plan"):
            result = await api.plan(params["cid"], **data)
        else:
            raise AssertionError("unexpected API route")
        return ApiResponse(status=200, json={"ok": True, "result": result})

    monkeypatch.setattr("clytie.api.client.Client.request", request)
    return calls
