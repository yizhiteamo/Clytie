"""Real loopback requests, virtual clock, no platform or model connections."""

import secrets
import socket

import pytest
from aiohttp import ClientSession

from clytie.api.server import Server
from clytie.sdk.testing import MockClock
from clytie.supervisor.common import Audit


class Backend:
    async def policy(self, route, params, data):
        return route == "/silence", False, "将改变所有伴侣的静默状态"

    async def call(self, route, params, data, query, method):
        return {"route": route}


async def test_loopback_auth_host_origin_csrf_audit(tmp_path):
    clock = MockClock()
    server = Server(tmp_path, clock, Backend(), Audit(tmp_path, clock), {"port": 0})
    await server.start()
    url = f"http://127.0.0.1:{server.port}/api/v1"
    test_value = secrets.token_urlsafe(24)
    server.auth.set_password(test_value)
    credentials = {}
    credentials["password"] = test_value
    try:
        async with ClientSession() as client:
            for headers, expected in [
                ({}, 401),
                ({"Host": "invalid.local"}, 403),
                ({"Origin": "http://invalid.local"}, 403),
            ]:
                async with client.post(
                    url + "/silence", json={"on": True, **credentials}, headers=headers
                ) as response:
                    assert response.status == expected
            async with client.post(url + "/auth/login", json=credentials) as response:
                assert response.status == 200
                session_cookie = response.cookies["clytie_session"]
                assert (
                    session_cookie["httponly"]
                    and session_cookie["samesite"] == "Strict"
                    and session_cookie["path"] == "/api/"
                )
                body = await response.json()
                headers = {}
                headers["Cookie"] = "clytie_session=" + session_cookie.value
            async with client.post(
                url + "/silence", json={"on": True}, headers=headers
            ) as response:
                assert response.status == 403
            headers["X-Clytie-CSRF"] = body["result"]["csrf"]
            async with client.post(
                url + "/silence", json={"on": True}, headers=headers
            ) as response:
                assert response.status == 428
                assert "所有伴侣" in (await response.json())["result"]["description"]
            async with client.post(
                url + "/silence", json={"on": True, "confirm": True}, headers=headers
            ) as response:
                assert response.status == 200
        audit = (tmp_path / "supervisor/audit.jsonl").read_text(encoding="utf-8")
        assert (
            test_value not in audit
            and session_cookie.value not in audit
            and server.auth.token not in audit
        )
        assert len(audit.splitlines()) == 7
    finally:
        await server.close()


async def test_default_port_rollover_and_explicit_no_rollover(tmp_path):
    clock = MockClock()
    blockers = []
    try:
        for port in (8765, 8766):
            sock = socket.socket()
            try:
                sock.bind(("127.0.0.1", port))
                sock.listen()
            except OSError:
                sock.close()
                continue
            blockers.append(sock)
        server = Server(tmp_path, clock, Backend(), Audit(tmp_path, clock))
        await server.start()
        try:
            assert server.port >= 8767
        finally:
            await server.close()
        from clytie.api.auth import APIError

        with pytest.raises(APIError, match="api_bind_failed"):
            await Server(tmp_path, clock, Backend(), Audit(tmp_path, clock), {"port": 8765}).start()
    finally:
        for sock in blockers:
            sock.close()


async def test_all_confirmation_routes_and_password_policies(tmp_path):
    from uuid import uuid4

    from clytie.supervisor.common import write

    from .test_api_management import management

    api = management(tmp_path)
    key = uuid4().hex
    write(
        tmp_path / "run/memory-plans" / (key + ".json"),
        {"id": key, "companion_id": "companion_a", "kind": "undo", "records": [{}]},
    )
    api.plans[key] = {
        "companion_id": "companion_a",
        "operation": "uninstall",
        "target": "echo-tool",
        "steps": ["echo-tool"],
        "stopped_companions": ["companion_b"],
    }

    async def call(*_):
        return {"done": True}

    api.call = call
    server = Server(tmp_path, api.clock, api, Audit(tmp_path, api.clock), {"port": 0})
    await server.start()
    routes = [
        ("/supervisor/stop", {}, "所有伴侣", False),
        ("/silence", {"on": True}, "开启", False),
        ("/silence", {"on": False}, "解除", False),
        ("/admin/logout-all", {}, "所有网页会话", False),
        ("/companions/companion_a/stop", {}, "companion_a", False),
        ("/companions/companion_a/restart", {}, "companion_a", False),
        ("/companions/companion_a/boundary/release", {"actor_id": "member_a"}, "member_a", False),
        ("/companions/companion_a/memory/recovery-code", {}, "companion_a", True),
        ("/memory/import/apply", {"plan_id": key, "kind": "import", "mode": "new"}, key, True),
        ("/companions/companion_a/plugins/apply", {"plan_id": key}, "echo-tool", True),
    ]
    try:
        async with ClientSession() as client:
            base = f"http://127.0.0.1:{server.port}/api/v1"
            bearer = {}
            bearer["Authorization"] = "Bearer " + server.auth.token
            for path, data, target, protected in routes:
                async with client.post(base + path, json=data, headers=bearer) as response:
                    value = await response.json()
                    assert response.status == 428, value
                    assert target in value["result"]["description"]
                    if protected:
                        assert "建议设置管理口令" in value["result"]["description"]
            phrase = secrets.token_urlsafe(24)
            server.auth.set_password(phrase)
            credentials = {}
            credentials["password"] = phrase
            async with client.post(base + "/auth/login", json=credentials) as response:
                value = await response.json()
                web = {
                    "X-Clytie-CSRF": value["result"]["csrf"],
                }
                web["Cookie"] = "clytie_session=" + response.cookies["clytie_session"].value
            api.clock.advance(16 * 60)
            for path, data, _, protected in routes:
                if not protected:
                    continue
                for headers, expected in ((bearer, "invalid_password"), (web, "reauth_required")):
                    async with client.post(
                        base + path, json={**data, "confirm": True}, headers=headers
                    ) as response:
                        assert response.status == 401
                        assert (await response.json())["error"]["code"] == expected
                async with client.post(
                    base + path,
                    json={**data, "confirm": True, "admin_password": phrase},
                    headers=bearer,
                ) as response:
                    assert response.status == 200, await response.json()
            async with client.post(
                base + "/admin/password",
                json={"old_password": "harmless", "new_password": phrase},
                headers=bearer,
            ) as response:
                assert response.status == 401
            audit = (tmp_path / "supervisor/audit.jsonl").read_text(encoding="utf-8")
            assert phrase not in audit and server.auth.token not in audit
    finally:
        await server.close()
