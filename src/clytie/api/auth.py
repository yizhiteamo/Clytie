"""Local authentication; no credential is included in errors or audit values."""

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from clytie.supervisor.common import read, write


class APIError(Exception):
    def __init__(self, code, status=400, result=None):
        self.code, self.status, self.result = code, status, result
        super().__init__(code)


class RequestNotDelivered(APIError):
    """Local rejection before any request frame is handed to the transport."""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def csrf(value):
    return digest("clytie-csrf:" + value)


@dataclass
class Identity:
    actor: str
    session: str | None = None


class Auth:
    def __init__(self, home, clock, config=None):
        self.home, self.clock, self.config = Path(home), clock, config or {}
        self.admin_path = self.home / "secrets/admin.json"
        self.sessions_path = self.home / "secrets/sessions.json"
        self.admin = read(self.admin_path)
        saved = read(self.sessions_path, {})
        self.sessions = (
            saved.get("sessions", {}) if saved.get("generation") == self.generation else {}
        )
        self.failures, self.locked_until = 0, 0
        self.token = secrets.token_urlsafe(32)
        path = self.home / "run/api-token"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            os.write(fd, self.token.encode())
        finally:
            os.close(fd)

    @property
    def generation(self):
        return self.admin.get("generation") if self.admin else None

    def refresh(self):
        current = read(self.admin_path)
        if current != self.admin:
            self.admin = current
            self.sessions.clear()
            self.save()

    def save(self):
        write(self.sessions_path, dict(generation=self.generation, sessions=self.sessions))

    def now(self):
        return self.clock.now().timestamp()

    def verify(self, value):
        self.refresh()
        if self.clock.monotonic() < self.locked_until:
            raise APIError("login_locked", 429)
        if value is None:
            raise APIError("invalid_password", 401)
        good = False
        if self.admin and isinstance(value, str):
            actual = hashlib.scrypt(
                value.encode(),
                salt=bytes.fromhex(self.admin["salt"]),
                n=self.admin["n"],
                r=self.admin["r"],
                p=self.admin["p"],
                dklen=32,
            ).hex()
            good = hmac.compare_digest(actual, self.admin["hash"])
        if not good:
            self.failures += 1
            settings = self.config.get("login_lockout", {})
            if self.failures >= settings.get("failures", 5):
                self.locked_until = self.clock.monotonic() + settings.get("seconds", 60)
            raise APIError("invalid_password", 401)
        self.failures = 0

    def set_password(self, new, old=None):
        self.refresh()
        if self.admin:
            self.verify(old)
        if not isinstance(new, str) or not new:
            raise APIError("invalid_request")
        salt = secrets.token_bytes(16)
        self.admin = dict(
            generation=secrets.token_hex(16),
            salt=salt.hex(),
            n=2**14,
            r=8,
            p=1,
            hash=hashlib.scrypt(new.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32).hex(),
        )
        write(self.admin_path, self.admin)
        self.logout_all()

    def login(self, value):
        self.verify(value)
        raw = secrets.token_urlsafe(32)
        self.sessions[digest(raw)] = dict(created=self.now(), used=self.now(), reauth=self.now())
        self.save()
        return raw

    def authenticate(self, bearer, cookie, method, supplied_csrf):
        self.refresh()
        if bearer:
            if not hmac.compare_digest(bearer, "Bearer " + self.token):
                raise APIError("unauthorized", 401)
            return Identity("cli")
        key = digest(cookie or "")
        row = self.sessions.get(key) if self.admin else None
        if (
            not row
            or self.now() - row["used"] >= self.config.get("session_idle_days", 30) * 24 * 60 * 60
        ):
            raise APIError("unauthorized", 401)
        if method != "GET" and not hmac.compare_digest(supplied_csrf or "", csrf(cookie)):
            raise APIError("csrf_rejected", 403)
        row["used"] = self.now()
        self.save()
        return Identity("webui:" + key[:8], key)

    def require_password(self, identity, value):
        self.refresh()
        if identity.actor == "cli":
            if self.admin:
                self.verify(value)
        elif (
            self.now() - self.sessions[identity.session]["reauth"]
            >= self.config.get("reauth_minutes", 15) * 60
        ):
            raise APIError("reauth_required", 401)

    def reauth(self, identity, value):
        self.verify(value)
        if identity.session:
            self.sessions[identity.session]["reauth"] = self.now()
            self.save()

    def logout_all(self):
        self.sessions.clear()
        self.save()

    def logout(self, identity):
        self.sessions.pop(identity.session, None)
        self.save()
