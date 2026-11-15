"""凭据只用临时虚构文件；生产宿主授权与生命周期回归。"""

import pytest

from clytie.plugin_host.credentials import Credentials
from clytie.sdk import CredentialUnavailable
from clytie.sdk.testing import assert_credentials_contract, assert_plugin_context_contract

from .host_helpers import SOURCE, enable, host, plugin

CREDENTIAL_FILE = "secret"


def reader(tmp_path):
    (tmp_path / "secrets").mkdir()
    return Credentials(
        tmp_path, "allowed", {"example": {CREDENTIAL_FILE: "key.txt", "plugins": ["allowed"]}}
    )


async def test_credentials_read_fresh_and_strip(tmp_path):
    credentials = reader(tmp_path)
    path = tmp_path / "secrets/key.txt"
    path.write_text("  fictional-marker-first\n", encoding="utf-8")
    assert await credentials.read("example") == "fictional-marker-first"
    await assert_credentials_contract(
        credentials, "example", "fictional-marker-first", errors={"absent": "unknown_ref"}
    )
    path.write_text("fictional-marker-second", encoding="utf-8")
    assert await credentials.read("example") == "fictional-marker-second"


@pytest.mark.parametrize("code", ["unknown_ref", "not_permitted", "missing", "empty", "unreadable"])
async def test_credentials_errors_are_codes_only(tmp_path, code):
    credentials = reader(tmp_path)
    ref = "example"
    if code == "unknown_ref":
        ref = "absent"
    elif code == "not_permitted":
        credentials.plugin_id = "other"
    elif code == "empty":
        (tmp_path / "secrets/key.txt").write_text(" \n", encoding="utf-8")
    elif code == "unreadable":
        (tmp_path / "secrets/key.txt").write_bytes(b"\xfffictional-secret-marker")
    with pytest.raises(CredentialUnavailable) as error:
        await credentials.read(ref)
    assert error.value.code == code
    assert str(error.value) == code


async def test_credentials_reject_changed_path(tmp_path):
    credentials = reader(tmp_path)
    credentials.declarations["example"]["secret"] = "../outside.txt"
    (tmp_path / "outside.txt").write_text("fictional marker", encoding="utf-8")
    with pytest.raises(CredentialUnavailable, match="^unreadable$"):
        await credentials.read("example")


async def test_host_credentials_authorized_surface_and_no_host_logging(tmp_path):
    plugin(tmp_path, "allowed")
    plugin(tmp_path, "other")
    h = host(tmp_path)
    credentials = reader(tmp_path)
    h.credentials = credentials.declarations
    marker = "fictional-secret-marker"
    (tmp_path / "secrets/key.txt").write_text(marker, encoding="utf-8")
    try:
        allowed = await enable(h, "allowed")
        other = await enable(h, "other")
        assert_plugin_context_contract(allowed.ctx, granted=("credentials",))
        assert_plugin_context_contract(other.ctx)
        assert await allowed.ctx.credentials.read("example") == marker
        assert not hasattr(other.ctx, "credentials")
        assert not hasattr(allowed.ctx.credentials, "home")
        assert not hasattr(allowed.ctx.credentials, "account_secret")
        assert marker not in repr(h.log.entries)
        assert marker not in repr(h.failures)
    finally:
        await h.close()


async def test_default_http_is_instance_owned_and_closed(tmp_path):
    plugin(tmp_path, "network-one", permissions=["network"])
    plugin(tmp_path, "network-two", permissions=["network"])
    h = host(tmp_path)
    one = await enable(h, "network-one")
    two = await enable(h, "network-two")
    assert one.owned_http is not two.owned_http
    assert hasattr(one.ctx, "http")
    assert not one.owned_http._closed
    await h.close()
    assert one.owned_http._closed and two.owned_http._closed


async def test_default_http_closed_after_failed_start(tmp_path, monkeypatch):
    from clytie.plugin_host.http import HttpBackend

    closed = []
    original = HttpBackend.close

    async def close(self):
        await original(self)
        closed.append(self)

    monkeypatch.setattr(HttpBackend, "close", close)
    plugin(
        tmp_path,
        "bad-network",
        SOURCE.replace("self.ctx = ctx", "raise ValueError('fictional')"),
        permissions=["network"],
    )
    h = host(tmp_path)
    result = await h.apply(h.plan_enable("companion_a", "bad-network").plan_id)
    assert result["failed"]
    assert len(closed) == 1 and closed[0]._closed
    await h.close()
