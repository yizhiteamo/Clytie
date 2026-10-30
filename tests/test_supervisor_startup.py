"""Issue #49：报告归属隔离、结构化记忆启动错误；只用临时实例。"""

import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from clytie.config import load_home
from clytie.memory.transfer import runtime_lock
from clytie.sdk.testing import MockClock
from clytie.supervisor.companion import Companion, StartupBlocked

ROOT = Path(__file__).resolve().parents[1]


def home_copy(tmp_path, *, two=False):
    home = tmp_path / "home"
    shutil.copytree(ROOT / "examples/home", home)
    if two:
        instance = yaml.safe_load((home / "clytie.local.yaml").read_text(encoding="utf-8"))
        instance["companions"].append("companion_b")
        instance["accounts"]["example_b"] = {"channel": "fake-channel", "account": "fictional-b"}
        shutil.copytree(home / "companions/companion_a", home / "companions/companion_b")
        path = home / "companions/companion_b/companion.yaml"
        companion = yaml.safe_load(path.read_text(encoding="utf-8"))
        companion["id"] = "companion_b"
        companion["channels"][0]["account"] = "example_b"
        path.write_text(yaml.safe_dump(companion), encoding="utf-8")
        (home / "clytie.local.yaml").write_text(yaml.safe_dump(instance), encoding="utf-8")
    return home


async def test_only_invalid_companion_blocked_others_start(tmp_path):
    home = home_copy(tmp_path, two=True)
    path = home / "companions/companion_b/companion.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["unknown_field"] = True
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    report = load_home(home)
    assert not report.valid and not report.global_errors
    assert {c["id"]: c["valid"] for c in report.companions} == {
        "companion_a": True,
        "companion_b": False,
    }
    good, bad = (
        Companion(home, "companion_a", MockClock()),
        Companion(home, "companion_b", MockClock()),
    )
    with pytest.raises(StartupBlocked) as exc:
        await bad.start()
    assert exc.value.reason == "config_invalid"
    await good.start()
    assert list(good.host.settings) == ["companion_a"]
    await good.stop()


@pytest.mark.parametrize(
    "failure", ["global", "account_conflict", "shared_manifest", "global_plugin_config"]
)
def test_shared_errors_are_global_not_filtered_by_file(tmp_path, failure):
    home = home_copy(tmp_path, two=True)
    instance_path = home / "clytie.local.yaml"
    instance = yaml.safe_load(instance_path.read_text(encoding="utf-8"))
    if failure == "global":
        instance["supervisor"] = {"hang_seconds": 10, "heartbeat_seconds": 5}
        instance_path.write_text(yaml.safe_dump(instance), encoding="utf-8")
    elif failure == "global_plugin_config":
        instance["plugins"]["fake-model"] = {"config": {"fictional_unknown": True}}
        instance_path.write_text(yaml.safe_dump(instance), encoding="utf-8")
    elif failure == "account_conflict":
        path = home / "companions/companion_b/companion.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["channels"][0]["account"] = "example_bot"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
    else:
        directory = home / "plugins/fake-model"
        directory.mkdir(parents=True)
        (directory / "plugin.json").write_text("{}", encoding="utf-8")
    report = load_home(home)
    assert report.global_errors
    assert all(not c["valid"] for c in report.companions)


@pytest.mark.parametrize(
    "failure",
    ["too_new", "unsupported", "incomplete", "snapshot_write", "snapshot_check", "locked"],
)
async def test_memory_failure_has_structured_startup_reason(tmp_path, monkeypatch, failure):
    home = home_copy(tmp_path)
    directory = home / "companions/companion_a/memory"
    directory.mkdir(parents=True)
    path = directory / "memory.sqlite3"
    if failure in {"too_new", "unsupported", "incomplete"}:
        db = sqlite3.connect(path)
        db.execute(
            "PRAGMA user_version="
            + str({"too_new": 3, "unsupported": -1, "incomplete": 2}[failure])
        )
        db.close()
    elif failure.startswith("snapshot"):
        from clytie.memory.store import MemoryStore

        store = MemoryStore(path, clock=MockClock(), config={}, instance={})
        for table in ("recollect_progress", "fade", "conversation_registry", "revision_origins"):
            store.db.execute("DROP TABLE " + table)
        store.db.execute("PRAGMA user_version=1")
        store.close()
        from clytie.memory import migration

        if failure == "snapshot_write":

            def fail(*args, **kwargs):
                raise OSError("fictional write failure")

            monkeypatch.setattr(migration, "atomic_write", fail)
        else:
            monkeypatch.setattr(migration, "un64", lambda _: b"invalid")
    lock = runtime_lock(home, "companion_a") if failure == "locked" else None
    if lock:
        lock.__enter__()
    runtime = Companion(home, "companion_a", MockClock())
    try:
        with pytest.raises(StartupBlocked) as exc:
            await runtime.start()
        assert exc.value.reason == ("lock_held" if lock else "migration_blocked")
        assert not runtime.feeds
    finally:
        await runtime.host.close()
        runtime.runtime.close()
        if lock:
            lock.__exit__(None, None, None)


async def test_persistent_silence_is_set_before_platform_connect(tmp_path, monkeypatch):
    from clytie.supervisor.common import write

    home = home_copy(tmp_path)
    write(home / "supervisor/silence.json", {"on": True, "since": "fictional", "by": "owner"})
    runtime = Companion(home, "companion_a", MockClock())
    original = runtime.host._load_class
    connected = []

    def load(manifest):
        cls, module = original(manifest)
        if manifest.id == "fake-channel":
            factory_name = manifest.data["contributes"]["channel"]["factory"]
            factory = getattr(cls, factory_name)

            def wrapped(self):
                channel = factory(self)
                connect = channel.connect

                async def check():
                    assert runtime.kernel.delivery.silenced
                    connected.append(True)
                    await connect()

                channel.connect = check
                return channel

            setattr(cls, factory_name, wrapped)
        return cls, module

    monkeypatch.setattr(runtime.host, "_load_class", load)
    await runtime.start()
    assert connected == [True] and runtime.health()["silenced"]
    assert runtime.health()["model"] == "unknown"
    await runtime.stop()


@pytest.mark.parametrize("code", ["auth_invalid", "transient", None])
async def test_connect_failure_uses_error_code_not_message(tmp_path, monkeypatch, code):
    from clytie.sdk import PluginUnavailable

    home = home_copy(tmp_path)
    runtime = Companion(home, "companion_a", MockClock())
    original = runtime.host._load_class

    def load(manifest):
        cls, module = original(manifest)
        if manifest.id == "fake-channel":
            name = manifest.data["contributes"]["channel"]["factory"]
            factory = getattr(cls, name)

            def wrapped(self):
                channel = factory(self)

                async def fail():
                    if code is not None:
                        raise PluginUnavailable(code, "fictional auth_invalid misleading message")
                    raise RuntimeError("auth_invalid")

                channel.connect = fail
                return channel

            setattr(cls, name, wrapped)
        return cls, module

    monkeypatch.setattr(runtime.host, "_load_class", load)
    try:
        if code == "auth_invalid":
            with pytest.raises(StartupBlocked) as exc:
                await runtime.start()
            assert exc.value.reason == code
        else:
            with pytest.raises(PluginUnavailable if code else RuntimeError):
                await runtime.start()
    finally:
        await runtime.stop()
