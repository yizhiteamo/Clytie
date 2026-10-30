"""清单导入器经宿主生命周期和 CLI 完成预览、确认及重做。"""

import json

from clytie.api.memory import MemoryAPI
from clytie.cli import main
from clytie.plugin_host import PluginHost
from clytie.sdk.testing import MockClock

from .host_helpers import ROOT, SOURCE, plugin
from .test_config import make_home, write_yaml


def test_manifest_importer_cli_reimport_approval_and_lifecycle(tmp_path, monkeypatch, capsys):
    home, _, _, companion = make_home(tmp_path)
    companion.setdefault("plugins", {})["fictional-importer"] = {"enabled": True, "config": {}}
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    plugin(
        home,
        "fictional-importer",
        SOURCE
        + """
    def make_importer(self):
        return self
    def detect(self, source):
        return source.name == "fictional-source.txt"
    def read(self, source, cancel):
        from clytie.sdk.types import ImportCandidate
        candidate = ImportCandidate(record_id="e" * 32, kind="preference", content="fictional memory",
            visibility="public", created_at=self.ctx.clock.now(), updated_at=self.ctx.clock.now(),
            subject_account={"channel": "fake-channel", "account": "1000000001"},
            origin_conversation={"channel": "fake-channel", "kind": "group", "platform_id": "1000000002"},
            proposed_basis="owner_confirmed")
        class Iterator:
            done = False
            def __aiter__(self):
                return self
            async def __anext__(self):
                cancel.raise_if_cancelled()
                if self.done:
                    raise StopAsyncIteration
                self.done = True
                return candidate
        return Iterator()
""",
        contributes={
            "memory": {"importer": {"factory": "make_importer", "formats": ["fictional"]}}
        },
    )
    api = MemoryAPI(home, repo_root=ROOT, clock=MockClock())
    from clytie.config import load_home

    report = load_home(home, repo_root=ROOT)
    assert report.valid, [(e.pointer, e.expected) for e in report.errors]
    monkeypatch.setattr("clytie.memory.cli.MemoryAPI", lambda home: api)
    monkeypatch.setattr(
        "clytie.memory.cli.getpass.getpass",
        lambda _: (_ for _ in ()).throw(AssertionError("no decryption needed")),
    )
    calls, closed = [], []
    invoke, close = PluginHost.invoke, PluginHost.close

    async def recording(self, cid, pid, *args, **kwargs):
        calls.append((pid, kwargs["method"]))
        return await invoke(self, cid, pid, *args, **kwargs)

    async def closing(self):
        result = await close(self)
        closed.append(result)
        return result

    monkeypatch.setattr(PluginHost, "invoke", recording)
    monkeypatch.setattr(PluginHost, "close", closing)
    source = home / "fictional-source.txt"
    source.write_text("fictional source", encoding="utf-8")
    args = [
        "memory",
        "import",
        "plan",
        "companion_a",
        str(source),
        "--mode",
        "new",
        "--home",
        str(home),
        "--importer",
        "fictional-importer",
        "--confirm-identity",
    ]
    assert main([*args, "--approve-proposals"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert not (home / "companions/companion_a/memory/memory.sqlite3").exists()
    assert main(["memory", "import", "apply", plan["id"], "--home", str(home)]) == 0
    capsys.readouterr()
    with api.open("companion_a", readonly=True) as store:
        assert store.get_raw("e" * 32).basis == "owner_confirmed"
        uid = store.uid
    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["downgrade_count"] == 1 and second["reimport"]
    assert main(["memory", "import", "apply", second["id"], "--home", str(home)]) == 0
    with api.open("companion_a", readonly=True) as store:
        assert store.uid == uid and store.get_raw("e" * 32).basis == "observed"
    assert calls == [
        ("fictional-importer", method) for _ in range(4) for method in ("detect", "read")
    ]
    assert len(closed) == 4
