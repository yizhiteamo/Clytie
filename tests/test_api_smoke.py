"""Windows/Linux real processes use only the repository's fictional fake plugins."""

import shutil
import socket
from pathlib import Path

import yaml

from clytie.api.client import Client, detached_start
from clytie.clock import SystemClock
from clytie.supervisor.entry import status

ROOT = Path(__file__).resolve().parents[1]


async def test_real_detach_api_stop_and_duplicate(tmp_path):
    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    path = home / "clytie.local.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["api"] = {"port": port}
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    clock = SystemClock()
    client = Client(home)
    try:
        started = await detached_start(home, clock)
        assert started["ok"], started
        assert not (await detached_start(home, clock))["ok"]
        for _ in range(250):
            result = await client.request("GET", "/api/v1/status")
            if all(
                v["state"] in {"running", "degraded"}
                for v in result.json["result"]["companions"].values()
            ):
                break
            await clock.sleep(0.1)
        assert result.json["result"]["api_port"] == port
        response = await client.request(
            "POST", "/api/v1/silence", json={"on": True, "confirm": True}
        )
        assert response.status == 200, response.json
        response = await client.request("GET", "/api/v1/companions/companion_a/plugins")
        assert response.status == 200, response.json
        assert any(p["id"] == "fake-model" for p in response.json["result"])
    finally:
        if status(home)["running"]:
            result = await client.request("POST", "/api/v1/supervisor/stop", json={"confirm": True})
            assert result.status == 200, result.json
            for _ in range(300):
                if not status(home)["running"]:
                    break
                await clock.sleep(0.1)
        assert not status(home)["running"]


async def test_real_detach_explicit_port_failure_leaves_no_daemon(tmp_path):
    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        path = home / "clytie.local.yaml"
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        config["api"] = {"port": blocker.getsockname()[1]}
        path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        assert not (await detached_start(home, SystemClock()))["ok"]
    assert not status(home)["running"]


async def test_real_two_companion_enable_uninstall_race(tmp_path):
    import asyncio

    from clytie.supervisor.common import read

    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    shutil.copytree(home / "companions/companion_a", home / "companions/companion_b")
    b = home / "companions/companion_b/companion.yaml"
    b.write_text(
        b.read_text(encoding="utf-8")
        .replace("companion_a", "companion_b")
        .replace("example_bot", "second_bot"),
        encoding="utf-8",
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    file = home / "clytie.local.yaml"
    config = yaml.safe_load(file.read_text(encoding="utf-8"))
    config["companions"].append("companion_b")
    config["accounts"]["second_bot"] = {"channel": "fake-channel", "account": "1000000004"}
    config["api"] = {"port": port}
    file.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    client = Client(home)
    clock = SystemClock()

    async def post(cid, op, data):
        return await client.request("POST", f"/api/v1/companions/{cid}/plugins/{op}", json=data)

    try:
        assert (await detached_start(home, clock))["ok"]
        for _ in range(200):
            reply = await client.request("GET", "/api/v1/status")
            if all(x["state"] == "running" for x in reply.json["result"]["companions"].values()):
                break
            await clock.sleep(0.1)
        assert all(x["state"] == "running" for x in reply.json["result"]["companions"].values())
        # Obtain both plans while both processes have the optional plugin disabled.
        plans = await asyncio.gather(
            post("companion_a", "plan", {"op": "uninstall", "target": "echo-tool"}),
            post("companion_b", "plan", {"op": "enable", "target": "echo-tool"}),
        )
        assert all(p.status == 200 for p in plans), [p.json for p in plans]
        results = await asyncio.gather(
            *[
                post(cid, "apply", {"plan_id": plan.json["result"]["plan_id"], "confirm": True})
                for cid, plan in zip(("companion_a", "companion_b"), plans)
            ]
        )
        assert sum(r.status == 200 for r in results) == 1, [r.json for r in results]
        excluded = read(home / "plugins/.host/uninstalled.json", [])
        plugins = await client.request("GET", "/api/v1/companions/companion_b/plugins")
        active = any(p["id"] == "echo-tool" and p["enabled"] for p in plugins.json["result"])
        assert not (active and "echo-tool" in excluded)
    finally:
        if status(home)["running"]:
            await client.request("POST", "/api/v1/supervisor/stop", json={"confirm": True})
            for _ in range(300):
                if not status(home)["running"]:
                    break
                await clock.sleep(0.1)
        assert not status(home)["running"]


async def test_startup_handshake_ignores_early_ack_and_stops_on_eof():
    import asyncio

    from clytie.supervisor.entry import StartupHandshake

    from .test_supervisor import spin

    queue = asyncio.Queue()
    gate = StartupHandshake(asyncio.get_running_loop(), queue)
    gate.observe({"type": "startup_confirmed"})
    assert not gate.confirmed
    gate.observe(None)
    await spin()
    assert gate.cancelled.is_set() and await queue.get() is None
    gate = StartupHandshake(asyncio.get_running_loop(), queue)
    gate.ready.set()
    gate.observe({"type": "startup_confirmed"})
    gate.observe(None)
    await spin()
    assert gate.confirmed and not gate.cancelled.is_set() and queue.empty()


async def test_detach_timeout_closes_launch_pipe(tmp_path, monkeypatch):
    import asyncio
    import io
    from types import SimpleNamespace

    from clytie.sdk.testing import MockClock

    from .test_supervisor import spin

    pipe = io.BytesIO()
    process = SimpleNamespace(pid=42, stdin=pipe, poll=lambda: None)
    monkeypatch.setattr("subprocess.Popen", lambda *_, **__: process)
    clock = MockClock()
    task = asyncio.create_task(detached_start(tmp_path, clock))
    await spin()
    clock.advance(30)
    await spin()
    assert (await task)["error"]["code"] == "startup_timeout"
    assert pipe.closed


def test_launch_scripts_only_start_foreground():
    for name in ("start-clytie.cmd", "start-clytie.sh"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "supervisor start" in text and "--detach" not in text
        assert ".venv" in text
        for forbidden in ("schtasks", "systemctl", "crontab", "mklink", "New-Item", "setx"):
            assert forbidden not in text


async def test_real_foreground_survives_all_default_ports_occupied(tmp_path):
    import asyncio
    import subprocess

    from clytie.supervisor.common import read
    from clytie.supervisor.processes import creation, python_process

    from .test_supervisor_smoke import wait_until

    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    blockers = []
    front = None
    clock = SystemClock()
    try:
        for port in range(8765, 8775):
            sock = socket.socket()
            try:
                sock.bind(("127.0.0.1", port))
                sock.listen()
            except OSError:
                sock.close()
            else:
                blockers.append(sock)
        executable, env = python_process()
        env.pop("CLYTIE_HOME", None)
        with (tmp_path / "frontend.txt").open("wb") as output:
            front = subprocess.Popen(
                [executable, "-m", "clytie", "supervisor", "start", "--home", str(home)],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                env=env,
                **creation(),
            )
            await wait_until(
                lambda: any(
                    row["state"] == "running"
                    for row in read(home / "run/health.json", {}).get("companions", {}).values()
                ),
                clock,
            )
            assert front.poll() is None
            assert read(home / "run/health.json")["api_port"] is None
            assert "api_bind_failed" in (tmp_path / "frontend.txt").read_text(encoding="utf-8")
    finally:
        if front is not None and front.poll() is None:
            front.kill()
            await asyncio.to_thread(front.wait)
            await wait_until(lambda: not status(home)["running"], clock, 130)
        for sock in blockers:
            sock.close()
