"""A4.2 authentication uses temporary credentials and virtual time only."""

import json
import os
import secrets

import pytest

from clytie.api.auth import APIError, Auth, Identity, csrf
from clytie.sdk.testing import MockClock


def test_password_sessions_restart_expiry_and_csrf(tmp_path):
    clock = MockClock()
    auth = Auth(tmp_path, clock)
    value = secrets.token_urlsafe(24)
    auth.set_password(value)
    assert value not in (tmp_path / "secrets/admin.json").read_text()
    raw = auth.login(value)
    ident = auth.authenticate(None, raw, "GET", None)
    clock.advance(29 * 24 * 60 * 60)
    auth.authenticate(None, raw, "GET", None)
    restarted = Auth(tmp_path, clock)
    assert restarted.authenticate(None, raw, "GET", None).session == ident.session
    with pytest.raises(APIError, match="csrf_rejected"):
        restarted.authenticate(None, raw, "POST", None)
    restarted.authenticate(None, raw, "POST", csrf(raw))
    with pytest.raises(APIError, match="reauth_required"):
        restarted.require_password(ident, None)
    restarted.reauth(ident, value)
    restarted.require_password(ident, None)
    clock.advance(30 * 24 * 60 * 60)
    with pytest.raises(APIError, match="unauthorized"):
        restarted.authenticate(None, raw, "GET", None)


def test_lockout_cli_and_password_change(tmp_path):
    clock = MockClock()
    auth = Auth(tmp_path, clock)
    value = secrets.token_urlsafe(24)
    auth.set_password(value)
    raw = auth.login(value)
    for _ in range(5):
        with pytest.raises(APIError, match="invalid_password"):
            auth.require_password(Identity("cli"), "harmless")
    with pytest.raises(APIError, match="login_locked"):
        auth.login(value)
    clock.advance(60)
    auth.login(value)
    with pytest.raises(APIError):
        auth.set_password("harmless")
    auth.set_password("harmless", value)
    with pytest.raises(APIError, match="unauthorized"):
        auth.authenticate(None, raw, "GET", None)
    auth.logout_all()
    assert not auth.sessions


def test_restart_token_and_manual_password_removal(tmp_path):
    clock = MockClock()
    auth = Auth(tmp_path, clock)
    old = auth.token
    auth.set_password("harmless")
    raw = auth.login("harmless")
    auth = Auth(tmp_path, clock)
    with pytest.raises(APIError, match="unauthorized"):
        auth.authenticate("Bearer " + old, None, "GET", None)
    auth.authenticate("Bearer " + auth.token, None, "POST", None)
    if os.name != "nt":
        assert (tmp_path / "run/api-token").stat().st_mode & 0o777 == 0o600
    # Simulate the owner's explicit manual deletion; production never deletes this file.
    (tmp_path / "secrets/admin.json").unlink()
    with pytest.raises(APIError, match="unauthorized"):
        auth.authenticate(None, raw, "GET", None)
    assert not json.loads((tmp_path / "secrets/sessions.json").read_text())["sessions"]
