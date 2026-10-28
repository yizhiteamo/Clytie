import json

from clytie.config import load_home
from clytie.plugin_host import PluginHost
from clytie.sdk.testing import MockClock

from .host_helpers import SOURCE
from .test_config import make_home, update_plugin, write_yaml
from .test_host_upgrades import MIGRATE, update


async def test_validated_fourth_layer_per_channel_unit_and_primary_contributions(tmp_path):
    home, repo, instance, companion = make_home(tmp_path)
    schema = {
        "type": "object",
        "properties": {"required_inline": {"type": "integer"}},
        "required": ["required_inline"],
    }
    update_plugin(repo, schema)
    companion["model"]["config"] = {"required_inline": 4}
    for pid in ("fake-model", "fake-channel"):
        path = repo / "plugins" / pid / "plugin.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["contributes"]["services"] = [
            {"name": pid + ".state", "version": "1.0", "methods": ["get"], "handler": "service"}
        ]
        manifest["config"] = schema
        manifest["requires"]["sdk"] = ">=0.3,<0.4"
        manifest["hot_reload"] = True
        if pid == "fake-model":
            manifest["contributes"]["model"] = {"factory": "create_model"}
        else:
            manifest["contributes"]["channel"] = {
                "factory": "create_channel",
                "platform": "fictional",
            }
        path.write_text(json.dumps(manifest), encoding="utf-8")
        source = (
            SOURCE
            + """
    def create_model(self):
        from clytie.sdk.testing import FakeModel
        return FakeModel(self.ctx)
    def create_channel(self):
        from clytie.sdk.testing import FakeChannel
        return FakeChannel(self.ctx)
    async def service(self, method, args):
        return {"inline": self.ctx.config["required_inline"]}
"""
        )
        (path.parent / "plugin.py").write_text(source, encoding="utf-8")
    instance["accounts"]["second_bot"] = {"channel": "fake-channel", "account": "1000000004"}
    companion["channels"][0]["config"] = {"required_inline": 5}
    companion["channels"].append(
        {"plugin": "fake-channel", "account": "second_bot", "config": {"required_inline": 6}}
    )
    write_yaml(home / "clytie.local.yaml", instance)
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=repo)
    assert report.valid, report.to_dict()
    h = PluginHost(repo, home, clock=MockClock())
    h.configure_report(report)
    for pid in ("fake-model", "fake-channel"):
        result = await h.apply(h.plan_enable("companion_a", pid).plan_id)
        assert not result["failed"], h.failures
    channels = h.runtimes["companion_a", "fake-channel"]
    assert [u.ctx.config["required_inline"] for u in channels.units] == [5, 6]
    assert channels.units[0].storage is channels.units[1].storage
    assert [r.key for r in h.registrations("companion_a", "channel")] == [
        "example_bot",
        "second_bot",
    ]
    assert len(h.registrations("companion_a", "service")) == 2
    assert await h.invoke(
        "companion_a", "fake-model", "service", "fake-model.state", "get", {}
    ) == {"inline": 4}
    assert await h.invoke(
        "companion_a", "fake-channel", "service", "fake-channel.state", "get", {}
    ) == {"inline": 5}
    assert (
        channels.units[1].ctx.identity.resolve_actor("fictional-unknown").channel_id == "second_bot"
    )
    companion["channels"][1]["config"]["required_inline"] = 8
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    h.configure_report(load_home(home, repo_root=repo))
    assert (await h.reconfigure("companion_a", "fake-channel"))["outcome"] == "applied"
    assert [
        u.ctx.config["required_inline"] for u in h.runtimes["companion_a", "fake-channel"].units
    ] == [5, 8]
    # 平台和模型迁移都只得到始终属性，且保留第四层必填配置。
    for pid, value in (("fake-model", 4), ("fake-channel", 5)):
        await h.apply(h.plan_disable("companion_a", pid).plan_id)
        path = repo / "plugins" / pid / "plugin.json"
        update(path, script=MIGRATE + f'    assert ctx.config["required_inline"] == {value}\n')
        plan = h.plan_migration("companion_a", pid)
        assert (await h.apply_migration(plan.plan_id))["outcome"] == "migrated"
    assert [
        u.ctx.config["required_inline"] for u in h.runtimes["companion_a", "fake-channel"].units
    ] == [5, 8]
    await h.close()
