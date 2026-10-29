"""最后一轮：关闭容错与真实版本短锁超时。"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from clytie.plugin_host.dependencies import stop_order
from clytie.plugin_host.locking import MetadataLockTimeout, file_lock, process_identity

from .host_helpers import SOURCE, enable, host, plugin


async def test_close_after_required_dependency_reload_fails(tmp_path):
    base = plugin(tmp_path, "base", hot_reload=True)
    plugin(tmp_path, "dependent", dependencies={"base": {"version": ">=1"}})
    plugin(tmp_path, "other")
    h = host(tmp_path)
    h.configure("companion_b")
    await enable(h, "dependent")
    await enable(h, "other", "companion_b")
    (base.parent / "plugin.py").write_text(
        SOURCE.replace("self.ctx = ctx", 'raise ValueError("fictional reload failure")'),
        encoding="utf-8",
    )
    assert (await h.reconfigure("companion_a", "base"))["reason"] == "start_failed"
    assert h.runtimes["companion_a", "dependent"].status == "active"
    result = await h.close()
    assert result == {
        "companion_a": {"stopped": ["dependent"], "not_stopped": {}},
        "companion_b": {"stopped": ["other"], "not_stopped": {}},
    }
    assert h.runtimes["companion_a", "dependent"].status == "disabled"
    assert h.runtimes["companion_b", "other"].status == "disabled"


@pytest.mark.parametrize("point", ["sort", "stop"])
async def test_close_exception_is_scoped_to_one_companion(tmp_path, monkeypatch, point):
    import clytie.plugin_host.host as module

    plugin(tmp_path, "first")
    plugin(tmp_path, "other")
    h = host(tmp_path)
    h.configure("companion_b")
    await enable(h, "first")
    await enable(h, "other", "companion_b")
    order, stop = module.stop_order, h._stop

    def fail_order(loaded):
        if "first" in loaded:
            raise ValueError("fictional sorting failure")
        return order(loaded)

    async def fail_stop(cid, pid, reason):
        if cid == "companion_a":
            raise OSError("fictional stop failure")
        return await stop(cid, pid, reason)

    monkeypatch.setattr(module, "stop_order", fail_order if point == "sort" else order)
    monkeypatch.setattr(h, "_stop", fail_stop if point == "stop" else stop)
    result = await h.close()
    assert result["companion_a"] == {
        "stopped": [],
        "not_stopped": {"first": {"error": "ValueError" if point == "sort" else "OSError"}},
    }
    assert result["companion_b"] == {"stopped": ["other"], "not_stopped": {}}
    monkeypatch.setattr(module, "stop_order", order)
    monkeypatch.setattr(h, "_stop", stop)
    await h.close()


def test_stop_order_ignores_missing_dependencies_and_orders_cycle_remainder():
    def manifest(*deps):
        return SimpleNamespace(data={"dependencies": {p: {"version": ">=1"} for p in deps}})

    assert stop_order({"base": manifest("missing"), "dependent": manifest("base")}) == (
        "dependent",
        "base",
    )
    assert stop_order(
        {
            "cycle_b": manifest("cycle_a"),
            "cycle_a": manifest("cycle_b"),
            "leaf": manifest("cycle_b"),
        }
    ) == ("leaf", "cycle_a", "cycle_b")


async def test_metadata_real_wait_counter_and_timeout(tmp_path, monkeypatch):
    import clytie.plugin_host.locking as module

    h = host(tmp_path)
    path = tmp_path / "companions/companion_a/plugin-versions.lock"

    async def advance(seconds):
        h.clock.advance(seconds)
        await h.clock.sleep(0)

    h.operations.clock = SimpleNamespace(sleep=advance)
    attempts = []
    real_lock = module.file_lock

    @contextmanager
    def counted(*args, **kwargs):
        attempts.append(args[0])
        with real_lock(*args, **kwargs):
            yield

    with real_lock(path, {**process_identity(), "operation": "version_record"}):
        monkeypatch.setattr(module, "file_lock", counted)
        with pytest.raises(MetadataLockTimeout) as caught:
            async with h.operations.metadata("companion_a"):
                pytest.fail("busy metadata lock was bypassed")
        assert len(attempts) == 501
        assert caught.value.enabled == ()
        assert caught.value.patch == {"plugins": {}}
    async with h.operations.metadata("companion_a"):
        pass


async def test_later_metadata_timeout_reports_enabled_plugins_and_patch(tmp_path, monkeypatch):
    plugin(tmp_path, "base")
    plugin(tmp_path, "dependent", dependencies={"base": {"version": ">=1"}})
    h = host(tmp_path)

    async def advance(seconds):
        h.clock.advance(seconds)
        await h.clock.sleep(0)

    h.operations.clock = SimpleNamespace(sleep=advance)
    start = h._start
    held = []

    async def start_and_hold(cid, manifest, **kwargs):
        started = await start(cid, manifest, **kwargs)
        if manifest.id == "base" and started:
            guard = file_lock(
                tmp_path / "companions/companion_a/plugin-versions.lock",
                {**process_identity(), "operation": "version_record"},
            )
            guard.__enter__()
            held.append(guard)
        return started

    monkeypatch.setattr(h, "_start", start_and_hold)
    try:
        with pytest.raises(MetadataLockTimeout) as caught:
            await h.apply(h.plan_enable("companion_a", "dependent").plan_id)
        assert caught.value.enabled == ("base",)
        assert caught.value.patch == {"plugins": {"base": {"enabled": True}}}
        assert h.runtimes["companion_a", "base"].status == "active"
        assert h.runtimes["companion_a", "dependent"].status == "disabled"
        assert h._version("companion_a", "base")["data_version"] == 1
        assert h._version("companion_a", "dependent") is None
    finally:
        for guard in held:
            guard.__exit__(None, None, None)
        await h.close()
