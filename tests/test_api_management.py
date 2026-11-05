import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from clytie.api.auth import APIError
from clytie.api.management import Management
from clytie.sdk.testing import MockClock
from clytie.supervisor.common import write


def management(tmp_path):
    clock = MockClock()
    svc = SimpleNamespace(
        home=tmp_path,
        clock=clock,
        companions=["companion_a", "companion_b"],
        rows={c: {"state": "stopped", "inflight_tasks": 2} for c in ("companion_a", "companion_b")},
        setting=lambda group, key, default: default,
    )
    return Management(svc, None)


@pytest.mark.parametrize(
    "route,params,data,expected",
    [
        ("/supervisor/stop", {}, {}, "所有伴侣"),
        ("/silence", {}, {"on": True}, "开启"),
        ("/silence", {}, {"on": False}, "解除"),
        ("/admin/logout-all", {}, {}, "所有网页会话"),
        ("/companions/<cid>/stop", {"cid": "companion_a"}, {}, "companion_a"),
        ("/companions/<cid>/restart", {"cid": "companion_a"}, {}, "companion_a"),
        (
            "/companions/<cid>/boundary/release",
            {"cid": "companion_a"},
            {"actor_id": "member_a"},
            "member_a",
        ),
        ("/companions/<cid>/memory/recovery-code", {"cid": "companion_a"}, {}, "companion_a"),
    ],
)
async def test_confirmation_policy_names_target(tmp_path, route, params, data, expected):
    confirm, needs_password, text = await management(tmp_path).policy(route, params, data)
    assert confirm and expected in text
    assert needs_password == route.endswith("recovery-code")


@pytest.mark.parametrize(
    "kind,mode,protected",
    [
        ("undo", None, True),
        ("import", "restore", True),
        ("import", "merge", False),
        ("import", "new", False),
    ],
)
async def test_plan_kind_is_server_authority(tmp_path, kind, mode, protected):
    api = management(tmp_path)
    key = uuid4().hex
    plan = {"id": key, "companion_id": "companion_a", "kind": kind, "mode": mode, "records": [{}]}
    write(tmp_path / "run/memory-plans" / (key + ".json"), plan)
    confirm, needs_password, text = await api.policy(
        "/memory/import/apply", {}, {"plan_id": key, "kind": "import", "mode": "new"}
    )
    assert confirm and needs_password is protected and key in text
    api.svc.rows["companion_a"]["state"] = "running"
    if protected:
        with pytest.raises(APIError, match="companion_running"):
            await api.policy("/memory/import/apply", {}, {"plan_id": key})
    else:
        await api.policy("/memory/import/apply", {}, {"plan_id": key})


async def test_running_undo_and_restore_rejected_before_rpc(tmp_path):
    api = management(tmp_path)
    api.svc.rows["companion_a"]["state"] = "running"
    for route, data in [
        ("/companions/<cid>/memory/import/undo", {"import_id": "fake"}),
        ("/companions/<cid>/memory/import/plan", {"mode": "restore"}),
    ]:
        with pytest.raises(APIError, match="companion_running"):
            await api.call(route, {"cid": "companion_a"}, data, {}, "POST")


async def test_uninstall_stopped_optional_and_required(tmp_path):
    api = management(tmp_path)
    config = {
        "model": {"plugin": "fake-model"},
        "channels": [{"plugin": "fake-channel"}],
        "plugins": {"echo-tool": {"enabled": True}},
    }
    api.configuration = lambda: SimpleNamespace(plugin_configs={"companion_b": {"config": config}})
    assert await api.uninstall_context("companion_a", "echo-tool") == ["companion_b"]
    for pid in ("fake-model", "fake-channel"):
        with pytest.raises(APIError, match="required_by_stopped_companion") as error:
            await api.uninstall_context("companion_a", pid)
        assert error.value.result == {"companions": ["companion_b"]}
    assert config["plugins"]["echo-tool"]["enabled"] is True
    api.svc.rows["companion_b"]["state"] = "running"

    async def transfer(*_):
        return [{"id": "echo-tool", "enabled": True}]

    api.transfer = transfer
    with pytest.raises(APIError, match="plugin_in_use"):
        await api.uninstall_context("companion_a", "echo-tool")


async def test_ipc_timeout_does_not_kill_child(tmp_path):
    from .test_supervisor import make, spin

    svc, launcher, clock = await make(tmp_path, config={"hang_seconds": 120})
    try:
        call = asyncio.create_task(svc.request("companion_0", "panels", {}, timeout=30))
        await spin()
        clock.advance(30)
        await spin()
        with pytest.raises(APIError, match="companion_timeout"):
            await call
        assert not launcher.children[0][1].killed
        for phase in ("starting", "migrating", "stopping"):
            svc.rows["companion_0"].update(state="starting", phase=phase)
            api = Management(svc, None)
            with pytest.raises(APIError, match="companion_busy"):
                await api.transfer("companion_0", "panels", {})
    finally:
        await svc.stop()


async def test_single_stop_ignores_heartbeat_timeout_and_does_not_restart(tmp_path):
    from .test_supervisor import make, spin

    svc, launcher, clock = await make(tmp_path, stop=False)
    child = launcher.children[0][1]
    stopping = asyncio.create_task(svc.stop_companion("companion_0"))
    await spin()
    clock.advance(31)
    await spin()
    assert not child.killed and svc.rows["companion_0"]["state"] == "stopping"
    await child.heartbeat(phase="running")
    await spin()
    assert svc.rows["companion_0"]["state"] == "stopping"
    await child.queue.put(
        {"type": "stop_result", "result": {"stopped": ["fake-plugin"], "not_stopped": {}}}
    )
    await child.exit(0)
    assert (await stopping)["stopped"] == ["fake-plugin"]
    clock.advance(300)
    await spin()
    assert len(launcher.children) == 1
    await svc.stop()


async def test_single_wait_stop_escalates_with_normal_timeout(tmp_path):
    from .test_supervisor import make, spin

    svc, launcher, clock = await make(tmp_path, stop=False)
    stopping = asyncio.create_task(svc.stop_companion("companion_0", "wait"))
    await spin()
    clock.advance(300)
    await spin()
    assert not stopping.done()
    await svc.escalate()
    await spin()
    clock.advance(120)
    await spin()
    assert (await stopping)["reason"] == "timeout_killed"
    assert launcher.children[0][1].killed
    await svc.stop()
