"""真实子进程只加载 fake-channel/fake-model；无真实账号、通知或模型。"""

import asyncio
import shutil
from pathlib import Path

from clytie.clock import SystemClock
from clytie.plugin_host.support import timed
from clytie.supervisor.processes import ProcessLauncher

ROOT = Path(__file__).resolve().parents[1]


async def test_real_companion_ready_heartbeat_stop(tmp_path):
    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    import yaml

    config = yaml.safe_load((home / "clytie.local.yaml").read_text(encoding="utf-8"))
    cid = config["companions"][0]
    launcher = ProcessLauncher(home)
    child = await launcher.launch(cid)
    clock = SystemClock()
    messages = []
    try:
        for _ in range(3):
            msg = await timed(child.receive(), clock, 25)
            messages.append(msg)
            if msg is None or msg["type"] == "heartbeat":
                break
        assert any(m and m["type"] == "ready" for m in messages), (messages, child.log.name)
        assert any(m and m["type"] == "heartbeat" for m in messages)
        await child.send(dict(type="stop", seq=2, task_mode="cancel"))
        result = None
        while True:
            msg = await timed(child.receive(), clock, 25)
            if msg is None:
                break
            if msg["type"] == "stop_result":
                result = msg["result"]
        assert result and result["state"] == "stopped"
        assert await child.wait() == 0
    finally:
        await child.kill()
        launcher.close()


async def wait_until(predicate, clock, timeout=25):
    end = clock.monotonic() + timeout
    while not predicate():
        assert clock.monotonic() < end, "process condition timed out"
        await clock.sleep(0.05)


async def test_real_frontend_disappears_daemon_stops(tmp_path):
    import os
    import subprocess
    import sys

    from clytie.supervisor.common import read
    from clytie.supervisor.entry import status
    from clytie.supervisor.processes import creation

    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    clock = SystemClock()
    with (tmp_path / "frontend-output.txt").open("wb") as output:
        front = subprocess.Popen(
            [sys.executable, "-m", "clytie", "supervisor", "start", "--home", str(home)],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            env={**os.environ, "PYTHONIOENCODING": "ascii"},
            **creation(),
        )
        try:
            await wait_until(
                lambda: any(
                    r["state"] == "running"
                    for r in read(home / "run/health.json", {}).get("companions", {}).values()
                ),
                clock,
            )
            output.flush()
            assert "关闭这个窗口会安全停止所有伴侣" in (tmp_path / "frontend-output.txt").read_text(
                encoding="utf-8"
            )
            front.kill()
            await asyncio.to_thread(front.wait)
            await wait_until(lambda: not status(home)["running"], clock)
            audit = (home / "supervisor/audit.jsonl").read_text(encoding="utf-8")
            assert '"operation":"stop"' in audit
        finally:
            if front.poll() is None:
                front.kill()
                await asyncio.to_thread(front.wait)
            # EOF initiates cleanup even on test failure; wait for lock release.
            await wait_until(lambda: not status(home)["running"], clock, 130)


async def test_real_supervisor_crash_restart_then_stop(tmp_path):
    from clytie.supervisor.service import Supervisor

    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    clock = SystemClock()
    launcher = ProcessLauncher(home)
    supervisor = Supervisor(
        home, ["companion_a"], launcher, clock, config={"restart": {"backoff_seconds": [1]}}
    )
    await supervisor.start()
    try:
        await wait_until(lambda: supervisor.rows["companion_a"]["state"] == "running", clock)
        await supervisor.children["companion_a"].kill()
        await wait_until(
            lambda: (
                supervisor.rows["companion_a"]["restart_count"] == 1
                and supervisor.rows["companion_a"]["state"] == "running"
            ),
            clock,
        )
        result = await supervisor.stop()
        assert result["companion_a"]["state"] == "stopped"
    except AssertionError as exc:
        log = home / "companions/companion_a/logs/process.log"
        detail = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        raise AssertionError(str(supervisor.rows) + "\n" + detail) from exc
    finally:
        if supervisor.lock:
            await supervisor.stop()
        launcher.close()


async def test_real_child_eof_exits_and_lock_released(tmp_path):
    from clytie.plugin_host.locking import file_lock, process_identity

    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    launcher = ProcessLauncher(home)
    child = await launcher.launch("companion_a")
    clock = SystemClock()
    try:
        assert (await timed(child.receive(), clock, 25))["type"] == "ready"
        child.process.stdin.close()
        assert await timed(child.wait(), clock, 6) == 0
        with file_lock(home / "companions/companion_a/ledger/.lock", process_identity()):
            pass
    finally:
        await child.kill()
        launcher.close()


async def test_real_supervisor_restart_does_not_replay_ledger(tmp_path):
    from clytie.kernel.ledger import Ledger
    from clytie.supervisor.service import Supervisor

    from .test_kernel_delivery import settle, task_with_intents
    from .test_kernel_service import new_record, setup

    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    directory = home / "companions/companion_a/ledger"
    directory.mkdir(parents=True)
    kernel, _, fake_clock = setup(directory)
    kernel.reactions.enabled = False
    record, _, intents = await task_with_intents(kernel)
    kernel.ledger.begin_attempt(intents[0].intent_id)
    running, _, _ = await new_record(kernel, name="running-request")
    running["exec_status"] = "running"
    with kernel.ledger.transaction():
        kernel.ledger.put("record", running["trace_id"], running)
    await settle(kernel)
    kernel.ledger.close()
    clock = SystemClock()
    launcher = ProcessLauncher(home)
    supervisor = Supervisor(
        home, ["companion_a"], launcher, clock, config={"restart": {"backoff_seconds": [1]}}
    )
    await supervisor.start()
    try:
        await wait_until(lambda: supervisor.rows["companion_a"]["state"] == "running", clock)
        await supervisor.children["companion_a"].kill()
        await wait_until(
            lambda: (
                supervisor.rows["companion_a"]["restart_count"] == 1
                and supervisor.rows["companion_a"]["state"] == "running"
            ),
            clock,
        )
        await supervisor.stop()
        ledger = Ledger(directory / "ledger.sqlite3", "companion_a", fake_clock)
        try:
            assert ledger.get("intent", intents[0].intent_id)["status"] == "unknown"
            assert ledger.get("intent", intents[0].intent_id)["attempts"] == 1
            assert ledger.get("record", running["trace_id"])["exec_status"] == "interrupted"
            assert len(ledger.rows("invocation")) == 1
        finally:
            ledger.close()
    finally:
        if supervisor.lock:
            await supervisor.stop()
        launcher.close()


async def test_real_job_close_terminates_windows_child(tmp_path):
    import sys

    import pytest

    if sys.platform != "win32":
        pytest.skip("Windows Job Object; Linux covered by pipe EOF smoke")
    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    launcher = ProcessLauncher(home)
    child = await launcher.launch("companion_a")
    clock = SystemClock()
    try:
        assert (await timed(child.receive(), clock, 25))["type"] == "ready"
        launcher.job.close()
        await timed(child.wait(), clock, 6)
        assert child.process.poll() is not None
    finally:
        await child.kill()
        launcher.close()
