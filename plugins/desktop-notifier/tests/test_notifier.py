from pathlib import Path

import pytest

from clytie.sdk.testing import FakeContext, MockClock, assert_notifier_contract
from clytie.sdk.testing.lifecycle import plugin_from_manifest
from clytie.sdk.types import Notice


@pytest.mark.parametrize(
    ("config", "expected_id"),
    [
        ({}, r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"),
        ({"app_id": "Example.App ' \" <>"}, "Example.App ' \" <>"),
    ],
    ids=["windows-powershell", "configured-app-id"],
)
async def test_desktop_notifier_contract_uses_fake_subprocess_only(tmp_path, config, expected_id):
    ctx = FakeContext(
        plugin_id="desktop-notifier",
        clock=MockClock(),
        root=tmp_path,
        permissions=["subprocess"],
        config=config,
    )
    async with plugin_from_manifest(Path(__file__).parents[1] / "plugin.json", ctx) as loaded:
        notifier = loaded.factory("notifier")
        await assert_notifier_contract(
            notifier,
            Notice(level="info", title="fictional", body="fictional", dedupe_key="example"),
        )
    assert len(ctx.subprocess.calls) == 1
    args, _, env, _ = ctx.subprocess.calls[0]
    assert env["CLYTIE_NOTICE_APP_ID"] == expected_id
    assert expected_id not in " ".join(args)
    assert "CreateToastNotifier($env:CLYTIE_NOTICE_APP_ID)" in args[-1]
