"""源组装文件 I/O 受控阻塞；只用虚构目录，不作性能测量。"""

import asyncio
import threading

import pytest

from clytie.backup import source

from .test_backup_source import fictional


def block_stage(monkeypatch, stage, home, block):
    if stage == "prepare":
        original = source._configuration

        def configuration(*args):
            block()
            return original(*args)

        monkeypatch.setattr(source, "_configuration", configuration)
    elif stage == "publish":
        original = source._put

        def put(root, name, value):
            if name == "clytie.local.yaml":
                block()
            return original(root, name, value)

        monkeypatch.setattr(source, "_put", put)
    else:
        original = source._small
        seen = []

        def small(path, *args):
            if path == home / "clytie.local.yaml":
                seen.append(True)
                if len(seen) == 2:
                    block()
            return original(path, *args)

        monkeypatch.setattr(source, "_small", small)


@pytest.mark.parametrize("stage", ["prepare", "publish", "finalcheck"])
async def test_file_block_does_not_block_capture_callers_event_loop(tmp_path, monkeypatch, stage):
    home, clock = fictional(tmp_path)
    loop = asyncio.get_running_loop()
    entered, release, loop_progress = threading.Event(), threading.Event(), threading.Event()
    observed = []

    def block():
        entered.set()
        assert release.wait(5)

    def observer():
        assert entered.wait(5)
        loop.call_soon_threadsafe(loop_progress.set)
        observed.append(loop_progress.wait(0.5))
        release.set()

    block_stage(monkeypatch, stage, home, block)
    thread = threading.Thread(target=observer)
    thread.start()
    try:
        captured = await source.capture_instance(
            home, clock, tmp_path / "stage", source_instance_id="fictional-instance", epoch="test"
        )
    finally:
        release.set()
        thread.join(5)
    assert observed == [True]
    assert captured["manifest"]["completeness"] == "complete"


@pytest.mark.parametrize("stage", ["prepare", "publish", "finalcheck"])
async def test_cancellation_waits_for_real_file_worker_without_late_success(
    tmp_path,
    monkeypatch,
    stage,
):
    home, clock = fictional(tmp_path)
    loop = asyncio.get_running_loop()
    entered, exited = asyncio.Event(), threading.Event()
    release = threading.Event()
    returned = []

    def block():
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(5)
        finally:
            exited.set()

    block_stage(monkeypatch, stage, home, block)

    async def capture():
        returned.append(
            await source.capture_instance(
                home,
                clock,
                tmp_path / "stage",
                source_instance_id="fictional-instance",
                epoch="test",
            )
        )

    task = asyncio.create_task(capture())
    try:
        await entered.wait()
        task.cancel()
        await clock.drain()
        task.cancel()
        await clock.drain()
        assert not task.done()
        assert not exited.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert exited.is_set()
    assert not returned


async def test_metadata_locks_and_publication_stay_in_their_worker_threads(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from pathlib import Path

    from clytie.backup import assets
    from clytie.supervisor.common import Audit

    home, clock = fictional(tmp_path)
    audit = Audit(home, clock, rotate_bytes=30)
    audit.append("fictional_before_backup")
    loop_thread = threading.get_ident()
    seen = []
    original_lock = source.config_commit_lock

    @contextmanager
    def config_lock(*args):
        creator = threading.get_ident()
        assert creator != loop_thread
        with original_lock(*args):
            yield
            assert threading.get_ident() == creator
        seen.append("lock")

    monkeypatch.setattr(source, "config_commit_lock", config_lock)
    for owner, name in (
        (Path, "mkdir"),
        (source, "discover"),
        (source, "_put"),
        (source, "source_registration"),
        (source, "version"),
        (assets, "consolidate"),
    ):
        original = getattr(owner, name)

        def observed(*args, _original=original, _name=name, **kwargs):
            assert threading.get_ident() != loop_thread
            seen.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, observed)
    captured = await source.capture_instance(
        home,
        clock,
        tmp_path / "stage",
        source_instance_id="fictional-instance",
        epoch="test",
        audit_writers=(audit.writer,),
    )
    assert {
        "lock",
        "mkdir",
        "discover",
        "_put",
        "source_registration",
        "version",
        "consolidate",
    } <= set(seen)
    assert captured["manifest"]["completeness"] == "complete"
    paths = [entry["path"] for entry in captured["files"]]
    assert "supervisor/audit.jsonl.rotation.json" in paths
