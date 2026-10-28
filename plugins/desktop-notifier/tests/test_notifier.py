from pathlib import Path

from clytie.sdk.testing import FakeContext, MockClock, assert_notifier_contract
from clytie.sdk.testing.lifecycle import plugin_from_manifest
from clytie.sdk.types import Notice


async def test_desktop_notifier_contract_uses_fake_subprocess_only(tmp_path):
    ctx = FakeContext(
        plugin_id="desktop-notifier", clock=MockClock(), root=tmp_path, permissions=["subprocess"]
    )
    async with plugin_from_manifest(Path(__file__).parents[1] / "plugin.json", ctx) as loaded:
        notifier = loaded.factory("notifier")
        await assert_notifier_contract(
            notifier,
            Notice(level="info", title="fictional", body="fictional", dedupe_key="example"),
        )
    assert len(ctx.subprocess.calls) == 1
