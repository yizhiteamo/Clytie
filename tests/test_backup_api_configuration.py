import threading

import pytest
from aiohttp import ClientSession
from tests.test_api_management import management
from tests.test_config import make_home

from clytie.api.server import Server
from clytie.backup.configuration import BackupConfigWriter
from clytie.supervisor.common import Audit


async def setup(tmp_path):
    home, repository, _, _ = make_home(tmp_path)
    api = management(home)
    api._backup_config_writer = BackupConfigWriter(home, api.clock, repo_root=repository)
    server = Server(home, api.clock, api, Audit(home, api.clock), {"port": 0})
    await server.start()
    return server, api, home


async def test_config_http_auth_server_confirmation_and_audit(tmp_path):
    server, api, home = await setup(tmp_path)
    base = f"http://127.0.0.1:{server.port}/api/v1/backup/config/"
    try:
        async with ClientSession(trust_env=False) as client:
            async with client.post(base + "plan", json={"patch": {"enabled": False}}) as r:
                assert r.status == 401
            headers = {}
            headers["Authorization"] = "Bearer " + server.auth.token
            async with client.post(
                base + "plan", json={"patch": {"enabled": False}}, headers=headers
            ) as r:
                assert r.status == 200
                plan = (await r.json())["result"]
            assert plan["requires_confirmation"]
            async with client.post(
                base + "apply",
                json={
                    "plan_id": plan["plan_id"],
                    "requires_confirmation": False,
                    "_backup_config_confirmed": True,
                },
                headers=headers,
            ) as r:
                assert r.status == 428
            async with client.post(
                base + "apply", json={"plan_id": plan["plan_id"], "confirm": True}, headers=headers
            ) as r:
                assert r.status == 200
                assert (await r.json())["result"]["saved"]
        text = (home / "supervisor/audit.jsonl").read_text()
        assert "/backup/config/plan" in text and "/backup/config/apply" in text
        assert server.auth.token not in text
    finally:
        await server.close()


async def test_config_http_safe_change_without_confirm_and_stale_plan(tmp_path):
    server, api, home = await setup(tmp_path)
    base = f"http://127.0.0.1:{server.port}/api/v1/backup/config/"
    headers = {}
    headers["Authorization"] = "Bearer " + server.auth.token
    try:
        async with ClientSession(trust_env=False) as client:
            plans = []
            for value in [8, 7]:
                async with client.post(
                    base + "plan", json={"patch": {"logs": {"archives": value}}}, headers=headers
                ) as r:
                    assert r.status == 200
                    plans.append((await r.json())["result"])
            for plan, status in zip(plans, [200, 409]):
                async with client.post(
                    base + "apply", json={"plan_id": plan["plan_id"]}, headers=headers
                ) as r:
                    assert r.status == status
    finally:
        await server.close()


async def test_config_backend_rechecks_confirmation_and_plan_runs_off_loop(tmp_path):
    from clytie.api.auth import APIError

    server, api, home = await setup(tmp_path)
    try:
        plan = await api.call("/backup/config/plan", {}, {"patch": {"enabled": False}}, {}, "POST")
        with pytest.raises(APIError) as error:
            await api.call("/backup/config/apply", {}, {"plan_id": plan["plan_id"]}, {}, "POST")
        assert error.value.status == 428
        event_thread = threading.get_ident()
        original = api._backup_config_writer.plan
        thread_ids = []

        def observed(patch):
            thread_ids.append(threading.get_ident())
            return original(patch)

        api._backup_config_writer.plan = observed
        await api.call("/backup/config/plan", {}, {"patch": {"logs": {"archives": 8}}}, {}, "POST")
        assert thread_ids and thread_ids[0] != event_thread
    finally:
        await server.close()
