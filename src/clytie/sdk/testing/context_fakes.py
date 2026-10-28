"""上下文的离线参考实现。只操作注入的虚构记录和测试目录，不联网或启动进程。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from random import Random

from clytie.sdk.context import RevisionConflict, ServiceUnavailable
from clytie.sdk.immutable import FrozenDict, freeze, thaw
from clytie.sdk.types import (
    Actor,
    ApiResponse,
    AssetRef,
    BusEvent,
    CompanionState,
    Conversation,
    HttpResponse,
    MemoryDecay,
    MemoryDraft,
    MemoryRecord,
    ModelTurn,
    ProcessResult,
    TokenUsage,
)

from .clock import MockClock


def json_copy(value):
    return json.loads(json.dumps(thaw(value), ensure_ascii=False, allow_nan=False))


class FakeLog:
    def __init__(self):
        self.entries = []

    def _log(self, level, msg, fields):
        self.entries.append((level, msg, freeze(json_copy(fields))))

    def debug(self, msg, **fields):
        self._log("debug", msg, fields)

    def info(self, msg, **fields):
        self._log("info", msg, fields)

    def warning(self, msg, **fields):
        self._log("warning", msg, fields)

    def error(self, msg, **fields):
        self._log("error", msg, fields)


class FakeStorage:
    def __init__(self, path, clock):
        self.path, self.clock = Path(path), clock
        self.path.mkdir(parents=True, exist_ok=True)
        self._values = {}

    def _key(self, key):
        if not isinstance(key, str) or len(key) > 200:
            raise ValueError("storage key must be a string of at most 200 characters")

    async def get(self, key):
        self._key(key)
        await self.clock.sleep(0)
        return json_copy(self._values.get(key))

    async def set(self, key, value):
        self._key(key)
        value = json_copy(value)
        if (
            len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            > 1024 * 1024
        ):
            raise ValueError("storage value exceeds one MiB")
        await self.clock.sleep(0)
        self._values[key] = value

    async def delete(self, key):
        self._key(key)
        await self.clock.sleep(0)
        self._values.pop(key, None)

    async def keys(self, prefix=""):
        self._key(prefix)
        await self.clock.sleep(0)
        return sorted(key for key in self._values if key.startswith(prefix))


class FakeEvents:
    def __init__(self, plugin_id, clock):
        self.plugin_id, self.clock = plugin_id, clock
        self.published = []

    async def publish(self, name, payload):
        if not name.startswith(self.plugin_id + "."):
            raise ValueError("event name must have the publishing plugin prefix")
        payload = json_copy(payload)
        await self.clock.sleep(0)
        self.published.append(
            BusEvent(name=name, payload=payload, at=self.clock.now(), source=self.plugin_id)
        )


class FakeState:
    def __init__(self, ctx):
        self.ctx = ctx
        self.snapshot = CompanionState(companion_id=ctx.companion_id, states={})

    async def set(self, payload):
        payload = json_copy(payload)
        if not isinstance(payload, dict):
            raise ValueError("state must be a JSON object")
        await self.ctx.clock.sleep(0)
        self.snapshot = CompanionState(
            companion_id=self.ctx.companion_id, states={self.ctx.plugin_id: payload}
        )
        self.ctx.events.published.append(
            BusEvent(
                name="companion.state_changed",
                source="core",
                at=self.ctx.clock.now(),
                payload={
                    "companion_id": self.ctx.companion_id,
                    "plugin_id": self.ctx.plugin_id,
                    "state": payload,
                },
            )
        )


class FakeAssets:
    def __init__(self, path, clock):
        self._path, self.clock = Path(path), clock
        self._items = {}

    async def info(self, asset_id):
        await self.clock.sleep(0)
        return self._items[asset_id][0]

    async def read(self, asset_id):
        await self.clock.sleep(0)
        return self._items[asset_id][1]

    async def path(self, asset_id):
        await self.clock.sleep(0)
        self._items[asset_id]
        return self._path / asset_id


class FakeAssetWriter:
    def __init__(self, reader):
        self.reader = reader

    async def put(self, data, media_type, origin):
        await self.reader.clock.sleep(0)
        data = bytes(data)
        asset_id = hashlib.sha256(data).hexdigest()
        if asset_id in self.reader._items:
            return self.reader._items[asset_id][0]
        ref = AssetRef(asset_id=asset_id, media_type=media_type, size=len(data), origin=origin)
        self.reader._path.mkdir(parents=True, exist_ok=True)
        path = self.reader._path / asset_id
        if not path.exists():
            path.write_bytes(data)
            path.chmod(0o444)
        self.reader._items[asset_id] = ref, data
        return ref


class FakeCandidateReader:
    def __init__(self, records=(), *, allowed_visibility=("public",), clock=None):
        self.records = tuple(records)
        self.allowed_visibility = frozenset(allowed_visibility)
        self.clock = clock or MockClock()

    def _visible(self):
        return [
            r
            for r in self.records
            if r.visibility in self.allowed_visibility and not r.retracted and not r.decay.frozen
        ]

    async def iter(self, *, kinds=None, scope=None, updated_after=None, batch=200):
        if not isinstance(batch, int) or isinstance(batch, bool) or batch < 1:
            raise ValueError("batch must be positive")
        records = [
            r
            for r in self._visible()
            if (kinds is None or r.kind in kinds)
            and (scope is None or r.scope == scope)
            and (updated_after is None or r.updated_at > updated_after)
        ]
        for offset in range(0, len(records), batch):
            await self.clock.sleep(0)
            yield records[offset : offset + batch]

    async def get(self, record_ids):
        await self.clock.sleep(0)
        records = {r.record_id: r for r in self._visible()}
        return [records[key] for key in record_ids if key in records]

    async def count(self):
        await self.clock.sleep(0)
        return len(self._visible())


class FakeMemory:
    def __init__(self, clock, records=(), *, private=False, scene="public_group"):
        self.clock, self.private, self.scene = clock, private, scene
        self._records = {r.record_id: r for r in records}

    def _reader(self, scene):
        visible = ["public", "relationship"]
        if self.private and scene == "owner_private":
            visible.append("private")
        return FakeCandidateReader(
            self._records.values(), allowed_visibility=visible, clock=self.clock
        )

    async def search(self, text, *, scene, limit=10):
        if limit < 0:
            raise ValueError("limit must be nonnegative")
        return [
            r async for batch in self._reader(scene).iter() for r in batch if text in r.content
        ][:limit]

    async def get(self, record_id):
        records = await self._reader(self.scene).get([record_id])
        return records[0] if records else None


class FakeWritableMemory(FakeMemory):
    async def write(self, draft: MemoryDraft):
        await self.clock.sleep(0)
        index = len(self._records) + 1
        while f"{index:032x}" in self._records:
            index += 1
        record = MemoryRecord(
            **draft.model_dump(),
            record_id=f"{index:032x}",
            revision=1,
            write_mode="auto",
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
            decay=MemoryDecay(weight=1, frozen=False),
            retracted=False,
        )
        self._records[record.record_id] = record
        return record

    async def revise(self, record_id, expected_revision, changes):
        await self.clock.sleep(0)
        record = self._records[record_id]
        if record.revision != expected_revision:
            raise RevisionConflict(record_id)
        values = {
            **json_copy(changes),
            "record_id": record_id,
            "revision": record.revision + 1,
            "updated_at": self.clock.now(),
            "write_mode": "auto",
        }
        updated = record.model_copy(update=values)
        self._records[record_id] = updated
        return updated

    async def retract(self, record_id, expected_revision, reason):
        await self.revise(record_id, expected_revision, {"retracted": True})


class FakeApiClient:
    def __init__(self, response=None, clock=None):
        self.response = response or ApiResponse(status=200, json={})
        self.clock = clock or MockClock()
        self.requests = []

    async def request(self, method, path, *, json=None, query=None):
        if not path.startswith("/api/v1/"):
            raise ValueError("API path must start with /api/v1/")
        body, query = json_copy(json), json_copy(query)
        await self.clock.sleep(0)
        self.requests.append((method, path, body, query))
        return self.response


class FakeServiceProxy:
    def __init__(self, clock, *, available=True):
        self.clock, self.available = clock, available

    async def call(self, method, args):
        await self.clock.sleep(0)
        if not self.available:
            raise ServiceUnavailable(method)
        return FrozenDict({"method": method, "args": json_copy(args)})


class FakeServices:
    def __init__(self, services=None):
        self.services = dict(services or {})

    def get(self, name):
        return self.services.get(name)


class FakePeer:
    def __init__(self, clock):
        self.clock, self.requests = clock, []

    async def send(self, companion_id, kind, payload):
        payload = json_copy(payload)
        await self.clock.sleep(0)
        self.requests.append((companion_id, kind, payload))
        return f"fake-peer-{len(self.requests)}"

    async def request(self, companion_id, kind, payload, timeout_s):
        await self.send(companion_id, kind, payload)
        return FrozenDict({"reply": "fictional"})


class FakeModelInvoker:
    def __init__(self, ctx):
        self.ctx = ctx

    async def generate(self, req, cancel, *, purpose):
        cancel.raise_if_cancelled()
        await self.ctx.clock.sleep(0)
        cancel.raise_if_cancelled()
        self.ctx.log.info("model.invoke", purpose=purpose)
        return ModelTurn(finish="stop", text="fictional", usage=TokenUsage())


class FakeHttp:
    def __init__(self, clock, response=None):
        self.clock, self.requests = clock, []
        self.response = response or HttpResponse(status=200, headers={}, body=b"fictional")

    async def request(
        self, method, url, *, headers=None, body=None, timeout_s=30, max_bytes=20 * 1024 * 1024
    ):
        if len(self.response.body) > max_bytes:
            raise ValueError("response exceeds max_bytes")
        await self.clock.sleep(0)
        self.requests.append((method, url, json_copy(headers), body, timeout_s, max_bytes))
        return self.response


class FakeProactive:
    def __init__(self, clock, *, allowed=True):
        self.clock, self.allowed, self.intents = clock, allowed, []

    async def submit(self, conversation_id, parts, *, reason):
        await self.clock.sleep(0)
        if not self.allowed:
            raise PermissionError("fictional boundary denial")
        self.intents.append((conversation_id, tuple(parts), reason))
        return f"{len(self.intents):032x}"


class FakeSubprocess:
    def __init__(self, path, clock, result=None):
        self.path, self.clock, self.calls = Path(path), clock, []
        self.result = result or ProcessResult(
            returncode=0, stdout=b"fictional", stderr=b"", truncated=False
        )

    async def run(self, args, *, timeout_s, env=None, max_output_bytes=10 * 1024 * 1024):
        if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
            raise ValueError("args must be a list of strings")
        if max_output_bytes < 0:
            raise ValueError("max_output_bytes must be nonnegative")
        await self.clock.sleep(0)
        self.calls.append((tuple(args), timeout_s, json_copy(env), self.path))
        stdout = self.result.stdout[:max_output_bytes]
        stderr = self.result.stderr[: max_output_bytes - len(stdout)]
        return self.result.model_copy(
            update={
                "stdout": stdout,
                "stderr": stderr,
                "truncated": self.result.truncated
                or len(stdout) + len(stderr) < len(self.result.stdout) + len(self.result.stderr),
            }
        )


class FakeIdentity:
    def __init__(self, channel_id, *, actors=None, conversations=None):
        self.channel_id, self.actors, self.conversations = (
            channel_id,
            dict(actors or {}),
            dict(conversations or {}),
        )

    def resolve_actor(self, account):
        if account in self.actors:
            return self.actors[account]
        actor_id = f"anon:{self.channel_id}:{hashlib.sha256(account.encode()).hexdigest()[:16]}"
        return Actor(actor_id=actor_id, role="stranger", channel_id=self.channel_id)

    def resolve_conversation(self, kind, platform_id):
        if (kind, platform_id) in self.conversations:
            return self.conversations[kind, platform_id]
        digest = hashlib.sha256(platform_id.encode()).hexdigest()[:16]
        return Conversation(
            conversation_id=f"anon:{self.channel_id}:{digest}",
            kind=kind,
            scene="public_group" if kind == "group" else "stranger_private",
            channel_id=self.channel_id,
        )

    def account_for(self, actor_id):
        return next((k for k, v in self.actors.items() if v.actor_id == actor_id), None)

    def platform_id_for(self, conversation_id):
        return next(
            (k[1] for k, v in self.conversations.items() if v.conversation_id == conversation_id),
            None,
        )


class FakeContext:
    def __init__(
        self,
        plugin_id="fake-plugin",
        companion_id="companion_a",
        config=None,
        clock=None,
        random=None,
        *,
        permissions=(),
        root=None,
        services=None,
        channel=False,
        records=(),
        scene="public_group",
        identity=None,
    ):
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", plugin_id):
            raise ValueError("invalid plugin id")
        self.plugin_id, self.companion_id = plugin_id, companion_id
        self.config, self.clock, self.random = (
            FrozenDict(json_copy(config or {})),
            clock or MockClock(),
            random or Random(0),
        )
        self.log = FakeLog()
        root = Path(root) if root is not None else Path(".cache/sdk-testing")
        self.storage = FakeStorage(root / "plugin-data" / plugin_id, self.clock)
        self.events, self.state = FakeEvents(plugin_id, self.clock), FakeState(self)
        self.assets = FakeAssets(root / "assets", self.clock)
        if "assets.write" in permissions:
            self.asset_writer = FakeAssetWriter(self.assets)
        if any(p.startswith("memory.") for p in permissions):
            cls = FakeWritableMemory if "memory.write" in permissions else FakeMemory
            self.memory = cls(
                self.clock, records, private="memory.read.private" in permissions, scene=scene
            )
        if services is not None:
            self.services = FakeServices(services)
        if "peer" in permissions:
            self.peer = FakePeer(self.clock)
        if "model.invoke" in permissions:
            self.model = FakeModelInvoker(self)
        if "network" in permissions:
            self.http = FakeHttp(self.clock)
        if "message.proactive" in permissions:
            self.proactive = FakeProactive(self.clock)
        if "subprocess" in permissions:
            self.subprocess = FakeSubprocess(self.storage.path, self.clock)
        if channel:
            self.identity = identity or FakeIdentity(plugin_id)
