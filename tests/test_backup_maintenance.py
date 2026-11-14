"""Real loopback/process checks; no business backends are authorized in maintenance."""

import pytest

from clytie.api.client import Client, detached_start
from clytie.clock import SystemClock
from clytie.supervisor.entry import status


async def stop_home(home, client, clock):
    if status(home)["running"]:
        response = await client.request("POST", "/api/v1/supervisor/stop", json={"confirm": True})
        assert response.status == 200
        for _ in range(300):
            if not status(home)["running"]:
                break
            await clock.sleep(0.1)
    assert not status(home)["running"]


@pytest.mark.parametrize("broken", [False, True])
async def test_real_maintenance_empty_or_invalid_home_never_starts_business(tmp_path, broken):
    home = tmp_path / "fictional-maintenance"
    home.mkdir()
    if broken:
        (home / "clytie.local.yaml").write_text("unknown: true\n", encoding="utf-8")
    clock, client = SystemClock(), Client(home)
    try:
        started = await detached_start(home, clock, maintenance=True)
        assert started["ok"], started
        reply = await client.request("GET", "/api/v1/status")
        assert reply.status == 200
        result = reply.json["result"]
        assert result["mode"] == "maintenance"
        assert result["companions"] == {}
        assert not result["configuration"]["valid"]
        denied = await client.request("POST", "/api/v1/companions/companion_a/start", json={})
        assert denied.json["error"]["code"] == "maintenance_mode"
        assert not (home / "companions").exists()
        if not broken:
            assert not (home / "clytie.local.yaml").exists()
    finally:
        if status(home)["running"]:
            await client.request("POST", "/api/v1/supervisor/stop", json={"confirm": True})
            for _ in range(300):
                if not status(home)["running"]:
                    break
                await clock.sleep(0.1)
        assert not status(home)["running"]


async def test_real_restore_maintenance_credentials_source_confirmation_and_start(
    tmp_path, monkeypatch
):
    from .test_backup_restore import configured_package

    manager, package, key, home, workspace = configured_package(tmp_path, monkeypatch)
    plan = manager.plan(package, key, home, workspace)
    state = manager.apply(plan["id"])
    assert state["data_state"] == "ready_for_switch"
    assert not state["configuration"]["valid"]
    client, clock = Client(home), SystemClock()
    try:
        # Data readiness cannot bypass the ordinary global credential error.
        assert not (await detached_start(home, clock))["ok"]
        assert (await detached_start(home, clock, maintenance=True))["ok"]
        response = await client.request("GET", "/api/v1/backup/status")
        value = response.json["result"]
        assert value["data_state"] == "ready_for_switch"
        assert value["credential_exclusions"]
        assert not value["configuration"]["valid"]
        assert value["configuration"]["global_errors"]
        assert (await client.request("GET", "/api/v1/status")).json["result"]["companions"] == {}
        import secrets

        admin_value = secrets.token_urlsafe(24)
        assert (
            await client.request(
                "POST", "/api/v1/admin/password", json={"new_password": admin_value}
            )
        ).status == 200
        route = "/api/v1/backup/restore/source-stopped"
        args = {"restore_id": state["restore_id"]}
        assert (await client.request("POST", route, json=args)).status == 428
        args["confirm"] = True
        assert (await client.request("POST", route, json=args)).status == 401
        # Supply a deliberately fictional file; no authentication probe is made.
        (home / "secrets/fictional.json").write_text("harmless", encoding="utf-8")
        response = await client.request("GET", "/api/v1/backup/status")
        assert response.json["result"]["configuration"]["valid"]
        assert not response.json["result"]["credential_exclusions"]
        assert not response.json["result"]["source_stopped"]
        args["admin_password"] = admin_value
        response = await client.request("POST", route, json=args)
        assert response.status == 200, response.json
        assert response.json["result"]["source_stopped"]
        early = await client.request(
            "POST",
            "/api/v1/backup/restore/acknowledge",
            json={"restore_id": state["restore_id"], "confirm": True},
        )
        assert early.status == 409
        await stop_home(home, client, clock)
        # A subsequent modification cannot borrow the successful validation above.
        file = home / "clytie.local.yaml"
        original = file.read_text(encoding="utf-8")
        file.write_text(original + "unexpected: harmless\n", encoding="utf-8")
        assert not (await detached_start(home, clock))["ok"]
        file.write_text(original, encoding="utf-8")
        assert (await detached_start(home, clock))["ok"]
        for _ in range(250):
            response = await client.request("GET", "/api/v1/status")
            rows = response.json["result"]["companions"]
            if rows and all(row["state"] == "running" for row in rows.values()):
                break
            await clock.sleep(0.1)
        assert all(row["state"] == "running" for row in rows.values()), rows
        response = await client.request(
            "POST",
            "/api/v1/backup/restore/acknowledge",
            json={"restore_id": state["restore_id"], "confirm": True},
        )
        assert response.status == 200, response.json
        assert response.json["result"]["switched"]
    finally:
        await stop_home(home, client, clock)
