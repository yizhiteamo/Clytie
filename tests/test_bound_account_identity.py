"""账号身份和密钥只来自各自绑定；所有数据均为手写虚构数据。"""

import pytest

from clytie.plugin_host import credentials as credentials_module
from clytie.plugin_host.identity import IdentityResolver
from clytie.sdk import CredentialUnavailable

CREDENTIAL_FILE = "secret"


def test_self_account_is_bound_and_fixed_for_lifecycle():
    instance = {
        "accounts": {
            "channel_a": {"account": "fictional_account_a"},
            "channel_b": {"account": "fictional_account_b"},
        }
    }
    first = IdentityResolver("channel_a", "fake-channel", instance)
    second = IdentityResolver("channel_b", "fake-channel", instance)
    instance["accounts"]["channel_a"]["account"] = "fictional_replacement"
    assert first.self_account() == "fictional_account_a"
    assert second.self_account() == "fictional_account_b"


def test_self_account_without_binding_is_none():
    assert IdentityResolver("absent", "fake-channel", {}).self_account() is None


async def test_account_secret_fresh_reads_and_binding_isolation(tmp_path, caplog):
    root = tmp_path / "secrets"
    root.mkdir()
    (root / "first.txt").write_text("  fictional_first_token\n", encoding="utf-8")
    (root / "second.txt").write_text("fictional_second_token", encoding="utf-8")
    account = {CREDENTIAL_FILE: "first.txt"}
    first = credentials_module.ChannelCredentials(tmp_path, "fake-channel", {}, account)
    second = credentials_module.ChannelCredentials(
        tmp_path, "fake-channel", {}, {CREDENTIAL_FILE: "second.txt"}
    )
    account["secret"] = "second.txt"
    assert await first.account_secret() == "fictional_first_token"
    assert await second.account_secret() == "fictional_second_token"
    (root / "first.txt").write_text("fictional_rotated_token", encoding="utf-8")
    assert await first.account_secret() == "fictional_rotated_token"
    assert "fictional_first_token" not in caplog.text
    assert "fictional_second_token" not in caplog.text
    assert "fictional_rotated_token" not in caplog.text


@pytest.mark.parametrize(
    ("account", "content", "code"),
    [
        (None, None, "unknown_ref"),
        ({}, None, "missing"),
        ({CREDENTIAL_FILE: "key.txt"}, None, "missing"),
        ({CREDENTIAL_FILE: "key.txt"}, b" \n", "empty"),
        ({CREDENTIAL_FILE: "key.txt"}, b"\xfffictional_secret_marker", "unreadable"),
        ({CREDENTIAL_FILE: "../outside.txt"}, None, "unreadable"),
    ],
)
async def test_account_secret_errors_do_not_expose_paths_or_contents(
    tmp_path, account, content, code
):
    root = tmp_path / "secrets"
    root.mkdir()
    if content is not None:
        (root / "key.txt").write_bytes(content)
    credentials = credentials_module.ChannelCredentials(tmp_path, "fake-channel", {}, account)
    with pytest.raises(CredentialUnavailable) as error:
        await credentials.account_secret()
    assert error.value.code == code
    assert str(error.value) == code
    assert error.value.__cause__ is None


async def test_channel_credentials_preserve_reference_authorization(tmp_path):
    root = tmp_path / "secrets"
    root.mkdir()
    (root / "key.txt").write_text("fictional_token", encoding="utf-8")
    declarations = {"api": {CREDENTIAL_FILE: "key.txt", "plugins": ["other-plugin"]}}
    credentials = credentials_module.ChannelCredentials(
        tmp_path, "fake-channel", declarations, {CREDENTIAL_FILE: "key.txt"}
    )
    assert await credentials.account_secret() == "fictional_token"
    with pytest.raises(CredentialUnavailable, match="^not_permitted$"):
        await credentials.read("api")
    with pytest.raises(CredentialUnavailable, match="^unknown_ref$"):
        await credentials.read("absent")
    assert not hasattr(
        credentials_module.Credentials(tmp_path, "other-plugin", {}), "account_secret"
    )
