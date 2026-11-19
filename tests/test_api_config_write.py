import pytest

from clytie.api.auth import APIError
from clytie.api.config_write import ConfigWriter, patch_source, proposal_source
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
    after = proposal_source(before, "qq-onebot", "bot", patch)
    assert "url: ws://127.0.0.1:1 # keep" in after
    assert after.startswith("# keep\n") and after.endswith("plugins: {}\n")
    import yaml

    assert yaml.safe_load(after)["channels"][0]["config"]["emoji_ids"] == patch["emoji_ids"]


def test_emoji_multiline_preserves_comments_and_other_keys():
    before = 'channels:\n  - plugin: qq-onebot\n    account: bot\n    config:\n      emoji_ids:\n        "👌": "9" # keep-note\n        # keep-between\n        "⏳": "8"\n      max_text_chars: 123\n'
    after = proposal_source(
        before, "qq-onebot", "bot", {"emoji_ids": {"👌": "1", "⏳": "2", "✅": "3", "❌": "4"}}
    )
    assert "# keep-note" in after and "# keep-between" in after
    assert "      max_text_chars: 123\n" in after
    import yaml

    assert yaml.safe_load(after)["channels"][0]["config"]["emoji_ids"] == {
        "👌": "1",
        "⏳": "2",
        "✅": "3",
        "❌": "4",
    }


@pytest.mark.parametrize(
    "before, changes, expected",
    [
        (
            "config: {flag: false, keep: 'style'}",
            {"flag": True, "count": 2},
            """config: {"count": 2, flag: true, keep: 'style'}""",
        ),
        (
            "config:\n      list:\n      - before\n      other: 1  # keep",
            {"list": ["after"]},
            'config:\n      list:\n       ["after"]\n      other: 1  # keep',
        ),
        (
            "enabled: true # keep",
            {"flag": False},
            '"config": {"flag": false}\n    enabled: true # keep',
        ),
    ],
)
def test_ordinary_plugin_proposals_preserve_sibling_format(before, changes, expected):
    prefix = "plugins:\n  sample:\n    "
    assert (
        proposal_source(prefix + before + "\n", "sample", None, changes) == prefix + expected + "\n"
    )


async def test_active_tool_can_propose_only_for_explicit_tool_binding(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    import yaml

    from clytie.api.operations import companion_operation
    from clytie.plugin_host.manifests import read_manifest

    from .test_config import make_home, write_yaml

    home, repo, _, companion = make_home(tmp_path)
    monkeypatch.setattr("clytie.config.repository_root", lambda: repo)
    monkeypatch.setattr("clytie.api.config_write.repository_root", lambda: repo)
    cid, pid = "companion_a", "sample-tool"
    path = home / "companions" / cid / "companion.yaml"
    companion["plugins"][pid] = {"enabled": True}
    write_yaml(path, companion)
    manifest_path = repo / "plugins" / pid / "plugin.json"
    manifest_path.parent.mkdir()
    manifest = json.loads((repo / "plugins/fake-channel/plugin.json").read_bytes())
    manifest.update(
        id=pid, category="tool", config_writable=["level"], requires={"sdk": ">=0.1,<1"}
    )
    manifest["config"]["properties"]["level"] = {"type": "integer", "minimum": 1}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    async def propose(request):
        return {"status": 200, "json": {"changes": {"level": 2}}}

    registration = SimpleNamespace(
        plugin_id=pid,
        implementation=propose,
        declaration={"method": "GET", "path": f"/plugins/{pid}/config/proposal"},
    )
    host = SimpleNamespace(
        is_active=lambda *_: True,
        unit_configs={},
        settings={cid: {"plugins": {}}},
        registrations=lambda *_: [registration],
        runtimes={
            (cid, pid): SimpleNamespace(manifest=read_manifest(manifest_path, repo_root=repo))
        },
    )
    runtime = SimpleNamespace(host=host, home=home, clock=MockClock(), cid=cid)
    args = {"pid": pid, "session": "sample"}
    with pytest.raises(APIError, match="config_binding_mismatch"):
        await companion_operation(runtime, "config_proposal", args)
    host.settings[cid]["plugins"] = companion["plugins"]
    proposal = await companion_operation(runtime, "config_proposal", args)
    writer = ConfigWriter(home, runtime.clock)
    assert writer.save_proposal(cid, **proposal) == {"saved": True}
    assert yaml.safe_load(path.read_bytes())["plugins"][pid] == {
        "enabled": True,
        "config": {"level": 2},
    }
    manifest["config_writable"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(APIError, match="config_not_writable"):
        writer.save_proposal(cid, **proposal)
    assert path.read_bytes() == before


def test_plugin_patch_rejects_null_map():
    with pytest.raises(APIError) as caught:
        patch_source("plugins: {}\n", {"plugins": None})
    assert caught.value.code == "config_not_writable"
