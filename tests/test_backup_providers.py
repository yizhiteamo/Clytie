import asyncio
import hashlib
from datetime import timedelta

import pytest

from clytie.backup.providers import NamespaceSink, ProviderCapture
from clytie.sdk import CancelToken
from clytie.sdk.backup import BackupRequest, BackupTicket, BackupView
from clytie.sdk.testing import MockClock


async def chunks(value=b"fictional data"):
    yield value


async def test_sink_limits_namespace_duplicates_credentials_and_late_writes(tmp_path):
    sink = NamespaceSink(tmp_path / "plugin", credentials={"secret.key"})
    entry = await sink.write("data/state", chunks())
    assert entry.size == len(b"fictional data")
    assert entry.sha256 == hashlib.sha256(b"fictional data").hexdigest()
    for path in ("../escape", "/absolute", "a\\b", "a:stream", "secret.key", "DATA/state"):
        with pytest.raises(ValueError):
            await sink.write(path, chunks())
    sink.close()
    with pytest.raises(ValueError, match="sink_closed"):
        await sink.write("late", chunks())
    assert not (tmp_path / "plugin/late").exists()


class Provider:
    def __init__(self):
        self.calls = []

    async def prepare(self, request, cancel):
        self.calls.append("prepare")
        return BackupTicket("fictional")

    async def capture(self, ticket, epoch, cancel):
        self.calls.append("capture")
        return BackupView(epoch, 1, "revision_a", ticket.handle)

    async def materialize(self, view, sink, cancel):
        self.calls.append("materialize")
        await sink.write("state", chunks())

    async def release(self, ticket):
        self.calls.append("release")


def session(tmp_path, provider, clock):
    request = BackupRequest(
        operation_id="test",
        companion_id="companion_a",
        plugin_id="fake",
        data_version=1,
        deadline=clock.now() + timedelta(seconds=10),
    )
    return ProviderCapture(
        provider, request, clock, NamespaceSink(tmp_path / "sink"), cancel=CancelToken()
    )


async def test_provider_lifecycle_and_idempotent_release(tmp_path):
    provider = Provider()
    run = session(tmp_path, provider, MockClock())
    assert await run.prepare()
    assert await run.capture("epoch_a")
    assert await run.materialize()
    assert await run.release()
    assert run.result()["status"] == "complete"
    assert provider.calls == ["prepare", "capture", "materialize", "release"]


async def test_prepare_timeout_keeps_late_ticket_tracked_and_releases(tmp_path):
    clock = MockClock()
    finish = asyncio.Event()

    class Late(Provider):
        async def prepare(self, request, cancel):
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()
            return BackupTicket("late")

    provider = Late()
    run = session(tmp_path, provider, clock)
    task = asyncio.create_task(run.prepare())
    await clock.drain()
    await clock.advance_and_drain(10)
    assert not await task
    assert run.result()["resource_pending"]
    finish.set()
    await clock.drain(64)
    assert provider.calls == ["release"]
    assert not run.result()["resource_pending"]
    assert run.result()["status"] == "partial"


async def test_wrong_view_is_partial_and_released(tmp_path):
    class Wrong(Provider):
        async def capture(self, ticket, epoch, cancel):
            return BackupView("wrong", 1, "revision", ticket.handle)

    provider = Wrong()
    run = session(tmp_path, provider, MockClock())
    assert await run.prepare()
    assert not await run.capture("expected")
    await run.release()
    assert provider.calls == ["prepare", "release"]
    assert run.result()["status"] == "partial"


async def test_sink_rejects_case_colliding_parent_and_symlink(tmp_path):
    sink = NamespaceSink(tmp_path / "sink")
    await sink.write("Folder/a", chunks())
    with pytest.raises(ValueError):
        await sink.write("folder/b", chunks())
    external = tmp_path / "external"
    external.mkdir()
    link = sink.root / "linked"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not allow symlink creation")
    with pytest.raises(ValueError):
        await sink.write("linked/escape", chunks())
    assert not (external / "escape").exists()


async def test_capture_timeout_defers_release_outside_gate(tmp_path):
    clock = MockClock()

    class Slow(Provider):
        async def capture(self, ticket, epoch, cancel):
            await clock.sleep(10)

    provider = Slow()
    run = session(tmp_path, provider, clock)
    assert await run.prepare()
    task = asyncio.create_task(run.capture("test"))
    await clock.drain()
    await clock.advance_and_drain(0.25)
    assert not await task
    assert "release" not in provider.calls
    assert await run.release()


async def test_release_timeout_is_partial_and_late_task_remains_tracked(tmp_path):
    clock = MockClock()
    finish = asyncio.Event()

    class SlowRelease(Provider):
        async def release(self, ticket):
            self.calls.append("release")
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()

    provider = SlowRelease()
    run = session(tmp_path, provider, clock)
    assert await run.prepare()
    assert await run.capture("test")
    task = asyncio.create_task(run.materialize())
    # materialize includes real background file I/O; yield until release has started.
    while "release" not in provider.calls:
        await clock.sleep(0)
    await clock.advance_and_drain(2)
    assert not await task
    assert run.result()["resource_pending"]
    finish.set()
    await clock.drain()
    assert not run.result()["resource_pending"]
    assert run.result()["status"] == "partial"


async def test_materialize_timeout_rejects_late_output(tmp_path):
    clock = MockClock()
    finish = asyncio.Event()

    class LateWrite(Provider):
        async def materialize(self, view, sink, cancel):
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()
            with pytest.raises(ValueError, match="sink_closed"):
                await sink.write("late", chunks())

    provider = LateWrite()
    run = session(tmp_path, provider, clock)
    assert await run.prepare()
    assert await run.capture("test")
    task = asyncio.create_task(run.materialize(timeout=1))
    await clock.drain()
    await clock.advance_and_drain(1)
    assert not await task
    assert run.result()["resource_pending"]
    finish.set()
    await clock.drain(64)
    assert "release" in provider.calls
    assert not (tmp_path / "sink/late").exists()


async def test_synchronous_capture_over_budget_is_rejected(tmp_path):
    clock = MockClock()

    class OverBudget(Provider):
        async def capture(self, ticket, epoch, cancel):
            clock.advance(0.3)
            return BackupView(epoch, 1, "revision", ticket.handle)

    run = session(tmp_path, OverBudget(), clock)
    assert await run.prepare()
    assert not await run.capture("test")
    assert run.result()["errors"][-1]["reason"] == "provider_timeout"
    await run.release()


async def test_provider_self_cancel_is_partial_not_outer_cancellation(tmp_path):
    class SelfCancel(Provider):
        async def prepare(self, request, cancel):
            raise asyncio.CancelledError()

    run = session(tmp_path, SelfCancel(), MockClock())
    assert not await run.prepare()
    assert run.result()["status"] == "partial"


async def test_swallowed_sink_violation_still_invalidates_provider(tmp_path):
    class Swallow(Provider):
        async def materialize(self, view, sink, cancel):
            await sink.write("state", chunks())
            try:
                await sink.write("STATE", chunks())
            except ValueError:
                pass

    run = session(tmp_path, Swallow(), MockClock())
    assert await run.prepare()
    assert await run.capture("test")
    assert not await run.materialize()
    assert run.result()["status"] == "partial"


@pytest.mark.parametrize("mutation", ["replace", "invalid"])
async def test_materialize_cannot_change_captured_asset_dependencies(tmp_path, mutation):
    from clytie.sdk.types import AssetRef

    ref = AssetRef(asset_id="a" * 64, size=1, media_type="text/plain", origin="fake")

    class Changed(Provider):
        async def capture(self, ticket, epoch, cancel):
            return BackupView(epoch, 1, "revision", ticket.handle, (ref,))

        async def materialize(self, view, sink, cancel):
            object.__setattr__(view, "assets", () if mutation == "replace" else (object(),))

    run = session(tmp_path, Changed(), MockClock())
    assert await run.prepare()
    assert await run.capture("test")
    assert not await run.materialize()
    assert run.result()["errors"][-1]["reason"] == "provider_contract"
    assert run.released
