"""Loopback HTTP transport; route policy and authentication precede every mutation."""

import asyncio
import hashlib
import json
import os
import re
import socket

from aiohttp import web

from clytie.supervisor.common import write

from . import appearance
from .auth import APIError, Auth, Identity, csrf

ROUTES = [
    ("GET", "/status"),
    ("POST", "/companions/<cid>/plugins/<pid>/config/apply"),
    ("GET", "/backup/status"),
    ("GET", "/backup/list"),
    ("POST", "/backup/create"),
    ("POST", "/backup/verify"),
    ("GET", "/backup/operations/<operation_id>"),
    ("POST", "/backup/operations/<operation_id>/cancel"),
    ("POST", "/backup/restore/plan"),
    ("POST", "/backup/restore/apply"),
    ("POST", "/backup/cleanup/plan"),
    ("POST", "/backup/cleanup/apply"),
    ("GET", "/backup/cleanup/plans/<plan_id>"),
    ("GET", "/backup/plans/<kind>/<plan_id>"),
    ("POST", "/backup/plans/<kind>/<plan_id>/cancel"),
    ("POST", "/backup/memory/plan"),
    *[("POST", "/backup/config/" + x) for x in ("plan", "apply")],
    ("POST", "/backup/restore/source-stopped"),
    ("POST", "/backup/restore/acknowledge"),
    ("GET", "/backup/key/fingerprint"),
    *[("POST", "/backup/key/" + x) for x in ("export", "verify", "acknowledge")],
    *[("POST", "/auth/" + x) for x in ("login", "logout", "reauth")],
    *[("POST", "/admin/" + x) for x in ("password", "logout-all")],
    *[("POST", "/supervisor/" + x) for x in ("stop", "escalate")],
    ("POST", "/silence"),
    *[("POST", "/companions/<cid>/" + x) for x in ("start", "stop", "restart", "proactive")],
    ("GET", "/companions/<cid>/smoke/records"),
    *[("GET" if route == appearance.ROOT else "POST", route) for route in appearance.FIELDS],
    ("GET", "/companions/<cid>/tasks/<task_id>"),
    ("GET", "/companions/<cid>/boundary"),
    ("POST", "/companions/<cid>/boundary/release"),
    *[
        ("POST", "/companions/<cid>/memory/" + x)
        for x in ("export", "thaw", "recovery-code", "import/plan", "import/undo")
    ],
    ("GET", "/companions/<cid>/memory/operations/<operation_id>"),
    ("GET", "/memory/import/plans/<plan_id>"),
    ("POST", "/memory/import/plans/<plan_id>/content"),
    ("POST", "/memory/import/apply"),
    ("GET", "/companions/<cid>/plugins"),
    *[("POST", "/companions/<cid>/plugins/" + x) for x in ("plan", "apply")],
    ("GET", "/companions/<cid>/panels"),
]


def match(method, path):
    for verb, route in ROUTES:
        pattern = re.sub(r"<([a-z_]+)>", r"(?P<\1>[a-zA-Z0-9_-]+)", route)
        found = re.fullmatch("/api/v1" + pattern, path)
        if verb == method and found:
            return route, found.groupdict()
    plugin = re.fullmatch(
        r"/api/v1/companions/(?P<cid>[a-zA-Z0-9_-]+)/plugins/(?P<pid>[a-zA-Z0-9_-]+)/(?P<subpath>.*)",
        path,
    )
    if plugin:
        return "/companions/<cid>/plugins/<pid>/<subpath>", plugin.groupdict()
    raise APIError("not_found", 404)


class Server:
    def __init__(self, home, clock, backend, audit, config=None):
        self.home, self.clock, self.backend, self.audit = home, clock, backend, audit
        self.config = config or {}
        self.auth = Auth(home, clock, self.config)
        self.port, self.runner = None, None

    async def start(self):
        # Bind the actual socket once: no test-and-rebind race.
        first = self.config.get("port", 8765)
        for port in range(first, first + (1 if "port" in self.config else 10)):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("127.0.0.1", port))
                sock.listen(128)
                sock.setblocking(False)
            except OSError:
                sock.close()
                continue
            self.port = sock.getsockname()[1]
            app = web.Application(client_max_size=1024 * 1024)
            app.router.add_route("*", "/{path:.*}", self.handle)
            self.runner = web.AppRunner(app, access_log=None)
            try:
                await self.runner.setup()
                await web.SockSite(self.runner, sock).start()
                write(
                    self.home / "run/api.json",
                    dict(
                        port=self.port,
                        pid=os.getpid(),
                        started_at=self.clock.now().isoformat(),
                    ),
                )
                return self.port
            except BaseException:
                sock.close()
                await self.close()
                raise
        raise APIError("api_bind_failed", 503)

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    async def handle(self, request):
        route, params, identity, result_code = "unmatched", {}, Identity("internal"), "ok"
        session_cookie = None
        try:
            try:
                route, params = match(request.method, request.path)
            except APIError:
                pass
            hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
            if request.headers.get("Host") not in hosts:
                raise APIError("host_rejected", 403)
            origin = request.headers.get("Origin")
            if origin and origin not in {"http://" + h for h in hosts}:
                raise APIError("origin_rejected", 403)
            if route == "unmatched":
                raise APIError("not_found", 404)
            if route != "/auth/login":
                identity = self.auth.authenticate(
                    request.headers.get("Authorization"),
                    request.cookies.get("clytie_session"),
                    request.method,
                    request.headers.get("X-Clytie-CSRF"),
                )
            if route in appearance.FIELDS and request.query:
                raise APIError("invalid_request")
            if route == appearance.ADD:
                request = request.clone(client_max_size=appearance.UPLOAD_BODY_BYTES)
            try:
                data = await request.json() if request.can_read_body else {}
            except web.HTTPRequestEntityTooLarge:
                if route == appearance.ADD:
                    raise APIError("request_too_large", 413) from None
                raise
            if not isinstance(data, dict):
                raise APIError("invalid_request")
            if route == "/auth/login":
                session_cookie = self.auth.login(data.get("password"))
                identity = Identity(
                    "webui:" + hashlib.sha256(session_cookie.encode()).hexdigest()[:8]
                )
                result = {"csrf": csrf(session_cookie)}
            else:
                confirm, password, description = await self.backend.policy(route, params, data)
                if confirm and data.get("confirm") is not True:
                    if password and not self.auth.admin:
                        if description and not description.endswith(("。", "！", "？")):
                            description += "。"
                        description += "建议设置管理口令。"
                    raise APIError("confirmation_required", 428, {"description": description})
                if password:
                    self.auth.require_password(identity, data.get("admin_password"))
                if route == "/auth/logout":
                    self.auth.logout(identity)
                    result = {}
                elif route == "/auth/reauth":
                    self.auth.reauth(identity, data.get("password"))
                    result = {}
                elif route == "/admin/password":
                    self.auth.set_password(data.get("new_password"), data.get("old_password"))
                    result = {}
                elif route == "/admin/logout-all":
                    self.auth.logout_all()
                    result = {}
                else:
                    # Authentication secrets never reach plugins or IPC.
                    clean = {
                        k: v for k, v in data.items() if k not in {"admin_password", "confirm"}
                    }
                    if route == "/backup/config/apply":
                        # Server-owned authorization context; never trust a body field.
                        clean["_backup_config_confirmed"] = data.get("confirm") is True
                    if route in {"/backup/memory/plan", "/backup/restore/plan"}:
                        clean["_source_repository_confirmed"] = data.get("confirm") is True
                    result = await self.backend.call(
                        route, params, clean, dict(request.query), request.method
                    )
            status = 200
            body = {"ok": True, "result": result}
            if "<subpath>" in route:
                status = result["status"]
                body["result"] = result["json"]
                if status >= 400:
                    result_code = "plugin_error"
                    body.update(ok=False, error={"code": result_code, "message": "插件返回错误"})
            response = web.json_response(
                body, status=status, dumps=lambda v: json.dumps(v, ensure_ascii=False)
            )
            if session_cookie:
                response.set_cookie(
                    "clytie_session", session_cookie, httponly=True, samesite="Strict", path="/api/"
                )
            elif identity.session and route not in {
                "/auth/logout",
                "/admin/password",
                "/admin/logout-all",
            }:
                response.set_cookie(
                    "clytie_session",
                    request.cookies["clytie_session"],
                    httponly=True,
                    samesite="Strict",
                    path="/api/",
                )
            if route in {"/auth/logout", "/admin/password", "/admin/logout-all"}:
                response.del_cookie("clytie_session", path="/api/")
            return response
        except APIError as exc:
            result_code = exc.code
            message = (
                appearance.ERROR_MESSAGES.get(exc.code, exc.code)
                if route in appearance.FIELDS
                else exc.code
            )
            body = {"ok": False, "error": {"code": exc.code, "message": message}}
            if exc.result is not None:
                body["result"] = exc.result
            return web.json_response(body, status=exc.status)
        except asyncio.CancelledError:
            result_code = "cancelled"
            raise
        except (ValueError, TypeError, KeyError, web.HTTPException):
            result_code = "invalid_request"
            return web.json_response(
                {"ok": False, "error": {"code": result_code, "message": "请求格式无效"}}, status=400
            )
        except Exception:
            result_code = "operation_failed"
            return web.json_response(
                {"ok": False, "error": {"code": result_code, "message": "操作失败"}}, status=500
            )
        finally:
            if request.method != "GET":
                self.audit.append(
                    "api",
                    result=result_code,
                    actor=identity.actor,
                    route=route,
                    target={k: v for k, v in params.items() if k != "subpath"},
                )
