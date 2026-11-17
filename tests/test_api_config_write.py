import pytest

from clytie.api.auth import APIError
from clytie.api.config_write import ConfigWriter, patch_source
from clytie.sdk.testing import MockClock


def test_scalar_patch_preserves_all_other_bytes():
    before = "# heading\r\nschema_version: 1\r\nplugins:\r\n  echo-tool: # note\r\n    enabled: true # toggle\r\n    config: {text: 'harmless'}\r\n"
    assert patch_source(before, {"plugins": {"echo-tool": {"enabled": False}}}) == before.replace(
        "enabled: true", "enabled: false"
    )


@pytest.mark.parametrize(
    "source",
    ["plugins: &alias {}\n", "plugins:\n  echo-tool: {enabled: true}\n", "plugins:\n  <<: {}\n"],
)
def test_unsafe_yaml_is_not_reformatted(source):
    with pytest.raises(APIError, match="config_not_writable"):
        patch_source(source, {"plugins": {"echo-tool": {"enabled": False}}})


def test_history_retention_and_invalid_write_restore(tmp_path):
    clock = MockClock()
    path = tmp_path / "companions/companion_a/companion.yaml"
    path.parent.mkdir(parents=True)
    original = "plugins:\n  echo-tool:\n    enabled: true\n"
    path.write_text(original, encoding="utf-8")
    writer = ConfigWriter(tmp_path, clock, validate=lambda: True)
    for index in range(12):
        writer.save("companion_a", {"plugins": {"echo-tool": {"enabled": bool(index % 2)}}})
        clock.advance(1)
    assert len(list((path.parent / "config-history").iterdir())) == 10
    before = path.read_bytes()
    writer.validate = lambda: False
    with pytest.raises(APIError, match="config_invalid_after_write"):
        writer.save("companion_a", {"plugins": {"echo-tool": {"enabled": False}}})
    assert path.read_bytes() == before


def _capture_config_lock(home, ready, release):
    from clytie.backup.configuration import config_commit_lock

    with config_commit_lock(home):
        ready.set()
        assert release.wait(15)


@pytest.mark.parametrize("release_on_contention", [True, False])
def test_config_save_coordinates_with_real_capture_process(
    tmp_path, monkeypatch, release_on_contention
):
    import multiprocessing
    from contextlib import contextmanager
    from pathlib import Path

    from clytie.backup import coordination
    from clytie.plugin_host.locking import OperationBusy

    path = tmp_path / "companions/companion_a/companion.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("plugins:\n  echo-tool:\n    enabled: false\n", encoding="utf-8")
    before = path.read_bytes()
    clock = MockClock()
    # importlib 模式不添加仓库路径；spawn 需要独立导入目标测试模块。
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_capture_config_lock, args=(tmp_path, ready, release))
    process.start()
    original = coordination.file_lock
    observed = []

    @contextmanager
    def observed_lock(*args, **kwargs):
        manager = original(*args, **kwargs)
        try:
            manager.__enter__()
        except OperationBusy:
            observed.append(True)
            if release_on_contention:
                release.set()
            else:
                clock.advance(3)
            raise
        try:
            yield
        finally:
            manager.__exit__(None, None, None)

    try:
        assert ready.wait(10)
        monkeypatch.setattr(coordination, "file_lock", observed_lock)
        writer = ConfigWriter(tmp_path, clock, validate=lambda: True)
        patch = {"plugins": {"echo-tool": {"enabled": True}}}
        if release_on_contention:
            assert writer.save("companion_a", patch) == {"saved": True}
            assert b"enabled: true" in path.read_bytes()
        else:
            with pytest.raises(APIError, match="config_not_writable"):
                writer.save("companion_a", patch)
            assert path.read_bytes() == before
        assert observed
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(5)
    assert process.exitcode == 0


def test_emoji_patch_preserves_unrelated_bytes():
    before = "# keep\nchannels:\n  - plugin: qq-onebot\n    account: bot\n    config:\n      url: ws://127.0.0.1:1 # keep\n      emoji_ids: {}\nplugins: {}\n"
    patch = {"emoji_ids": {"👌": "1", "⏳": "2", "✅": "3", "❌": "4"}}
    after = patch_source(before, patch)
    assert "url: ws://127.0.0.1:1 # keep" in after
    assert after.startswith("# keep\n") and after.endswith("plugins: {}\n")
    import yaml

    assert yaml.safe_load(after)["channels"][0]["config"]["emoji_ids"] == patch["emoji_ids"]


def test_emoji_multiline_preserves_comments_and_other_keys():
    before = 'channels:\n  - plugin: qq-onebot\n    config:\n      emoji_ids:\n        "👌": "9" # keep-note\n        # keep-between\n        "⏳": "8"\n      max_text_chars: 123\n'
    after = patch_source(before, {"emoji_ids": {"👌": "1", "⏳": "2", "✅": "3", "❌": "4"}})
    assert "# keep-note" in after and "# keep-between" in after
    assert "      max_text_chars: 123\n" in after
    import yaml

    assert yaml.safe_load(after)["channels"][0]["config"]["emoji_ids"] == {
        "👌": "1",
        "⏳": "2",
        "✅": "3",
        "❌": "4",
    }
