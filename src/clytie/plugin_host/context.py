"""仅暴露声明且授权的方法；外部能力由本体注入。"""

from random import Random
from types import SimpleNamespace

from clytie.sdk.context import ServiceUnavailable, validate_proactive
from clytie.sdk.immutable import FrozenDict
from clytie.sdk.types import AssetRef, HttpResponse, ModelTurn, ProcessResult

from .memory import memory_view
from .support import json_copy, timed
from .surface import surface


class BackendUnavailable(ValueError):
    def __init__(self, permission):
        self.permission = permission
        super().__init__("backend_unavailable")


class AssetWriter:
    def __init__(self, backend):
        self._backend = backend

    async def put(self, data, media_type, origin):
        return AssetRef.model_validate(await self._backend.put(bytes(data), media_type, origin))


class Peer:
    def __init__(self, backend):
        self._backend = backend

    async def send(self, companion_id, kind, payload):
        result = await self._backend.send(companion_id, kind, json_copy(payload))
        if not isinstance(result, str):
            raise ValueError("peer must return a request id")
        return result

    async def request(self, companion_id, kind, payload, timeout_s):
        return json_copy(
            await self._backend.request(companion_id, kind, json_copy(payload), timeout_s)
        )


class Proactive:
    def __init__(self, backend):
        self._backend = backend

    async def submit(self, conversation_id, *, topic=None, material=None, parts=None, reason):
        validate_proactive(topic=topic, material=material, parts=parts)
        result = await self._backend.submit(
            conversation_id, topic=topic, material=json_copy(material), parts=parts, reason=reason
        )
        if not isinstance(result, str):
            raise ValueError("proactive backend must return a trace id")
        return result


class Context(SimpleNamespace):
    def __setattr__(self, name, value):
        raise AttributeError("plugin context is immutable")

    def __delattr__(self, name):
        raise AttributeError("plugin context is immutable")


class ReadAssets:
    def __init__(self, backend=None):
        self._backend = backend

    def _require(self, asset_id):
        if self._backend is None:
            raise KeyError(asset_id)

    async def info(self, asset_id):
        self._require(asset_id)
        return AssetRef.model_validate(await self._backend.info(asset_id))

    async def read(self, asset_id):
        self._require(asset_id)
        result = await self._backend.read(asset_id)
        if not isinstance(result, bytes):
            raise ValueError("asset backend must return bytes")
        return result

    async def path(self, asset_id):
        self._require(asset_id)
        path = await self._backend.path(asset_id)
        if not path.is_file() or path.stat().st_mode & 0o222:
            raise ValueError("asset backend path must be read-only")
        return path


class Events:
    def __init__(self, host, companion_id, plugin_id, names):
        self._host, self._companion, self._plugin = host, companion_id, plugin_id
        self._names = tuple(names)

    async def publish(self, name, payload):
        if not name.startswith(self._plugin + "."):
            raise ValueError("event must use plugin prefix")
        if not self._host.matches(name, self._names):
            raise ValueError("event must be declared in the manifest")
        await self._host.emit(self._companion, name, json_copy(payload), self._plugin)


class State:
    def __init__(self, host, companion_id, plugin_id):
        self._host, self._companion, self._plugin = host, companion_id, plugin_id

    async def set(self, payload):
        payload = json_copy(payload)
        if not isinstance(payload, dict):
            raise ValueError("plugin state must be a JSON object")
        self._host.states.setdefault(self._companion, {})[self._plugin] = payload
        await self._host.emit(
            self._companion,
            "companion.state_changed",
            {
                "companion_id": self._companion,
                "plugin_id": self._plugin,
                "state": payload,
            },
        )


class ServiceProxy:
    def __init__(self, host, companion, provider, name, methods):
        self._host, self._companion, self._provider = host, companion, provider
        self._name, self._methods = name, tuple(methods)

    async def call(self, method, args):
        if method not in self._methods:
            raise ValueError("undeclared service method")
        args = json_copy(args)
        if not isinstance(args, dict):
            raise ValueError("service arguments must be a JSON object")
        try:
            result = await self._host.invoke(
                self._companion, self._provider, "service", self._name, method, args
            )
        except self._host.failure_type as exc:
            raise ServiceUnavailable(self._name) from exc
        if not isinstance(result, dict):
            raise ServiceUnavailable(self._name)
        return json_copy(result)


class Services:
    def __init__(self, host, companion, available):
        self._host, self._companion = host, companion
        self._proxies = {
            name: ServiceProxy(host, companion, provider, name, methods)
            for name, (provider, methods) in available.items()
        }

    def get(self, name):
        proxy = self._proxies.get(name)
        if proxy is None or not self._host.is_active(self._companion, proxy._provider):
            return None
        return surface(proxy, ("call",))


class Subprocess:
    def __init__(self, backend, path, clock):
        self._backend, self._path, self._clock = backend, path, clock

    async def run(self, args, *, timeout_s, env=None, max_output_bytes=10 * 1024 * 1024):
        if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
            raise ValueError("subprocess args must be a nonempty string list")
        if timeout_s <= 0 or max_output_bytes < 0:
            raise ValueError("invalid subprocess budget")
        if env is not None and (
            not isinstance(env, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
        ):
            raise ValueError("subprocess environment must contain strings")
        result = ProcessResult.model_validate(
            await timed(
                self._backend.run(
                    list(args),
                    timeout_s=timeout_s,
                    env=dict(env) if env is not None else None,
                    max_output_bytes=max_output_bytes,
                    cwd=self._path,
                    shell=False,
                ),
                self._clock,
                timeout_s,
            )
        )
        stdout = result.stdout[:max_output_bytes]
        stderr = result.stderr[: max(0, max_output_bytes - len(stdout))]
        return ProcessResult(
            returncode=result.returncode,
            stdout=stdout,
            stderr=stderr,
            truncated=result.truncated or stdout != result.stdout or stderr != result.stderr,
        )


class Http:
    def __init__(self, backend, clock):
        self._backend, self._clock = backend, clock

    async def request(
        self,
        method,
        url,
        *,
        headers=None,
        body=None,
        timeout_s=30,
        max_bytes=None,
        follow_redirects: bool = True,
    ):
        from .http import ResponseTooLarge, media_download_limit

        media_limit = media_download_limit.get()
        if max_bytes is None:
            max_bytes = media_limit if media_limit is not None else 20 * 1024 * 1024
        elif media_limit is not None:
            max_bytes = min(max_bytes, media_limit)
        if timeout_s <= 0 or max_bytes < 0:
            raise ValueError("invalid HTTP budget")
        result = HttpResponse.model_validate(
            await timed(
                self._backend.request(
                    method,
                    url,
                    headers=headers,
                    body=body,
                    timeout_s=timeout_s,
                    max_bytes=max_bytes,
                    follow_redirects=follow_redirects,
                ),
                self._clock,
                timeout_s,
            )
        )
        if len(result.body) > max_bytes:
            raise ResponseTooLarge("HTTP response exceeds max_bytes")
        return result


class Model:
    def __init__(self, backend, log):
        self._backend, self._log = backend, log

    async def generate(self, req, cancel, *, purpose):
        cancel.raise_if_cancelled()
        self._log.info("model.invoke", purpose=purpose)
        return ModelTurn.model_validate(await self._backend.generate(req, cancel, purpose=purpose))


class WebSocket:
    def __init__(self, backend):
        self._backend = backend

    async def connect(self, url, *, headers=None, timeout_s=10, max_message_bytes=16 * 1024 * 1024):
        connection = await self._backend.connect(
            url, headers=headers, timeout_s=timeout_s, max_message_bytes=max_message_bytes
        )
        methods = surface(connection, ("send_text", "receive", "close"))

        class Connection:
            __slots__ = ()

            def __getattribute__(self, name):
                if name in {"closed", "close_code"}:
                    return getattr(connection, name)
                return getattr(methods, name)

            def __dir__(self):
                return ["close", "close_code", "closed", "receive", "send_text"]

        return Connection()


def build_context(host, runtime, config, backends, *, permissions=None, migration=False):
    manifest = runtime.manifest
    declared = frozenset(manifest.data["permissions"])
    granted = declared if permissions is None else declared & frozenset(permissions)
    values = {
        "plugin_id": manifest.id,
        "companion_id": runtime.companion_id,
        "config": FrozenDict(json_copy(config)),
        "log": host.log,
        "clock": host.clock,
        "random": Random(host.seed),
        "storage": runtime.storage,
        "events": Events(
            host,
            runtime.companion_id,
            manifest.id,
            manifest.data["contributes"].get("events", {}).get("publish", ()),
        ),
        "state": State(host, runtime.companion_id, manifest.id),
        "assets": ReadAssets(backends.get("assets")),
    }
    channel = (
        not migration
        and "channel" in manifest.data["contributes"]
        and runtime.unit_role in {None, "channel"}
    )
    if channel:
        from .credentials import ChannelCredentials

        values["credentials"] = ChannelCredentials(
            host.home, manifest.id, host.credentials, host.accounts.get(runtime.account_ref)
        )
    elif not migration and any(
        manifest.id in entry["plugins"] for entry in host.credentials.values()
    ):
        from .credentials import Credentials

        values["credentials"] = Credentials(host.home, manifest.id, host.credentials)
    options = {
        "assets.write": "asset_writer",
        "peer": "peer",
        "model.invoke": "model",
        "network": "http",
        "message.proactive": "proactive",
        "subprocess": "subprocess",
    }
    for permission, name in options.items():
        if permission not in granted:
            continue
        if name not in backends:
            raise BackendUnavailable(permission)
        backend = backends[name]
        if name == "subprocess":
            backend = Subprocess(backend, runtime.storage.path, host.clock)
        elif name == "http":
            backend = Http(backend, host.clock)
        elif name == "model":
            backend = Model(backend, host.log)
        elif name == "asset_writer":
            backend = AssetWriter(backend)
        elif name == "peer":
            backend = Peer(backend)
        elif name == "proactive":
            backend = Proactive(backend)
        values[name] = backend
    if "network" in granted:
        if "websocket" not in backends:
            raise BackendUnavailable("network")
        values["websocket"] = WebSocket(backends["websocket"])
    if any(p.startswith("memory.") for p in granted):
        if "memory" not in backends:
            raise BackendUnavailable("memory")
        values["memory"] = memory_view(
            backends["memory"],
            granted,
            host.private_scenes.get(runtime.companion_id, ("owner_private",)),
        )
    services = {}
    for dependency in manifest.data.get("dependencies", {}):
        provider = host.manifests.get(dependency)
        if provider is not None:
            for declaration in provider.data["contributes"].get("services", ()):
                services[declaration["name"]] = (dependency, declaration["methods"])
    if services and not migration:
        values["services"] = Services(host, runtime.companion_id, services)
    if (
        not migration
        and "channel" in manifest.data["contributes"]
        and runtime.unit_role in {None, "channel"}
    ):
        if "identity" not in backends:
            raise BackendUnavailable("identity")
        values["identity"] = backends["identity"]
    exposed = {
        "log": ("debug", "info", "warning", "error"),
        "clock": ("now", "monotonic", "sleep", "sleep_until", "local_tz"),
        "storage": ("path", "get", "set", "delete", "keys"),
        "events": ("publish",),
        "state": ("set",),
        "assets": ("info", "read", "path"),
        "services": ("get",),
        "subprocess": ("run",),
        "http": ("request",),
        "credentials": ("read", "account_secret") if channel else ("read",),
        "websocket": ("connect",),
        "model": ("generate",),
        "asset_writer": ("put",),
        "peer": ("send", "request"),
        "proactive": ("submit",),
        "identity": (
            "resolve_actor",
            "resolve_conversation",
            "account_for",
            "platform_id_for",
            "self_account",
        ),
    }
    for name, members in exposed.items():
        if name in values:
            values[name] = surface(values[name], members)
    return surface(Context(**values), values)
