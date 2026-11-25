"""真实子进程只加载 fake-channel/fake-model；无真实账号、通知或模型。"""

import asyncio
import shutil
from pathlib import Path

import pytest

from clytie.clock import SystemClock
from clytie.plugin_host.support import timed
from clytie.supervisor.processes import ProcessLauncher

ROOT = Path(__file__).resolve().parents[1]


def retain_child_stacks(monkeypatch):
    """Only wrap this test's real companion CLI; no fake runtime or relaxed deadline."""
    import subprocess

    popen = subprocess.Popen

    def launch(args, *positional, **kwargs):
        if args[1:4] == ["-m", "clytie", "companion"]:
            args = [args[0], str(ROOT / "tests/fixtures/companion_diagnostic.py"), *args[3:]]
        return popen(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)


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
        for _ in range(20):
            msg = await timed(child.receive(), clock, 25)
            messages.append(msg)
            if msg is None or (msg["type"] == "heartbeat" and msg.get("phase") == "running"):
                break
        assert any(m and m["type"] == "ready" for m in messages), (messages, child.log.name)
        assert any(m and m["type"] == "heartbeat" for m in messages)
        import os

        from clytie.plugin_host.locking import _owner

        descriptor = os.open(home / "companions" / cid / "ledger/.lock", os.O_RDONLY)
        try:
            assert _owner(descriptor)["pid"] == child.process.pid
        finally:
            os.close(descriptor)
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
    import subprocess

    from clytie.supervisor.common import read
    from clytie.supervisor.entry import status
    from clytie.supervisor.processes import creation, python_process

    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    clock = SystemClock()
    with (tmp_path / "frontend-output.txt").open("wb") as output:
        executable, env = python_process()
        front = subprocess.Popen(
            [executable, "-m", "clytie", "supervisor", "start", "--home", str(home)],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            env={**env, "PYTHONIOENCODING": "ascii"},
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


async def test_real_supervisor_crash_restart_then_stop(tmp_path, monkeypatch):
    from clytie.supervisor.service import Supervisor

    retain_child_stacks(monkeypatch)
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


async def test_real_child_eof_exits_and_lock_released(tmp_path, monkeypatch):
    from clytie.plugin_host.locking import file_lock, process_identity

    retain_child_stacks(monkeypatch)
    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    launcher = ProcessLauncher(home)
    child = await launcher.launch("companion_a")
    clock = SystemClock()
    try:
        await receive_ready(child, clock)
        child.process.stdin.close()
        assert await timed(child.wait(), clock, 6) == 0
        with file_lock(home / "companions/companion_a/ledger/.lock", process_identity()):
            pass
    except (AssertionError, TimeoutError):
        log = home / "companions/companion_a/logs/process.log"
        if log.exists():
            print(log.read_text(encoding="utf-8", errors="replace"))
        raise
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
        await receive_ready(child, clock)
        import os

        from clytie.plugin_host.locking import _owner, file_lock, process_identity

        lock = home / "companions/companion_a/ledger/.lock"
        descriptor = os.open(lock, os.O_RDONLY)
        try:
            assert _owner(descriptor)["pid"] == child.process.pid
        finally:
            os.close(descriptor)
        launcher.job.close()
        await timed(child.wait(), clock, 6)
        assert child.process.poll() is not None
        with file_lock(lock, process_identity()):
            pass
    finally:
        await child.kill()
        launcher.close()


async def receive_ready(child, clock):
    while True:
        msg = await timed(child.receive(), clock, 25)
        assert msg is not None
        if msg["type"] == "ready":
            return


@pytest.mark.parametrize(
    "eof_stage", ["memory_blocked", "before_memory", "memory_before_eof_observed"]
)
async def test_real_sync_migration_heartbeats_and_eof_waits_without_plugin_start(
    tmp_path, eof_stage
):
    import subprocess

    from clytie.supervisor.processes import Child, creation, frame, python_process

    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    release = tmp_path / "release-migration"
    entered = tmp_path / "memory-entered"
    completed = tmp_path / "memory-completed"
    allow_memory = tmp_path / "allow-memory"
    eof_read = tmp_path / "eof-read"
    eof_observed = tmp_path / "eof-observed"
    allow_eof = tmp_path / "allow-eof"
    code = """
import asyncio, sys, threading
from pathlib import Path
import clytie.supervisor.entry as entry
import clytie.supervisor.heartbeat as heartbeat
OriginalLines = entry.Lines
class ObservedLines(OriginalLines):
    def __init__(self, stream, on_message=None, *, upload_chunks=False):
        def observed(message):
            if message is None:
                Path(sys.argv[6]).write_text("eof-read", encoding="utf-8")
                if sys.argv[9] == "memory_before_eof_observed":
                    # Force the losing side of the parent's close/open race.
                    while not Path(sys.argv[8]).exists():
                        threading.Event().wait(0.01)
            if on_message:
                on_message(message)
            if message is None:
                # Acknowledges the real callback, not merely parent pipe close.
                Path(sys.argv[7]).write_text("eof-observed", encoding="utf-8")
        super().__init__(stream, observed, upload_chunks=upload_chunks)
entry.Lines = ObservedLines
class ObservedHeartbeat(heartbeat.Heartbeat):
    def __init__(self, clock, emit):
        def observed(kind, **fields):
            emit(kind, **fields, test_memory_entered=Path(sys.argv[3]).exists())
        super().__init__(clock, observed)
heartbeat.Heartbeat = ObservedHeartbeat
Original = entry.Companion
class Blocking(Original):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        original = self.host.memory_store
        def blocked(cid):
            Path(sys.argv[3]).write_text("memory-entered", encoding="utf-8")
            while not Path(sys.argv[2]).exists():
                threading.Event().wait(0.01)
            result = original(cid)
            Path(sys.argv[4]).write_text("memory-completed", encoding="utf-8")
            return result
        self.host.memory_store = blocked
        original_open = self.runtime.open
        async def open_ledger():
            ledger = await original_open()
            # Two real phase notifications are deliberately emitted before memory open.
            # They must not be mistaken for entry into the synchronous blocking section.
            self.on_phase("migrating")
            self.on_phase("migrating")
            while not Path(sys.argv[5]).exists():
                await self.clock.sleep(0.01)
            return ledger
        self.runtime.open = open_ledger
entry.Companion = Blocking
raise SystemExit(asyncio.run(entry.companion_main(Path(sys.argv[1]), "companion_a")))
"""
    executable, env = python_process()
    log = (tmp_path / "child-log.txt").open("wb")
    process = subprocess.Popen(
        [
            executable,
            "-c",
            code,
            str(home),
            str(release),
            str(entered),
            str(completed),
            str(allow_memory),
            str(eof_read),
            str(eof_observed),
            str(allow_eof),
            eof_stage,
        ],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=log,
        **creation(),
    )
    child = Child(process, log)
    clock = SystemClock()

    async def wait_signal(path):
        async def observed():
            while not path.exists():
                assert process.poll() is None, (path.name, process.returncode)
                await clock.sleep(0.01)

        await timed(observed(), clock, 12)

    try:
        frame(process.stdin, dict(type="boot", seq=0))
        migrating = 0
        while migrating < 2:
            msg = await timed(child.receive(), clock, 12)
            assert msg and msg["type"] != "ready"
            migrating += msg.get("phase") == "migrating"
        assert not entered.exists()
        if eof_stage == "memory_blocked":
            allow_memory.write_text("continue", encoding="utf-8")

            await wait_signal(entered)
            # Heartbeats continue while synchronous memory startup is blocked.
            while True:
                msg = await timed(child.receive(), clock, 12)
                assert msg["type"] == "heartbeat" and msg["phase"] == "migrating"
                if msg["test_memory_entered"]:
                    break
        process.stdin.close()
        if eof_stage == "memory_before_eof_observed":
            await wait_signal(eof_read)
            assert not eof_observed.exists()
            allow_memory.write_text("continue", encoding="utf-8")
            await wait_signal(entered)
            assert not eof_observed.exists()
            allow_eof.write_text("continue", encoding="utf-8")
        await wait_signal(eof_observed)
        if eof_stage != "before_memory":
            await clock.sleep(6)
            assert process.poll() is None
            assert not completed.exists()
            release.write_text("continue", encoding="utf-8")
        else:
            # The original test only closed stdin before allowing memory open;
            # that does not order the child's EOF observation before startup.
            assert not entered.exists()
            allow_memory.write_text("continue", encoding="utf-8")
        assert await timed(child.wait(), clock, 12) == 0
        assert entered.exists() == completed.exists() == (eof_stage != "before_memory")
        assert not (home / "companions/companion_a/plugin-versions.json").exists()
        while True:
            msg = await timed(child.receive(), clock, 5)
            if msg is None:
                break
            assert msg["type"] != "ready"
    finally:
        release.write_text("continue", encoding="utf-8")
        allow_memory.write_text("continue", encoding="utf-8")
        allow_eof.write_text("continue", encoding="utf-8")
        await child.kill()
        child.close()
