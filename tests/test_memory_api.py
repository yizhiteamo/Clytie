"""配置定位、只读计划、CLI 交互与无原文状态输出。"""

import json

import pytest

from clytie.api import local_status
from clytie.api.memory import MemoryAPI
from clytie.cli import main
from clytie.config import load_home
from clytie.memory.package import Exports
from clytie.memory.status import notices
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock

from .test_config import make_home, write_yaml


def test_memory_config_defaults_ranges_and_enabled_extension(tmp_path):
    home, repo, _, companion = make_home(tmp_path)
    report = load_home(home, repo_root=repo)
    assert report.valid
    memory = report.plugin_configs["companion_a"]["config"]["memory"]
    assert memory["core_chars"] == 4000 and memory["recall_limit"] == 10
    assert memory["embedder"] is None and memory["reranker"] is None
    assert memory["semantic_min_similarity"] == 0.5
    companion["memory"] = {
        "core_chars": 199,
        "reranker": "fake-model",
        "semantic_min_similarity": 1.1,
    }
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=repo)
    pointers = {error.pointer for error in report.errors}
    assert "/memory/core_chars" in pointers and "/memory/reranker" in pointers
    assert "/memory/semantic_min_similarity" in pointers
    companion["memory"] = {"reranker": "fake-model"}
    companion.setdefault("plugins", {})["fake-model"] = {"enabled": True, "config": {}}
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=repo)
    assert any(
        e.pointer == "/memory/reranker" and "implements" in e.expected for e in report.errors
    )
    path = repo / "plugins/fake-model/plugin.json"
    manifest = json.loads(path.read_bytes())
    manifest["contributes"]["memory"] = {"reranker": {"factory": "create_reranker"}}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = load_home(home, repo_root=repo)
    assert result.valid, result.errors


async def test_plan_without_database_and_apply_creates_after_confirmation(tmp_path):
    home, repo, _, _ = make_home(tmp_path)
    clock = MockClock()
    source_home = tmp_path / "source"
    source = MemoryStore(source_home / "memory.sqlite3", clock=clock, config={}, instance={})
    try:
        source.write(
            {"kind": "fact", "content": "虚构的搬家回忆", "visibility": "public"},
            scene="owner_private",
            conversation_id="test_group",
        )
        package = source_home / "source.enc"
        Exports(source, source_home, "companion_a").export(
            package, passphrase="fictional-long-passphrase"
        )
        api = MemoryAPI(home, repo_root=repo, clock=clock)
        plan = await api.plan(
            "companion_a", package, mode="new", passphrase="fictional-long-passphrase"
        )
        target = home / "companions/companion_a/memory/memory.sqlite3"
        assert not target.exists()
        assert not plan["target_exists"]
        with pytest.raises(ValueError, match="必须确认"):
            await api.apply(plan["id"], passphrase="fictional-long-passphrase")
        assert not target.exists()
        plan = await api.plan(
            "companion_a",
            package,
            mode="new",
            passphrase="fictional-long-passphrase",
            confirm_identity=True,
        )
        imported = await api.apply(plan["id"], passphrase="fictional-long-passphrase")
        assert target.is_file()
        undo = api.undo(None, imported)
        assert api.plan_kind(undo["id"]) == "undo"
        await api.apply(undo["id"])
        with api.open("companion_a", readonly=True) as store:
            assert store.uid == source.uid and all(r.retracted for r in store.all_records())
    finally:
        source.close()


def test_status_and_cli_export_do_not_expose_memory_or_secrets(tmp_path, monkeypatch, capsys):
    home, repo, _, companion = make_home(tmp_path)
    companion["memory"] = {"core_chars": 200}
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    api = MemoryAPI(home, repo_root=repo, clock=MockClock())
    private_text = "虚构的私密内容" * 20
    with api.open("companion_a") as store:
        store.materialize()
        for number in range(3):
            store.write(
                {"kind": "fact", "content": private_text + str(number), "visibility": "private"},
                scene="owner_private",
                conversation_id=None,
                owner="owner",
                explicit=True,
            )
    temporary = home / "run/memory-temp/companion_a-import-fictional.db"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text("fictional retained bytes", encoding="utf-8")
    report = local_status(home, repo_root=repo)
    assert private_text not in json.dumps(report.to_dict(), ensure_ascii=False)
    codes = {n["code"] for n in notices(report, clock=api.clock)}
    assert {
        "memory_core_overflow",
        "memory_keyword_only",
        "memory_temporary_file_retained",
    } <= codes
    from clytie.memory import cli

    monkeypatch.setattr(cli, "MemoryAPI", lambda selected: api)
    passphrase = "fictional-cli-passphrase"
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: passphrase)
    out = home / "memory.enc"
    assert main(["memory", "export", "companion_a", "--out", str(out), "--home", str(home)]) == 0
    printed = capsys.readouterr().out
    assert "恢复码只显示这一次" in printed
    assert private_text not in printed and passphrase not in printed
    assert temporary.exists()
