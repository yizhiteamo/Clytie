"""Local API client and daemon discovery; never falls back to a memory database."""

from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout

from clytie.sdk.types import ApiResponse
from clytie.supervisor.common import read
from clytie.supervisor.entry import status


class NotRunning(Exception):
    pass


class Client:
    def __init__(self, home):
        self.home = Path(home)

    async def request(self, method, path, *, json=None, query=None):
        if not path.startswith("/api/v1/") or ".." in path or "?" in path or "#" in path:
            raise ValueError("invalid API path")
        if not status(self.home)["running"]:
            raise NotRunning()
        operation_id = None
        if method == "POST" and "/memory/" in path:
            from uuid import uuid4

            json = dict(json or {})
            operation_id = json.setdefault("operation_id", uuid4().hex)
        try:
            endpoint = read(self.home / "run/api.json")
            value = (self.home / "run/api-token").read_text(encoding="utf-8")
            url = "http://127.0.0.1:" + str(endpoint["port"]) + path
            headers = {}
            headers["Authorization"] = "Bearer " + value
            async with ClientSession(
                timeout=ClientTimeout(total=None, sock_connect=5), trust_env=False
            ) as session:
                async with session.request(
                    method,
                    url,
                    json=json,
                    params=query,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    return ApiResponse(status=response.status, json=await response.json())
        except (OSError, ClientError, TimeoutError, TypeError, KeyError, ValueError):
            if operation_id is not None:
                return ApiResponse(
                    status=503,
                    json={
                        "ok": False,
                        "error": {
                            "code": "management_result_unconfirmed",
                            "message": "结果尚未确认，请按操作编号查询；不要重新执行",
                        },
                        "result": {"operation_id": operation_id},
                    },
                )
            raise NotRunning() from None


async def detached_start(home, clock):
    import subprocess

    from clytie.supervisor.processes import creation, python_process

    if status(home)["running"]:
        return {"ok": False, "error": {"code": "already_running", "message": "已在运行"}}
    directory = home / "logs/supervisor"
    directory.mkdir(parents=True, exist_ok=True)
    executable, env = python_process()
    with (directory / "process.log").open("ab") as log:
        process = subprocess.Popen(
            [executable, "-m", "clytie", "_supervisor", "--home", str(home), "--detach"],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            env=env,
            **creation(detached=True),
        )
    try:
        return await _await_start(process, Client(home), clock)
    finally:
        process.stdin.close()


async def _await_start(process, client, clock):
    from clytie.plugin_host.support import timed
    from clytie.supervisor.processes import frame

    deadline = clock.monotonic() + 30
    unreadable = False
    while clock.monotonic() < deadline:
        if process.poll() is not None:
            return {"ok": False, "error": {"code": "startup_failed", "message": "后台启动失败"}}
        try:
            info = read(client.home / "run/api.json", {})
            unreadable = False
        except PermissionError:
            # Readiness is not established. This may be persistent denial, not
            # necessarily a transient sharing conflict; keep the original deadline.
            unreadable = True
            info = {}
        except OSError:
            return {
                "ok": False,
                "error": {"code": "startup_failed", "message": "后台启动状态文件无法读取"},
            }
        if info.get("pid") == process.pid:
            try:
                response = await timed(
                    client.request("GET", "/api/v1/status"),
                    clock,
                    max(0, deadline - clock.monotonic()),
                )
                if response.status == 200:
                    frame(process.stdin, {"type": "startup_confirmed", "seq": 0})
                    return {
                        "ok": True,
                        "result": {"message": "已在后台运行，用 clytie supervisor stop 停止"},
                    }
            except (NotRunning, TimeoutError):
                pass
            except OSError:
                return {
                    "ok": False,
                    "error": {"code": "startup_failed", "message": "后台启动握手失败"},
                }
        await clock.sleep(min(0.1, max(0, deadline - clock.monotonic())))
    # Closing the launch pipe requests safe cancellation even if HTTP never became ready.
    return {
        "ok": False,
        "error": {
            "code": "startup_timeout",
            "message": (
                "后台启动超时：状态文件不可读，已请求安全停止"
                if unreadable
                else "后台启动超时，已请求安全停止"
            ),
        },
    }
